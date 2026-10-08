"""go2rtc: WebRTC -> local RTSP gateway for the Hi / K2 cameras.

The Python WebRTC client (aiortc) fails to set up DTLS encryption with the Hi's
camera, whereas go2rtc (also used by Home Assistant) supports it natively
(`#format=creality`). We run it in the background, listening on localhost
only; it only opens the connection to the printer while someone is reading
the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import platform
import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import httpx

from .paths import DATA_DIR

log = logging.getLogger(__name__)

VERSION = "v1.9.14"
# SHA-256 published by GitHub for this release, per (OS, CPU) asset.
DIGESTS = {
    "go2rtc_linux_amd64": "32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6",
    "go2rtc_linux_arm64": "359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50",
    "go2rtc_linux_arm": "4d7e1639af5a2722a28e864468fd8099b3c1682565446c798bf9e3b38fde12e4",
    "go2rtc_mac_amd64.zip": "9b0b9a27a4dc3a5b8b93376e7e8fc2787c6af624a512842622be84aec0171c7a",
    "go2rtc_mac_arm64.zip": "919b78adc759d6b3883d1e1b2ac915ac0985bb903ff1897b4d228527bd64690c",
    "go2rtc_win64.zip": "dd4167d75cb04abe618855b7c71f8658bd009f60c1a71835d134d2c11c939907",
    "go2rtc_win_arm64.zip": "814be0f6d8669025c7bccdd1f026ffaf613abae5352239f4ec84de543b94594a",
}
API_PORT = int(os.environ.get("PM_GO2RTC_API_PORT", "19840"))
RTSP_PORT = int(os.environ.get("PM_GO2RTC_RTSP_PORT", "19554"))
CAMERA_PORT = 8000  # printer's WebRTC signaling


def stream_name(printer_id: str) -> str:
    return f"pm_{printer_id}"


def rtsp_url(printer_id: str) -> str:
    return f"rtsp://127.0.0.1:{RTSP_PORT}/{stream_name(printer_id)}"


def source_url(host: str) -> str:
    return f"webrtc:http://{host}:{CAMERA_PORT}/call/webrtc_local#format=creality"


def release_asset() -> str | None:
    """Name of the go2rtc release file for this OS and CPU (None if unsupported)."""
    cpu = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if cpu is None and platform.machine().lower().startswith("arm"):
        cpu = "arm"
    name = {
        ("linux", "amd64"): "go2rtc_linux_amd64",
        ("linux", "arm64"): "go2rtc_linux_arm64",
        ("linux", "arm"): "go2rtc_linux_arm",
        ("darwin", "amd64"): "go2rtc_mac_amd64.zip",
        ("darwin", "arm64"): "go2rtc_mac_arm64.zip",
        ("win32", "amd64"): "go2rtc_win64.zip",
        ("win32", "arm64"): "go2rtc_win_arm64.zip",
    }.get((sys.platform, cpu or ""))
    return name if name in DIGESTS else None


class Go2rtc:
    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._streams: dict[str, str] = {}  # name -> source

    # ---------- binary ----------
    async def _binary(self) -> Path:
        if env := os.environ.get("GO2RTC"):
            return Path(env)
        if found := shutil.which("go2rtc"):
            return Path(found)
        asset = release_asset()
        if not asset:
            raise RuntimeError("go2rtc not found: install it (https://github.com/AlexxIT/go2rtc) or set GO2RTC")
        exe = "go2rtc.exe" if sys.platform == "win32" else "go2rtc"
        target = DATA_DIR / "bin" / f"go2rtc-{VERSION}" / exe
        if target.exists():
            return target
        url = f"https://github.com/AlexxIT/go2rtc/releases/download/{VERSION}/{asset}"
        log.info("downloading go2rtc %s (%s)…", VERSION, asset)
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as c:
            r = await c.get(url)
            r.raise_for_status()
        if hashlib.sha256(r.content).hexdigest() != DIGESTS[asset]:
            raise RuntimeError("downloaded go2rtc has an invalid checksum, file rejected")
        content = r.content
        if asset.endswith(".zip"):
            with zipfile.ZipFile(BytesIO(content)) as z:
                content = z.read(exe)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".part")
        tmp.write_bytes(content)
        tmp.chmod(0o755)
        tmp.replace(target)
        return target

    # ---------- process ----------
    async def ensure_running(self) -> None:
        async with self._lock:
            if self._proc and self._proc.returncode is None:
                return
            binary = await self._binary()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            config = DATA_DIR / "go2rtc.yaml"
            # Listen on localhost only; incoming WebRTC server disabled.
            config.write_text(
                f'api:\n  listen: "127.0.0.1:{API_PORT}"\n'
                f'rtsp:\n  listen: "127.0.0.1:{RTSP_PORT}"\n'
                'webrtc:\n  listen: ""\n'
                "log:\n  level: warn\n",
                encoding="utf-8",
            )
            self._proc = await asyncio.create_subprocess_exec(
                str(binary), "-config", str(config),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(50):
                with contextlib.suppress(httpx.HTTPError):
                    async with httpx.AsyncClient(timeout=1) as c:
                        await c.get(f"http://127.0.0.1:{API_PORT}/api")
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("go2rtc did not start")
            log.info("go2rtc started (pid %s)", self._proc.pid)
            # After a (re)start, the streams must be declared again.
            streams, self._streams = self._streams, {}
        for name, src in streams.items():
            await self._put(name, src)

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._proc.wait(), 5)

    # ---------- streams ----------
    async def _put(self, name: str, src: str) -> None:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.put(f"http://127.0.0.1:{API_PORT}/api/streams", params={"name": name, "src": src})
            r.raise_for_status()
        self._streams[name] = src

    async def ensure_stream(self, printer_id: str, host: str) -> str:
        await self.ensure_running()
        name, src = stream_name(printer_id), source_url(host)
        if self._streams.get(name) != src:
            await self._put(name, src)
        return rtsp_url(printer_id)

    async def reset_stream(self, printer_id: str, host: str) -> None:
        """Delete then recreate the stream: go2rtc closes its connection to the printer."""
        name = stream_name(printer_id)
        if self._proc and self._proc.returncode is None:
            with contextlib.suppress(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.delete(f"http://127.0.0.1:{API_PORT}/api/streams", params={"src": name})
        self._streams.pop(name, None)
        await self.ensure_stream(printer_id, host)

    async def remove_stream(self, printer_id: str) -> None:
        name = stream_name(printer_id)
        if self._streams.pop(name, None) and self._proc and self._proc.returncode is None:
            with contextlib.suppress(httpx.HTTPError):
                async with httpx.AsyncClient(timeout=5) as c:
                    await c.delete(f"http://127.0.0.1:{API_PORT}/api/streams", params={"src": name})


go2rtc = Go2rtc()
