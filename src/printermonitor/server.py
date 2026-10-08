from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import discovery, plugs, slicer
from .camera import BOUNDARY, CameraRelay
from .go2rtc import go2rtc
from .history import Tracker, parse_gcode_meta
from .printer import Printer, derive_state, safe_gcode_name
from .paths import CONFIG
from .store import Store
from . import mcp_server
from mcp.server.transport_security import TransportSecuritySettings

log = logging.getLogger("printermonitor")

STATIC = Path(__file__).parent / "static"
PASSWORD = os.environ.get("PM_PASSWORD", "")

printers: dict[str, Printer] = {}
cameras: dict[str, CameraRelay] = {}
clients: set[WebSocket] = set()
jobs: dict[str, dict[str, Any]] = {}
thumbs: dict[str, bytes | None] = {}  # thumbnail cache (printer URL -> PNG, None = missing)


# ---------- broadcast to browsers ----------
def _broadcast(msg: dict[str, Any]) -> None:
    text = json.dumps(msg, default=str)
    for ws in list(clients):
        asyncio.create_task(_safe_send(ws, text))


async def _safe_send(ws: WebSocket, text: str) -> None:
    try:
        await ws.send_text(text)
    except Exception:
        clients.discard(ws)


store: Store | None = None
tracker: Tracker | None = None
autooff: plugs.AutoOff | None = None
_maint_cache: dict[str, tuple[float, int]] = {}


def maintenance_due(pid: str) -> int:
    """Number of maintenance tasks due (cached for one minute)."""
    t, n = _maint_cache.get(pid, (0.0, 0))
    if store and time.monotonic() - t > 60:
        n = sum(1 for task in store.maintenance(pid) if task["due"])
        _maint_cache[pid] = (time.monotonic(), n)
    return n


def snapshot(p: Printer) -> dict[str, Any]:
    snap = p.snapshot()
    cam = cameras.get(p.id)
    snap["camera_status"] = cam.info() if cam else {"status": "idle", "error": None, "retry_at": None}
    snap["maintenance_due"] = maintenance_due(p.id)
    snap["plug_pending"] = autooff.pending.get(p.id) if autooff else None
    return snap


saved_ids: dict[str, tuple] = {}


def on_printer_update(p: Printer) -> None:
    if (p.model or p.mac) and saved_ids.get(p.id) != (p.model, p.mac):
        saved_ids[p.id] = (p.model, p.mac)
        save_config()
    if tracker:
        try:
            tracker.update(p)
        except Exception:
            log.exception("history: %s", p.name)
    _broadcast({"type": "printer", "printer": snapshot(p)})


def get_camera(p: Printer) -> CameraRelay:
    if p.id not in cameras:
        cameras[p.id] = CameraRelay(p, on_change=lambda: on_printer_update(p))
    return cameras[p.id]


def set_job(job_id: str, **fields: Any) -> None:
    jobs[job_id].update(fields)
    _broadcast({"type": "job", "job": jobs[job_id]})


# ---------- configuration ----------
def load_config() -> list[dict[str, str]]:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return []


def save_config() -> None:
    # The model is used to show the right picture even when the printer is off; the MAC,
    # to find it again if the router gives it another IP address.
    data = [
        {"id": p.id, "name": p.name, "host": p.host, **({"model": p.model} if p.model else {}), **({"mac": p.mac} if p.mac else {})}
        for p in printers.values()
    ]
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def add_printer(pid: str, name: str, host: str, model: str | None = None, mac: str | None = None) -> Printer:
    p = Printer(pid, name, host, on_printer_update)
    p.model, p.mac = model, mac
    printers[pid] = p
    p.start()
    return p


def get_printer(pid: str) -> Printer:
    if pid not in printers:
        raise HTTPException(404, "unknown printer")
    return printers[pid]


async def take_photo(p: Printer) -> bytes | None:
    """Camera photo (end of print)."""
    if p.camera_mode() == "webrtc":
        return await get_camera(p).snapshot(timeout=20)
    async with httpx.AsyncClient(timeout=8) as c:
        r = await c.get(p.snapshot_url())
        return r.content if r.status_code == 200 else None


def on_print_finished(p: Printer, record: dict[str, Any]) -> None:
    _maint_cache.pop(p.id, None)
    _broadcast({"type": "history", "printer": p.id, "status": record.get("status")})
    if record.get("status") == "completed" and autooff:
        autooff.schedule(p.id)


def printer_temp_state(pid: str) -> tuple[str, float | None]:
    p = printers.get(pid)
    if not p:
        return "offline", None
    try:
        nozzle = float(p.data.get("nozzleTemp"))
    except (TypeError, ValueError):
        nozzle = None
    return derive_state(p.data, p.online), nozzle


# ---------- MCP server (Claude) on /mcp/ ----------
def _mcp_allowed_hosts() -> list[str]:
    """Host names we accept requests for (protection against DNS rebinding)."""
    import socket

    hosts = {"localhost", "127.0.0.1", socket.gethostname()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            hosts.add(info[4][0])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))
            hosts.add(sock.getsockname()[0])
    except OSError:
        pass
    hosts |= {h.strip() for h in os.environ.get("PM_MCP_HOSTS", "").split(",") if h.strip()}
    return sorted(f"{h}:*" for h in hosts) + sorted(hosts)


mcp = mcp_server.build(mcp_server.Client(f"http://127.0.0.1:{os.environ.get('PM_PORT', '8000')}"))
mcp_http = mcp.streamable_http_app(
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(allowed_hosts=_mcp_allowed_hosts(), allowed_origins=[]),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store, tracker, autooff
    async with mcp.session_manager.run():
        async with _lifespan_inner():
            yield


@asynccontextmanager
async def _lifespan_inner():
    global store, tracker, autooff
    store = Store()
    tracker = Tracker(store, photo=take_photo, on_finish=on_print_finished)
    autooff = plugs.AutoOff(store.settings, printer_temp_state)
    config = load_config()
    for c in config:
        add_printer(c["id"], c["name"], c["host"], c.get("model"), c.get("mac"))
    if not config and not CONFIG.exists():
        asyncio.create_task(first_run_discovery())
    relocator = asyncio.create_task(relocate_loop())
    yield
    relocator.cancel()
    await asyncio.gather(*(c.close() for c in cameras.values()))
    await go2rtc.stop()
    await asyncio.gather(*(p.stop() for p in printers.values()))


app = FastAPI(title="Printer Monitor", lifespan=lifespan)


# ---------- optional password (access from outside the local network) ----------
def _authorized(header: str | None) -> bool:
    if not PASSWORD:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(pw, PASSWORD)


@app.middleware("http")
async def mcp_no_slash(request: Request, call_next):
    """`/mcp` and `/mcp/`: some clients (including `claude mcp add`) strip the trailing slash,
    and not all of them follow a redirect on a POST."""
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not _authorized(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="printermonitor"'})
    return await call_next(request)


# ---------- API ----------
class PrinterIn(BaseModel):
    name: str
    host: str


class PrinterEdit(BaseModel):
    name: str | None = None
    host: str | None = None


class CommandIn(BaseModel):
    params: dict[str, Any]


class FileIn(BaseModel):
    name: str


@app.get("/api/printers")
async def api_printers():
    return [snapshot(p) for p in printers.values()]


@app.post("/api/printers")
async def api_add_printer(body: PrinterIn):
    host = body.host.strip().removeprefix("http://").rstrip("/")
    if not host:
        raise HTTPException(400, "missing IP address")
    p = add_printer(uuid.uuid4().hex[:8], body.name.strip() or host, host)
    save_config()
    on_printer_update(p)
    return snapshot(p)


@app.patch("/api/printers/{pid}")
async def api_edit_printer(pid: str, body: PrinterEdit):
    p = get_printer(pid)
    if body.name is not None and body.name.strip():
        p.name = body.name.strip()
    if body.host is not None:
        host = body.host.strip().removeprefix("http://").rstrip("/")
        if not host:
            raise HTTPException(400, "missing IP address")
        if host != p.host:
            if cam := cameras.pop(pid, None):
                await cam.close()
            p.set_host(host)
    save_config()
    on_printer_update(p)
    return snapshot(p)


@app.get("/api/discover")
async def api_discover():
    """Creality printers found on the network, with the matching printer from the list."""
    found = await discovery.scan()
    by_mac = {p.mac: p for p in printers.values() if p.mac}
    by_host = {p.host: p for p in printers.values()}
    for f in found:
        known = by_mac.get(f["mac"]) or by_host.get(f["host"])
        f["known_id"] = known.id if known else None
        f["known_name"] = known.name if known else None
    return found


async def first_run_discovery() -> None:
    """First launch: add every Creality printer found on the local network."""
    try:
        found = await discovery.scan()
    except Exception as e:
        log.warning("network scan failed: %s", e)
        found = []
    for f in found:
        if any(p.host == f["host"] or (f["mac"] and p.mac == f["mac"]) for p in printers.values()):
            continue
        name = f["model_name"] or "Creality"
        if f["mac"]:
            name += f" {f['mac'][-4:]}"
        p = add_printer(uuid.uuid4().hex[:8], name, f["host"], f["model"], f["mac"])
        on_printer_update(p)
    save_config()  # also written when empty, so the scan only runs once
    log.info("first launch: %d printer(s) added", len(found))


RELOCATE_EVERY = 120  # s


async def relocate_loop() -> None:
    """An offline printer whose MAC we know may simply be at another address."""
    while True:
        await asyncio.sleep(RELOCATE_EVERY)
        lost = {p.mac: p for p in printers.values() if not p.online and p.mac}
        if not lost:
            continue
        try:
            found = await discovery.scan()
        except Exception as e:
            log.warning("network scan failed: %s", e)
            continue
        for f in found:
            p = lost.get(f["mac"])
            if p and f["host"] != p.host:
                log.info("%s changed address: %s -> %s", p.name, p.host, f["host"])
                if cam := cameras.pop(p.id, None):
                    await cam.close()
                p.set_host(f["host"])
                save_config()
                on_printer_update(p)


@app.delete("/api/printers/{pid}")
async def api_remove_printer(pid: str):
    p = get_printer(pid)
    await p.stop()
    if cam := cameras.pop(pid, None):
        await cam.close()
    await go2rtc.remove_stream(pid)
    del printers[pid]
    save_config()
    _broadcast({"type": "removed", "id": pid})
    return {"ok": True}


@app.post("/api/printers/{pid}/command")
async def api_command(pid: str, body: CommandIn):
    try:
        await get_printer(pid).set(body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@app.get("/api/printers/{pid}/files")
async def api_files(pid: str):
    try:
        files = await get_printer(pid).list_files()
    except (RuntimeError, asyncio.TimeoutError) as e:
        raise HTTPException(503, f"file list unavailable: {e or 'timed out'}")
    keep = ("name", "file_size", "create_time", "timeCost", "consumables", "material", "floorHeight")
    files = [{k: f.get(k) for k in keep} for f in files]
    return sorted(files, key=lambda f: f.get("create_time") or 0, reverse=True)


@app.post("/api/printers/{pid}/print")
async def api_print(pid: str, body: FileIn):
    try:
        await get_printer(pid).print_file(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@app.post("/api/printers/{pid}/delete")
async def api_delete(pid: str, body: FileIn):
    try:
        await get_printer(pid).delete_file(body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    return {"ok": True}


@app.get("/api/printers/{pid}/camera")
async def api_camera(pid: str, request: Request):
    """Camera MJPEG stream, served by this server alone (also works remotely)."""
    p = get_printer(pid)
    if p.camera_mode() == "webrtc":
        cam = get_camera(p)
        return StreamingResponse(
            cam.stream(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store"},
        )
    client = httpx.AsyncClient(timeout=httpx.Timeout(10, read=30))
    try:
        upstream = await client.send(client.build_request("GET", p.camera_url()), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(502, "camera unreachable")

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type", "multipart/x-mixed-replace"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/printers/{pid}/snapshot")
async def api_snapshot(pid: str):
    """A single frame: for the grid thumbnails (no long-lived connection)."""
    p = get_printer(pid)
    if p.camera_mode() == "webrtc":
        jpeg = await get_camera(p).snapshot()
    else:
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(p.snapshot_url())
                r.raise_for_status()
                jpeg = r.content
        except httpx.HTTPError:
            jpeg = None
    if not jpeg:
        raise HTTPException(503, "camera unavailable")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/printers/{pid}/camera/reset")
async def api_camera_reset(pid: str):
    p = get_printer(pid)
    if p.camera_mode() == "webrtc":
        try:
            await get_camera(p).reset()
        except RuntimeError as e:
            raise HTTPException(429, str(e))
    on_printer_update(p)
    return {"ok": True}


@app.post("/api/printers/{pid}/reboot")
async def api_reboot(pid: str):
    p = get_printer(pid)
    try:
        await p.reboot()
    except ValueError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    if cam := cameras.get(pid):
        await cam.close()  # the printer is restarting: no point keeping the session
    return {"ok": True}


@app.get("/api/printers/{pid}/thumb")
async def api_thumb(pid: str, name: str):
    """G-code thumbnail, relayed from the printer and cached."""
    p = get_printer(pid)
    try:
        url = p.thumbnail_url(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if url not in thumbs:
        try:
            async with httpx.AsyncClient(timeout=6) as c:
                r = await c.get(url)
            png = r.content if r.status_code == 200 and r.content[:4] == b"\x89PNG" else None
        except httpx.HTTPError:
            raise HTTPException(502, "printer unreachable")
        if len(thumbs) > 500:
            thumbs.clear()
        thumbs[url] = png
    if not thumbs[url]:
        raise HTTPException(404, "no thumbnail")
    return Response(thumbs[url], media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "unknown job")
    return jobs[job_id]


@app.get("/api/printers/{pid}/temps")
async def api_temps(pid: str):
    """Temperature history (last 30 minutes, one sample every 5 s)."""
    return [list(t) for t in get_printer(pid).temps]


# ---------- history, statistics ----------
@app.get("/api/history")
async def api_history(printer: str | None = None, limit: int = 100, offset: int = 0):
    return store.prints(printer, min(limit, 500), offset)


@app.delete("/api/history/{rec_id}")
async def api_history_delete(rec_id: int):
    store.delete_print(rec_id)
    return {"ok": True}


@app.get("/api/history/media/{name}")
async def api_history_media(name: str):
    if not re.fullmatch(r"\d+_(thumb\.png|photo\.jpg)", name) or not (store.media / name).exists():
        raise HTTPException(404, "not found")
    return FileResponse(store.media / name, headers={"Cache-Control": "max-age=86400"})


@app.get("/api/stats")
async def api_stats():
    return store.stats()


# ---------- settings ----------
@app.get("/api/settings")
async def api_settings():
    return {**store.settings(), "plug_types": plugs.TYPES}


@app.patch("/api/settings")
async def api_settings_update(patch: dict[str, Any]):
    try:
        settings = store.update_settings(patch)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if {"filaments", "energy", "default_filament"} & patch.keys():
        tracker.recompute_costs()
    _maint_cache.clear()
    return {**settings, "plug_types": plugs.TYPES}


# ---------- maintenance ----------
@app.get("/api/printers/{pid}/maintenance")
async def api_maintenance(pid: str):
    get_printer(pid)
    return {"hours": round(store.print_hours(pid), 1), "tasks": store.maintenance(pid)}


@app.post("/api/printers/{pid}/maintenance/{task_id}")
async def api_maintenance_done(pid: str, task_id: str):
    p = get_printer(pid)
    store.mark_done(pid, task_id)
    _maint_cache.pop(pid, None)
    on_printer_update(p)
    return {"hours": round(store.print_hours(pid), 1), "tasks": store.maintenance(pid)}


# ---------- smart plug ----------
class PlugIn(BaseModel):
    on: bool


@app.get("/api/printers/{pid}/plug")
async def api_plug(pid: str):
    get_printer(pid)
    cfg = store.settings()["plugs"].get(pid)
    if not cfg or not cfg.get("host"):
        return {"configured": False}
    try:
        on = await plugs.get_state(cfg)
    except Exception as e:
        return {"configured": True, "reachable": False, "error": str(e) or type(e).__name__, **cfg}
    return {"configured": True, "reachable": True, "on": on, "pending": autooff.pending.get(pid), **cfg}


@app.post("/api/printers/{pid}/plug")
async def api_plug_set(pid: str, body: PlugIn):
    p = get_printer(pid)
    cfg = store.settings()["plugs"].get(pid)
    if not cfg or not cfg.get("host"):
        raise HTTPException(400, "no smart plug configured for this printer")
    if not body.on and derive_state(p.data, p.online) in plugs.BUSY:
        raise HTTPException(409, "print in progress: the plug will not be switched off")
    autooff.cancel(pid)
    try:
        await plugs.set_state(cfg, body.on)
    except Exception as e:
        raise HTTPException(502, f"smart plug unreachable: {e}")
    return {"ok": True}


# ---------- slicing and sending (one or more printers) ----------
SLICED_DIR_NAME = "sliced"
MODEL_EXT = (".stl", ".3mf", ".obj", ".step", ".stp")


def sliced_dir() -> Path:
    d = store.dir / SLICED_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


@app.get("/api/slicer")
async def api_slicer(model: str | None = None):
    st = slicer.status()
    st["catalog"] = slicer.catalog(model or "Creality Hi") if st["available"] else None
    return st


@app.post("/api/slicer/install")
async def api_slicer_install():
    asyncio.create_task(slicer.install(lambda: _broadcast({"type": "slicer", "install": slicer.install_state})))
    return slicer.install_state


@app.post("/api/slice")
async def api_slice(
    file: UploadFile = File(...),
    machine: str = Form(...),
    process: str = Form(...),
    filament: str = Form(...),
    thumb300: str = Form(""),
    thumb96: str = Form(""),
):
    """Slice a 3D model; the G-code stays on the server for the preview and the upload."""
    filename = Path(file.filename or "model.stl").name
    if Path(filename).suffix.lower() not in MODEL_EXT:
        raise HTTPException(400, "supported formats: .stl, .3mf, .obj, .step")
    if not slicer.find_binary():
        raise HTTPException(400, "OrcaSlicer is not installed (Settings → Slicing).")
    workdir = slicer.new_workdir()
    src = workdir / re.sub(r"[^\w.\-()+]", "_", filename)
    with src.open("wb") as fh:
        shutil.copyfileobj(file.file, fh)
    thumbs = []
    for b64 in (thumb300, thumb96):
        if b64:
            try:
                png = base64.b64decode(b64.split(",", 1)[-1])
                if png[:8] == b"\x89PNG\r\n\x1a\n":
                    thumbs.append(png)
            except ValueError:
                pass
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"id": job_id, "kind": "slice", "file": filename, "step": "slicing", "error": None}
    asyncio.create_task(_slice_job(job_id, src, machine, process, filament, thumbs, workdir))
    return jobs[job_id]


async def _slice_job(job_id: str, src: Path, machine: str, process: str, filament: str, thumbs: list[bytes], workdir: Path) -> None:
    try:
        gcode = await slicer.slice_model(src, machine, process, filament, workdir)
        if thumbs:
            slicer.inject_thumbnails(gcode, thumbs)
        token = uuid.uuid4().hex
        name = safe_gcode_name(src.stem + ".gcode")
        target = sliced_dir() / f"{token}.gcode"
        shutil.move(gcode, target)
        (sliced_dir() / f"{token}.json").write_text(json.dumps({"name": name}), encoding="utf-8")
        meta = parse_gcode_meta(target.read_text(encoding="utf-8", errors="replace")[-300_000:])
        set_job(job_id, step="done", token=token, gcode=name, meta=meta)
    except Exception as e:
        log.exception("slicing %s", job_id)
        set_job(job_id, step="error", error=str(e) or type(e).__name__)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _cleanup_sliced()


def _cleanup_sliced(max_age: float = 86400) -> None:
    for f in sliced_dir().glob("*"):
        if time.time() - f.stat().st_mtime > max_age:
            f.unlink(missing_ok=True)


@app.get("/api/sliced/{token}")
async def api_sliced(token: str):
    path = sliced_dir() / f"{token}.gcode"
    if not re.fullmatch(r"[0-9a-f]{32}", token) or not path.exists():
        raise HTTPException(404, "sliced file not found (expired?)")
    return FileResponse(path, media_type="text/plain")


@app.post("/api/send")
async def api_send(
    printers_ids: str = Form(..., alias="printers"),
    start: bool = Form(False),
    token: str = Form(""),
    file: UploadFile | None = File(None),
):
    """Send a G-code (uploaded file, or sliced earlier) to one or more printers."""
    targets = [get_printer(pid) for pid in dict.fromkeys(x for x in printers_ids.split(",") if x)]
    if not targets:
        raise HTTPException(400, "select at least one printer")
    workdir = slicer.new_workdir()
    if token:
        src = sliced_dir() / f"{token}.gcode"
        if not re.fullmatch(r"[0-9a-f]{32}", token) or not src.exists():
            raise HTTPException(404, "sliced file not found (expired?)")
        name = json.loads((sliced_dir() / f"{token}.json").read_text(encoding="utf-8"))["name"]
    elif file is not None:
        name = Path(file.filename or "file.gcode").name
        if Path(name).suffix.lower() != ".gcode":
            raise HTTPException(400, "upload a .gcode, or slice the model first")
        src = workdir / "upload.gcode"
        with src.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
    else:
        raise HTTPException(400, "no file")
    try:
        name = safe_gcode_name(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if start:
        busy = [p.name for p in targets if derive_state(p.data, p.online) in plugs.BUSY or not p.online]
        if busy:
            raise HTTPException(409, f"unavailable: {', '.join(busy)}")
    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "id": job_id, "kind": "send", "file": name, "step": "uploading", "error": None, "start": start,
        "targets": {p.id: {"name": p.name, "step": "queued", "error": None} for p in targets},
        "printer": targets[0].id if len(targets) == 1 else None,
    }
    asyncio.create_task(_send_job(job_id, targets, src, name, start, workdir))
    return jobs[job_id]


async def _send_one(job_id: str, p: Printer, src: Path, name: str, start: bool) -> None:
    t = jobs[job_id]["targets"][p.id]
    try:
        t["step"] = "uploading"
        set_job(job_id)
        await p.upload(src, name)
        if start:
            t["step"] = "starting"
            set_job(job_id)
            await asyncio.sleep(1)  # let the printer index the file
            if not p.files:
                try:
                    await p.list_files()  # to find the exact G-code folder
                except Exception:
                    pass
            await p.print_file(name)
        t["step"] = "done"
    except Exception as e:
        log.exception("upload %s -> %s", name, p.name)
        t["step"], t["error"] = "error", str(e) or type(e).__name__


async def _send_job(job_id: str, targets: list[Printer], src: Path, name: str, start: bool, workdir: Path) -> None:
    try:
        await asyncio.gather(*(_send_one(job_id, p, src, name, start) for p in targets))
        errors_ = [f"{t['name']}: {t['error']}" for t in jobs[job_id]["targets"].values() if t["error"]]
        set_job(job_id, step="error" if errors_ else "done", gcode=name, error="\n".join(errors_) or None)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/api/printers/{pid}/upload")
async def api_upload(pid: str, file: UploadFile = File(...), start: bool = Form(False)):
    """Upload a .gcode from a printer's view (shortcut for /api/send)."""
    return await api_send(printers_ids=pid, start=start, token="", file=file)


@app.websocket("/api/ws")
async def ws_endpoint(ws: WebSocket):
    if not _authorized(ws.headers.get("authorization")):
        # Browsers resend the Basic header on the same-origin WebSocket.
        await ws.close(code=4401)
        return
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({"type": "init", "printers": [snapshot(p) for p in printers.values()]}, default=str))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/mcp", mcp_http)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
