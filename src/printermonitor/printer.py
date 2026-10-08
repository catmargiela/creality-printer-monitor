"""Local connection to a Creality OS printer (Hi, K1, K2…).

Protocol (same as Creality Print / OrcaSlicer):
  - WebSocket  ws://<ip>:9999           pushed telemetry + commands {"method": "set"|"get", "params": {...}}
  - HTTP POST  http://<ip>/upload/<name> G-code upload (multipart, "file" field)
  - HTTP GET   http://<ip>/info          printer model
  - camera     K1: MJPEG http://<ip>:8080/?action=stream
               Hi / K2: WebRTC on http://<ip>:8000/call/webrtc_local (through go2rtc, see go2rtc.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import websockets

from . import errors

log = logging.getLogger(__name__)

HTTP_PORT = 80
WS_PORT = 9999
MJPEG_PORT = 8080
MOONRAKER_PORT = 7125
HEARTBEAT_SECS = 10.0
POLL_SECS = 5.0
BOX_POLL_SECS = 60.0  # filament (boxsInfo): never pushed spontaneously by the printer
TEMP_SAMPLE_SECS = 5.0
TEMP_HISTORY = 360  # 30 min at 5 s
# Bulky responses we don't keep in the state broadcast to browsers.
DROP_KEYS = {"retMaterials", "historyList", "elapseVideoList"}

# K1 printers store G-codes in /usr/data, the others (Hi, K2…) in /mnt/UDISK.
K1_MODELS = {"K1", "K1 SE", "K1C", "K1_CFS-C", "K1 Max"}

# Internal codes returned by the firmware -> commercial names.
MODEL_NAMES = {"F018": "Creality Hi", "F008": "K2 Plus", "F012": "K2 Pro", "F021": "K2", "F022": "SPARKX i7"}

# Only these keys may go through the generic command route.
# `opGcodeFile` is deliberately excluded: it can rename or delete any file on
# the printer's system, so it only goes through print_file()/delete_file(),
# which validate the file name.
ALLOWED_SET_KEYS = {
    "pause",
    "stop",
    "lightSw",
    "nozzleTempControl",
    "bedTempControl",
    "setFeedratePct",
    "setFlowratePct",
    "gcodeCmd",
    "autohome",
    "cleanErr",  # acknowledge the displayed error
    "repoPlrStatus",  # after a power loss: 1 = resume, 0 = discard
}

_SAFE_NAME = re.compile(r"^[\w.\-()+ ]+\.gcode$", re.IGNORECASE)


def check_gcode_name(name: str) -> str:
    """Name of a G-code already on the printer (may contain spaces)."""
    if name != Path(name).name or not _SAFE_NAME.match(name):
        raise ValueError(f"invalid file name: {name!r}")
    return name


def safe_gcode_name(name: str) -> str:
    """Name used to upload a new file (spaces -> _, like Creality Print)."""
    return check_gcode_name(Path(name).name.replace(" ", "_"))


def derive_state(d: dict[str, Any], online: bool) -> str:
    """Job state. An error during a print (AI pause, filament runout…) leaves the
    state as "paused": the cause is described separately (errors.describe)."""
    if not online:
        return "offline"
    try:
        if 1 <= float(d.get("withSelfTest") or 0) <= 99:
            return "self_test"
    except (TypeError, ValueError):
        pass
    progress = d.get("printProgress", d.get("dProgress"))
    try:
        progress = float(progress)
        progress = int(progress) if math.isfinite(progress) else -1
    except (TypeError, ValueError):
        progress = -1
    state = d.get("state")
    job = "idle"
    if d.get("printFileName"):
        if progress >= 100 or state == 2:
            job = "completed"
        elif state == 5:
            job = "paused"
        elif state in (3, 4):
            job = "stopped"
        elif state == 1:
            job = "printing"
        elif state == 0:
            job = "processing"
    if job in ("idle", "stopped") and relevant_error(d, job):
        return "error"
    return job


def relevant_error(d: dict[str, Any], job: str) -> dict[str, Any] | None:
    """The firmware does not always reset the error code (e.g. 500 after an AI pause,
    even once the print is finished). So we only keep what makes sense:
      - printing or finished: nothing;
      - paused: the cause of the pause;
      - idle / stopped: hardware faults, not stale AI alerts."""
    if job in ("printing", "processing", "self_test", "completed"):
        return None
    err = errors.describe(d)
    if err and job != "paused" and err["category"] == "ai":
        return None
    return err


class Printer:
    def __init__(self, pid: str, name: str, host: str, on_update: Callable[[Printer], None]):
        self.id = pid
        self.name = name
        self.host = host
        self.data: dict[str, Any] = {}
        self.model: str | None = None
        self.mac: str | None = None  # identifies the printer even if its IP address changes
        self.online = False
        self.last_rx = 0.0
        self.files: list[dict[str, Any]] = []
        self._files_event = asyncio.Event()
        self._ws: websockets.ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._on_update = on_update
        self.temps: deque[tuple[float, float | None, float | None, float | None, float | None]] = deque(maxlen=TEMP_HISTORY)
        self._last_sample = 0.0

    # ---------- lifecycle ----------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"printer-{self.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "model": MODEL_NAMES.get(model := self.model or self.data.get("model"), model),
            "model_code": model,
            "mac": self.mac,
            "online": self.online,
            "camera": self.camera_mode(),
            "state": derive_state(self.data, self.online),
            "error": self.active_error(),
            "data": self.data,
        }

    # ---------- WebSocket ----------
    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._fetch_model()
                async with websockets.connect(
                    f"ws://{self.host}:{WS_PORT}",
                    subprotocols=["wsslicer"],
                    open_timeout=5,
                    ping_interval=None,  # the firmware does not handle WS pings well
                    max_size=8 * 1024 * 1024,  # the file list can be large
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info("%s: connected to %s", self.name, self.host)
                    poller = asyncio.create_task(self._poll())
                    try:
                        async for raw in ws:
                            await self._handle(raw)
                    finally:
                        poller.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network, printer switched off…
                log.debug("%s: %s", self.name, e)
            self._ws = None
            if self.online:
                self.online = False
                self._on_update(self)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.8, 30.0)

    async def _poll(self) -> None:
        """Like the printer's web UI: periodically request the full state again."""
        last_box = 0.0
        while True:
            if time.monotonic() - last_box > BOX_POLL_SECS:
                last_box = time.monotonic()
                try:
                    await self._send({"method": "get", "params": {"boxsInfo": 1}})
                except Exception:
                    pass
                if not self.mac:  # /info may have failed on connect: try again
                    await self._fetch_model()
                    if self.mac:
                        self._on_update(self)
            silence = time.monotonic() - self.last_rx
            if self.last_rx and silence > HEARTBEAT_SECS * 3:
                log.warning("%s: no data for %.0fs, reconnecting", self.name, silence)
                await self._ws.close()
                return
            try:
                await self._send({"method": "get", "params": {"ReqPrinterPara": 1}})
            except Exception:
                pass
            await asyncio.sleep(POLL_SECS)

    async def _handle(self, raw: str | bytes) -> None:
        self.last_rx = time.monotonic()
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        if "heart_beat" in raw:
            # The K1 firmware closes the connection if we don't acknowledge.
            await self._ws.send("ok")
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        files = msg.pop("retGcodeFileInfo2", None) or msg.pop("retGcodeFileInfo", None)
        if isinstance(files, list):
            self.files = files
            self._files_event.set()
        for k in DROP_KEYS & msg.keys():
            del msg[k]
        if msg:
            self.data.update(msg)
            self._sample_temps()
        was_online, self.online = self.online, True
        if msg or not was_online:
            self._on_update(self)

    def active_error(self) -> dict[str, Any] | None:
        """Error to display. The firmware sometimes keeps the old code (e.g. 500, AI pause)
        after resuming: while a print is running normally, we don't show it."""
        if not self.online:
            return None
        state = derive_state(self.data, True)
        return relevant_error(self.data, "idle" if state == "error" else state)

    def set_host(self, host: str) -> None:
        """New address: reconnect right away."""
        if host == self.host:
            return
        self.host = host
        if self._task:
            self._task.cancel()
        self.online = False
        self.start()

    def _sample_temps(self) -> None:
        now = time.time()
        if now - self._last_sample < TEMP_SAMPLE_SECS:
            return
        self._last_sample = now

        def f(k: str) -> float | None:
            try:
                return round(float(self.data[k]), 1)
            except (KeyError, TypeError, ValueError):
                return None

        self.temps.append((round(now), f("nozzleTemp"), f("targetNozzleTemp"), f("bedTemp0"), f("targetBedTemp0")))

    def thumbnail_url(self, name: str) -> str:
        """G-code thumbnail: http://<ip>/downloads/humbnail/<name>.png ("humbnail": sic, Creality's spelling)."""
        stem = Path(check_gcode_name(Path(name).name)).stem
        return f"http://{self.host}:{HTTP_PORT}/downloads/humbnail/{quote(stem)}.png"

    async def _send(self, obj: dict[str, Any]) -> None:
        async with self._send_lock:
            if not self._ws:
                raise RuntimeError("printer not connected")
            await self._ws.send(json.dumps(obj, separators=(",", ":")))

    # ---------- commands ----------
    async def set(self, params: dict[str, Any]) -> None:
        bad = set(params) - ALLOWED_SET_KEYS
        if bad:
            raise ValueError(f"command not allowed: {', '.join(sorted(bad))}")
        await self._send({"method": "set", "params": params})

    async def list_files(self, timeout: float = 8.0) -> list[dict[str, Any]]:
        self._files_event.clear()
        await self._send({"method": "get", "params": {"reqGcodeFile": 1}})
        await asyncio.wait_for(self._files_event.wait(), timeout)
        return self.files

    def gcode_dir(self) -> str:
        # Most reliable: the folder actually used by the files already there.
        for f in self.files:
            p = f.get("path") or ""
            if p.endswith("/" + (f.get("name") or "")) and "/printer_data/gcodes" in p:
                return p[: p.index("/printer_data/gcodes")] + "/printer_data/gcodes"
        root = "/usr/data" if (self.model in K1_MODELS) else "/mnt/UDISK"
        return f"{root}/printer_data/gcodes"

    async def print_file(self, name: str) -> None:
        name = check_gcode_name(name)
        await self._send({"method": "set", "params": {"opGcodeFile": f"printprt:{self.gcode_dir()}/{name}"}})

    async def delete_file(self, name: str) -> None:
        name = check_gcode_name(name)
        await self._send({"method": "set", "params": {"opGcodeFile": f"deleteprt:{self.gcode_dir()}/{name}"}})

    # ---------- HTTP ----------
    async def _fetch_model(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=4) as c:
                r = await c.get(f"http://{self.host}:{HTTP_PORT}/info")
                info = r.json()
                self.model = info.get("model") or self.model
                self.mac = (info.get("mac") or self.mac or "").upper() or None
        except Exception:
            pass

    async def upload(self, path: Path, name: str) -> str:
        name = safe_gcode_name(name)
        size = path.stat().st_size
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, write=600)) as c:
            with path.open("rb") as fh:
                r = await c.post(
                    f"http://{self.host}:{HTTP_PORT}/upload/{quote(name)}",
                    files={"file": (name, fh, "application/octet-stream")},
                )
        r.raise_for_status()
        log.info("%s: %s uploaded (%d bytes): %s", self.name, name, size, r.text[:200])
        return name

    async def reboot(self) -> None:
        """Restart the printer (through Moonraker): the only way to clear its locked camera sessions."""
        state = derive_state(self.data, self.online)
        if state in ("printing", "paused", "processing", "self_test"):
            raise ValueError("cannot restart while printing")
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"http://{self.host}:{MOONRAKER_PORT}/machine/reboot")
        except httpx.HTTPError as e:
            raise RuntimeError(f"the printer did not accept the restart ({type(e).__name__})")
        if r.status_code != 200:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:200]
            if "sudo" in msg:
                # The Hi's system has no sudo: Moonraker cannot restart it.
                raise RuntimeError(
                    "this printer cannot be restarted through Moonraker. "
                    "Enable root access on its touchscreen (Settings → Root account) to allow remote restarts."
                )
            raise RuntimeError(f"restart refused by the printer: {msg}")

    def camera_mode(self) -> str:
        model = self.model or self.data.get("model")
        return "mjpeg" if model in K1_MODELS else "webrtc"

    def camera_url(self) -> str:
        return f"http://{self.host}:{MJPEG_PORT}/?action=stream"

    def snapshot_url(self) -> str:
        return f"http://{self.host}:{MJPEG_PORT}/?action=snapshot"
