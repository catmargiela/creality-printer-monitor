"""MCP server: lets Claude (Desktop, Code…) control the printers.

It goes through the app's HTTP API (a single connection per printer, and all of
the app's features: camera, slicing, history, clear error messages).

Two ways to use it:
  - stdio: `printermonitor-mcp` (PM_URL variable if the app is not on localhost:8000);
  - network: the app serves it itself on http://<ip>:8000/mcp/ (Streamable HTTP).
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError  # the only error type whose message is shown to Claude
from mcp.types import ToolAnnotations

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
ACT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DANGER = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

STATE_LABELS = {
    "offline": "offline", "idle": "ready", "printing": "printing", "paused": "paused",
    "completed": "completed", "stopped": "stopped", "error": "error", "processing": "preparing",
    "self_test": "self-test",
}
BUSY = {"printing", "paused", "processing", "self_test"}
MODEL_EXT = {".stl", ".3mf", ".obj", ".step", ".stp"}

INSTRUCTIONS = """\
Control of the user's Creality 3D printers (Creality Hi, K1, K2) on their local network.
- Start with `list_printers` to learn which printers exist; refer to them by name.
- `camera_snapshot` returns a photo: use it to check a print (spaghetti, detachment,
  empty bed before starting a print) and describe what you see.
- Before starting a print, make sure the bed is clear (photo) and the printer is idle.
- Physical actions (printing, stopping, heating, moving axes, smart plug) have real effects:
  confirm with the user when in doubt, and never stop a print unless they ask.
- Temperatures in °C, durations in hours/minutes, costs in the configured currency. Reply in the user's language.
"""


class Client:
    """Access to the app's API."""

    def __init__(self, base_url: str | None = None):
        self.base = (base_url or os.environ.get("PM_URL", "http://127.0.0.1:8000")).rstrip("/")
        password = os.environ.get("PM_PASSWORD", "")
        self.auth = ("mcp", password) if password else None

    def _client(self, timeout: float = 30) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, auth=self.auth, timeout=timeout)

    async def request(self, method: str, path: str, timeout: float = 30, **kw: Any) -> Any:
        try:
            async with self._client(timeout) as c:
                r = await c.request(method, path, **kw)
        except httpx.ConnectError:
            raise ToolError(f"Printer Monitor unreachable at {self.base}: is the app running (uv run printermonitor)?")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise ToolError(str(detail or f"error {r.status_code}"))
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.content

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def printers(self) -> list[dict[str, Any]]:
        return await self.get("/api/printers")

    async def find(self, printer: str) -> dict[str, Any]:
        """Printer by id, name, IP address or unambiguous part of its name."""
        items = await self.printers()
        q = printer.strip().casefold()
        for p in items:
            if q in (p["id"].casefold(), p["name"].casefold(), p["host"].casefold()):
                return p
        partial = [p for p in items if q in p["name"].casefold()]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(f'"{p["name"]}"' for p in items) or "none"
        if partial:
            raise ToolError(f'"{printer}" is ambiguous: {", ".join(p["name"] for p in partial)}')
        raise ToolError(f'printer "{printer}" not found. Printers: {names}')

    async def command(self, p: dict[str, Any], params: dict[str, Any]) -> None:
        await self.post(f"/api/printers/{p['id']}/command", json={"params": params})

    async def wait_job(self, job_id: str, timeout: float = 900) -> dict[str, Any]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = await self.get(f"/api/jobs/{job_id}")
            if job["step"] in ("done", "error"):
                return job
            await asyncio.sleep(1)
        raise ToolError("timed out")


# ---------- formatting ----------
def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _dur(seconds: Any) -> str:
    s = _num(seconds)
    if s is None or s < 0:
        return "—"
    h, m = int(s // 3600), int(s % 3600 // 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _clock(seconds: Any) -> str | None:
    s = _num(seconds)
    if not s or s <= 0:
        return None
    end = datetime.now() + timedelta(seconds=s)
    return end.strftime("%H:%M") + ("" if end.date() == datetime.now().date() else " (tomorrow)")


def _filaments(d: dict[str, Any]) -> list[str]:
    out = []
    for box in (d.get("boxsInfo") or {}).get("materialBoxs", []):
        for m in box.get("materials", []):
            if m.get("state") == 0 and box.get("type") != 1:
                continue
            color = str(m.get("color") or "")[-6:]
            out.append(f"{m.get('name') or m.get('type')} (#{color}, {m.get('percent', '?')} %)")
    return out


def _job(p: dict[str, Any]) -> str:
    d = p.get("data") or {}
    if p["state"] not in BUSY | {"completed"} or not d.get("printFileName"):
        return ""
    name = Path(str(d["printFileName"])).name
    pct = _num(d.get("printProgress")) or 0
    parts = [f"{name} — {pct:.0f} %"]
    if d.get("TotalLayer"):
        parts.append(f"layer {d.get('layer', 0)}/{d['TotalLayer']}")
    if p["state"] != "completed":
        parts.append(f"elapsed {_dur(d.get('printJobTime'))}, remaining {_dur(d.get('printLeftTime'))}")
        if eta := _clock(d.get("printLeftTime")):
            parts.append(f"done around {eta}")
    return ", ".join(parts)


def _summary(p: dict[str, Any]) -> str:
    d = p.get("data") or {}
    line = f"- **{p['name']}** ({p.get('model') or 'unknown model'}, {p['host']}): {STATE_LABELS.get(p['state'], p['state'])}"
    if job := _job(p):
        line += f" — {job}"
    if p.get("error"):
        line += f" — ⚠ {p['error']['title']}"
    if p.get("online"):
        line += f" — nozzle {_num(d.get('nozzleTemp')) or 0:.0f} °C, bed {_num(d.get('bedTemp0')) or 0:.0f} °C"
    if p.get("maintenance_due"):
        line += f" — {p['maintenance_due']} maintenance task(s) due"
    return line


def build(client: Client | None = None) -> MCPServer:
    api = client or Client()
    mcp = MCPServer("creality-printers", title="Creality 3D Printers (Printer Monitor)", instructions=INSTRUCTIONS)

    # ---------- status ----------
    @mcp.tool(annotations=READ, structured_output=False)
    async def list_printers() -> str:
        """List the printers with their status: current print (progress, finish time), temperatures, problems."""
        items = await api.printers()
        if not items:
            return "No printer configured. Use `discover_printers`, then `add_printer`."
        return "\n".join(_summary(p) for p in items)

    @mcp.tool(annotations=READ, structured_output=False)
    async def printer_status(printer: str) -> str:
        """Detailed status of a printer: current job, error and advice, temperatures and targets,
        fans, speed, loaded filament, position, light, camera.

        Args:
            printer: printer name, id or IP address.
        """
        p = await api.find(printer)
        d = p.get("data") or {}
        lines = [f"# {p['name']}", f"Model: {p.get('model') or '?'} · address {p['host']} · state: {STATE_LABELS.get(p['state'], p['state'])}"]
        if job := _job(p):
            lines.append(f"Print: {job}")
        if e := p.get("error"):
            lines.append(f"⚠ Problem: {e['title']} — {e['advice']} (code {e['code']})")
        if p.get("online"):
            lines += [
                f"Nozzle: {_num(d.get('nozzleTemp')) or 0:.0f} °C (target {_num(d.get('targetNozzleTemp')) or 0:.0f} °C)",
                f"Bed: {_num(d.get('bedTemp0')) or 0:.0f} °C (target {_num(d.get('targetBedTemp0')) or 0:.0f} °C)",
                f"Fans: part {d.get('modelFanPct', '?')} %, case {d.get('caseFanPct', '?')} %, auxiliary {d.get('auxiliaryFanPct', '?')} %",
                f"Speed: {d.get('curFeedratePct', '?')} %",
                f"Light: {'on' if str(d.get('lightSw')) == '1' else 'off'}",
                f"Position: {d.get('curPosition', '?')}",
                f"Filament: {', '.join(_filaments(d)) or 'unknown'}",
                f"Camera: {(p.get('camera_status') or {}).get('status', '?')}",
            ]
            if p["state"] == "printing":
                lines.append(f"Actual speed: {_num(d.get('realTimeSpeed')) or 0:.0f} mm/s, flow {_num(d.get('realTimeFlow')) or 0:.1f} mm³/s")
        if p.get("maintenance_due"):
            lines.append(f"Maintenance: {p['maintenance_due']} task(s) due (see `maintenance`)")
        if p.get("plug_pending"):
            lines.append(f"Smart plug: {p['plug_pending']}")
        return "\n".join(lines)

    @mcp.tool(annotations=READ, structured_output=False)
    async def camera_snapshot(printer: str) -> list[Any]:
        """Current photo from the printer's camera (to check a print, or that the bed is clear).

        Args:
            printer: printer name, id or IP address.
        """
        p = await api.find(printer)
        if not p.get("online"):
            raise ToolError(f"{p['name']} is offline")
        jpeg = await api.get(f"/api/printers/{p['id']}/snapshot", timeout=40)
        caption = f"{p['name']} camera at {datetime.now():%H:%M:%S} — {STATE_LABELS.get(p['state'], p['state'])}"
        if job := _job(p):
            caption += f" — {job}"
        return [Image(data=jpeg, format="jpeg"), caption]

    @mcp.tool(annotations=READ, structured_output=False)
    async def list_files(printer: str, search: str = "") -> str:
        """G-code files stored on the printer, with estimated time and filament.

        Args:
            printer: printer name, id or IP address.
            search: optional filter on the file name.
        """
        p = await api.find(printer)
        files = await api.get(f"/api/printers/{p['id']}/files")
        files = [f for f in files if search.casefold() in f["name"].casefold()]
        if not files:
            return "No files." if not search else f'No file name contains "{search}".'
        return "\n".join(
            f"- {f['name']} — {_dur(f.get('timeCost'))}" + (f", {str(f['material']).split(';')[0]}" if f.get("material") else "")
            + (f", {f['consumables'] / 1000:.1f} m" if f.get("consumables") else "")
            for f in files
        )

    # ---------- printing ----------
    @mcp.tool(annotations=ACT, structured_output=False)
    async def start_print(printer: str, file: str) -> str:
        """Start printing a file already stored on the printer (see `list_files`).
        Check first that the bed is clear (`camera_snapshot`).

        Args:
            printer: printer name, id or IP address.
            file: exact name of the .gcode file on the printer.
        """
        p = await api.find(printer)
        if p["state"] in BUSY:
            raise ToolError(f"{p['name']} is busy ({STATE_LABELS[p['state']]})")
        if not p.get("online"):
            raise ToolError(f"{p['name']} is offline")
        await api.post(f"/api/printers/{p['id']}/print", json={"name": file})
        return f"Started printing {file} on {p['name']}."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def pause_print(printer: str) -> str:
        """Pause the current print.

        Args:
            printer: printer name, id or IP address.
        """
        p = await api.find(printer)
        if p["state"] != "printing":
            raise ToolError(f"{p['name']} is not printing ({STATE_LABELS.get(p['state'], p['state'])})")
        await api.command(p, {"pause": 1})
        return f"Pause requested on {p['name']}."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def resume_print(printer: str) -> str:
        """Resume a paused print (or one interrupted by a power loss).

        Args:
            printer: printer name, id or IP address.
        """
        p = await api.find(printer)
        if (p.get("error") or {}).get("resumable"):
            await api.command(p, {"repoPlrStatus": 1})
            return f"Resuming the interrupted print on {p['name']}."
        if p["state"] != "paused":
            raise ToolError(f"{p['name']} is not paused ({STATE_LABELS.get(p['state'], p['state'])})")
        await api.command(p, {"pause": 0})
        return f"Resume requested on {p['name']}."

    @mcp.tool(annotations=DANGER, structured_output=False)
    async def stop_print(printer: str) -> str:
        """Stop the current print for good (it cannot be resumed).
        Only when the user explicitly asks for it.

        Args:
            printer: printer name, id or IP address.
        """
        p = await api.find(printer)
        if p["state"] not in BUSY:
            raise ToolError(f"no print in progress on {p['name']}")
        await api.command(p, {"stop": 1})
        return f"Stop requested on {p['name']}."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def clear_error(printer: str) -> str:
        """Acknowledge the alert shown by the printer (after fixing the problem).

        Args:
            printer: printer name, id or IP address.
        """
        p = await api.find(printer)
        await api.command(p, {"cleanErr": 1})
        return f"Alert acknowledged on {p['name']}."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def send_file(
        printers: list[str],
        path: str = "",
        url: str = "",
        start: bool = False,
        quality: str = "",
        filament: str = "",
    ) -> str:
        """Send a file to one or more printers, and start printing if `start`.
        A .gcode is sent as is; a 3D model (.stl, .3mf, .obj, .step) is sliced first
        with the printer's OrcaSlicer profiles (see `slicer_profiles`).

        Args:
            printers: names of the target printers.
            path: path of the file on the machine running this MCP server.
            url: or direct web address of the file (http/https).
            start: start printing after the upload (check first that the beds are clear).
            quality: process (quality) profile (e.g. "0.20mm Standard @Creality Hi"); default: 0.20 mm Standard.
            filament: filament profile (e.g. "Creality Generic PETG @Hi-all"); default: the loaded one, otherwise PLA.
        """
        targets = [await api.find(name) for name in printers]
        if not targets:
            raise ToolError("specify at least one printer")
        name, data = await _read_source(path, url)
        ext = Path(name).suffix.lower()
        if ext not in MODEL_EXT | {".gcode"}:
            raise ToolError("supported formats: .gcode, .stl, .3mf, .obj, .step")
        report = []
        form: dict[str, str] = {"printers": ",".join(p["id"] for p in targets), "start": "true" if start else "false"}
        files = None
        if ext == ".gcode":
            files = {"file": (name, data)}
        else:
            model = targets[0].get("model") or "Creality Hi"
            st = await api.get("/api/slicer", params={"model": model})
            if not st.get("available"):
                raise ToolError("OrcaSlicer is not installed (Settings → Slicing): send a .gcode instead.")
            cat = st["catalog"]
            if not cat.get("machine"):
                raise ToolError(f'no slicing profiles for "{model}"')
            process = _pick(quality, cat["processes"], "0.20mm Standard")
            fil = _pick(filament or _loaded_profile(targets[0], cat["filaments"]), cat["filaments"], "Creality Generic PLA @")
            job = await api.post("/api/slice", files={"file": (name, data)},
                                 data={"machine": cat["machine"], "process": process, "filament": fil}, timeout=60)
            job = await api.wait_job(job["id"])
            if job["step"] == "error":
                raise ToolError(job["error"])
            meta = job.get("meta") or {}
            report.append(f'Sliced with "{process}" / "{fil}": {_dur(meta.get("est_time_s"))}, {meta.get("filament_g", "?")} g.')
            form["token"] = job["token"]
        job = await api.post("/api/send", data=form, files=files, timeout=120)
        job = await api.wait_job(job["id"], timeout=600)
        for t in job.get("targets", {}).values():
            report.append(f"- {t['name']}: {'OK' if t['step'] == 'done' else 'failed — ' + str(t['error'])}")
        verb = "sent and started" if start else "sent"
        head = f"{job.get('gcode', name)} {verb}" if job["step"] == "done" else "Upload incomplete"
        return head + "\n" + "\n".join(report)

    async def _read_source(path: str, url: str) -> tuple[str, bytes]:
        if bool(path) == bool(url):
            raise ToolError("give either `path` or `url`")
        if path:
            f = Path(path).expanduser()
            if not f.is_file():
                raise ToolError(f"file not found: {f}")
            return f.name, f.read_bytes()
        if urlparse(url).scheme not in ("http", "https"):
            raise ToolError("http(s) address expected")
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as c:
            r = await c.get(url)
            r.raise_for_status()
        name = Path(urlparse(str(r.url)).path).name or "model.stl"
        if cd := r.headers.get("content-disposition"):
            if "filename=" in cd:
                name = cd.split("filename=")[-1].strip('"; ')
        return name, r.content

    def _pick(wanted: str, options: list[str], default_prefix: str) -> str:
        if wanted:
            for o in options:
                if o.casefold() == wanted.casefold():
                    return o
            matches = [o for o in options if wanted.casefold() in o.casefold()]
            if matches:
                return matches[0]
            raise ToolError(f'unknown profile "{wanted}"; available: {", ".join(options)}')
        return next((o for o in options if o.startswith(default_prefix)), options[0])

    def _loaded_profile(p: dict[str, Any], options: list[str]) -> str:
        for box in ((p.get("data") or {}).get("boxsInfo") or {}).get("materialBoxs", []):
            for m in box.get("materials", []):
                kind = str(m.get("type") or "").upper()
                for o in options:
                    if o.upper().startswith(f"CREALITY GENERIC {kind} @"):
                        return o
        return ""

    @mcp.tool(annotations=DANGER, structured_output=False)
    async def delete_file(printer: str, file: str) -> str:
        """Delete a G-code file from the printer.

        Args:
            printer: printer name, id or IP address.
            file: exact file name.
        """
        p = await api.find(printer)
        await api.post(f"/api/printers/{p['id']}/delete", json={"name": file})
        return f"{file} deleted from {p['name']}."

    @mcp.tool(annotations=READ, structured_output=False)
    async def slicer_profiles(printer: str = "") -> str:
        """Process (quality) and filament profiles available for slicing (official OrcaSlicer profiles).

        Args:
            printer: printer whose profiles to list (default: Creality Hi).
        """
        model = (await api.find(printer)).get("model") if printer else "Creality Hi"
        st = await api.get("/api/slicer", params={"model": model or "Creality Hi"})
        if not st.get("available"):
            return "OrcaSlicer is not installed (Settings → Slicing in the app)."
        c = st["catalog"]
        return f"Machine: {c['machine']}\nProcesses: {', '.join(c['processes'])}\nFilaments: {', '.join(c['filaments'])}"

    # ---------- direct controls ----------
    @mcp.tool(annotations=ACT, structured_output=False)
    async def set_temperatures(printer: str, nozzle: float | None = None, bed: float | None = None) -> str:
        """Set the nozzle and/or bed target temperature (0 to turn heating off).

        Args:
            printer: printer name, id or IP address.
            nozzle: nozzle target in °C (0–300).
            bed: bed target in °C (0–110).
        """
        p = await api.find(printer)
        params: dict[str, Any] = {}
        if nozzle is not None:
            if not 0 <= nozzle <= 300:
                raise ToolError("nozzle: between 0 and 300 °C")
            params["nozzleTempControl"] = round(nozzle)
        if bed is not None:
            if not 0 <= bed <= 110:
                raise ToolError("bed: between 0 and 110 °C")
            params["bedTempControl"] = {"num": 0, "val": round(bed)}
        if not params:
            raise ToolError("specify `nozzle` and/or `bed`")
        await api.command(p, params)
        return f"Target sent to {p['name']}" + (f": nozzle {nozzle:.0f} °C" if nozzle is not None else "") + (f", bed {bed:.0f} °C" if bed is not None else "") + "."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def set_light(printer: str, on: bool) -> str:
        """Turn the printer light on or off.

        Args:
            printer: printer name, id or IP address.
            on: true to turn on.
        """
        p = await api.find(printer)
        await api.command(p, {"lightSw": 1 if on else 0})
        return f"{p['name']} light turned {'on' if on else 'off'}."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def set_fan(printer: str, fan: Literal["part", "case", "auxiliary"], percent: int) -> str:
        """Set a fan speed.

        Args:
            printer: printer name, id or IP address.
            fan: "part" (part cooling fan), "case" (chamber fan) or "auxiliary" (side fan).
            percent: 0 to 100.
        """
        p = await api.find(printer)
        channel = {"part": 0, "case": 1, "auxiliary": 2}[fan]
        pct = max(0, min(100, int(percent)))
        await api.command(p, {"gcodeCmd": f"M106 P{channel} S{round(255 * pct / 100)}"})
        return f"{p['name']} {fan} fan set to {pct} %."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def set_speed(printer: str, percent: int) -> str:
        """Change the speed of the current print (100 = normal).

        Args:
            printer: printer name, id or IP address.
            percent: 50 to 200.
        """
        p = await api.find(printer)
        pct = max(50, min(200, int(percent)))
        await api.command(p, {"setFeedratePct": pct})
        return f"{p['name']} speed set to {pct} %."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def home_axes(printer: str, axes: str = "XYZ") -> str:
        """Home the axes (refused while printing).

        Args:
            printer: printer name, id or IP address.
            axes: axes to home, e.g. "XY", "Z" or "XYZ".
        """
        p = await api.find(printer)
        if p["state"] in BUSY:
            raise ToolError(f"{p['name']} is printing")
        chosen = " ".join(a for a in "XYZ" if a in axes.upper())
        if not chosen:
            raise ToolError("axes: X, Y and/or Z")
        await api.command(p, {"autohome": chosen})
        return f"Homing ({chosen}) started on {p['name']}."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def move_axis(printer: str, axis: Literal["X", "Y", "Z"], distance_mm: float) -> str:
        """Move an axis by a relative distance (refused while printing).

        Args:
            printer: printer name, id or IP address.
            axis: X, Y or Z.
            distance_mm: distance in mm, negative to move backwards (max ±100 mm, ±20 mm for Z).
        """
        p = await api.find(printer)
        if p["state"] in BUSY:
            raise ToolError(f"{p['name']} is printing")
        limit = 20 if axis == "Z" else 100
        if abs(distance_mm) > limit:
            raise ToolError(f"at most ±{limit} mm on {axis}")
        feed = 600 if axis == "Z" else 6000
        await api.command(p, {"gcodeCmd": f"G91\nG1 {axis}{distance_mm:g} F{feed}\nG90"})
        return f"{axis} moved by {distance_mm:g} mm on {p['name']}."

    @mcp.tool(annotations=ACT, structured_output=False)
    async def send_gcode(printer: str, command: str) -> str:
        """Send a raw G-code command (advanced users; prefer the dedicated tools).

        Args:
            printer: printer name, id or IP address.
            command: G-code command(s), one per line.
        """
        p = await api.find(printer)
        await api.command(p, {"gcodeCmd": command})
        return f"Sent to {p['name']}: {command}"

    # ---------- history, costs, maintenance ----------
    @mcp.tool(annotations=READ, structured_output=False)
    async def print_history(printer: str = "", limit: int = 15) -> str:
        """Recent prints (status, duration, filament, cost in the configured currency), for all printers or one.

        Args:
            printer: filter on one printer (optional).
            limit: number of rows (max 100).
        """
        params: dict[str, Any] = {"limit": max(1, min(100, limit))}
        if printer:
            params["printer"] = (await api.find(printer))["id"]
        rows = await api.get("/api/history", params=params)
        if not rows:
            return "No prints recorded."
        status = {"completed": "completed", "failed": "failed", "stopped": "stopped", "running": "in progress"}
        out = []
        for r in rows:
            cost = (r.get("cost_filament") or 0) + (r.get("cost_energy") or 0)
            line = f"- {datetime.fromtimestamp(r['started_at']):%Y-%m-%d %H:%M} · {r['printer_name']} · {r['file']} · {status.get(r['status'], r['status'])}"
            if r["status"] != "running":
                line += f" · {_dur(r.get('duration_s'))} · {(r.get('filament_g') or 0):.0f} g · cost {cost:.2f}"
            if r.get("error_title"):
                line += f" · {r['error_title']}"
            out.append(line)
        return "\n".join(out)

    @mcp.tool(annotations=READ, structured_output=False)
    async def print_stats() -> str:
        """Statistics: per month (prints, success rate, hours, filament, cost in the configured currency) and per printer."""
        st = await api.get("/api/stats")
        lines = ["Per month:"]
        for m in st["months"]:
            rate = round(100 * (m["completed"] or 0) / m["prints"]) if m["prints"] else 0
            lines.append(f"- {m['month']}: {m['prints']} prints ({rate} % successful), {m['seconds'] / 3600:.1f} h, {m['grams'] / 1000:.2f} kg, cost {m['cost']:.2f}")
        lines.append("Per printer:")
        for p in st["printers"]:
            lines.append(f"- {p['printer_name']}: {p['prints']} prints, {p['seconds'] / 3600:.1f} h, {p['grams'] / 1000:.2f} kg, cost {p['cost']:.2f}")
        return "\n".join(lines) if len(lines) > 2 else "No statistics yet."

    @mcp.tool(annotations=READ, structured_output=False)
    async def maintenance(printer: str) -> str:
        """Printer maintenance: print hours and tasks due.

        Args:
            printer: printer name, id or IP address.
        """
        p = await api.find(printer)
        m = await api.get(f"/api/printers/{p['id']}/maintenance")
        lines = [f"{p['name']}: {m['hours']} print hours tracked."]
        for t in m["tasks"]:
            state = "DUE" if t["due"] else f"in {max(0, round(t['hours'] - t['since_hours']))} h"
            lines.append(f"- [{t['id']}] {t['name']}: {state} ({t['since_hours']} / {t['hours']} h)")
        return "\n".join(lines)

    @mcp.tool(annotations=ACT, structured_output=False)
    async def mark_maintenance_done(printer: str, task: str) -> str:
        """Record that a maintenance task was just done (resets its counter).

        Args:
            printer: printer name, id or IP address.
            task: task id (see `maintenance`, e.g. "bed", "lube", "nozzle").
        """
        p = await api.find(printer)
        await api.post(f"/api/printers/{p['id']}/maintenance/{task}")
        return f'Maintenance "{task}" recorded for {p["name"]}.'

    # ---------- smart plug, network ----------
    @mcp.tool(annotations=DANGER, structured_output=False)
    async def plug_power(printer: str, on: bool) -> str:
        """Turn the printer's smart plug on or off (refused while printing).

        Args:
            printer: printer name, id or IP address.
            on: true to power on, false to cut the power.
        """
        p = await api.find(printer)
        await api.post(f"/api/printers/{p['id']}/plug", json={"on": on})
        return f"{p['name']} smart plug turned {'on' if on else 'off'}."

    @mcp.tool(annotations=READ, structured_output=False)
    async def discover_printers() -> str:
        """Search the local network for Creality printers (including ones not added yet)."""
        found = await api.get("/api/discover", timeout=60)
        if not found:
            return "No Creality printer found on the network."
        return "\n".join(
            f"- {f['host']}: {f.get('model_name') or 'Creality'} (MAC …{(f.get('mac') or '????')[-4:]}) — "
            + (f'already added as "{f["known_name"]}"' if f.get("known_id") else "not added yet")
            for f in found
        )

    @mcp.tool(annotations=ACT, structured_output=False)
    async def add_printer(host: str, name: str = "") -> str:
        """Add a printer to the app (address found with `discover_printers`).

        Args:
            host: printer IP address.
            name: display name (optional).
        """
        p = await api.post("/api/printers", json={"name": name or host, "host": host})
        return f'"{p["name"]}" added ({p["host"]}).'

    return mcp


def main() -> None:
    """stdio entry point (Claude Desktop / Claude Code on this machine)."""
    build().run("stdio")
