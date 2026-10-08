"""WebRTC camera (Hi, K2) relayed as MJPEG by the server.

A WebRTC session that is not closed cleanly (reloaded tab, killed process) locks
the printer's camera until the printer restarts. So the server opens ONE session
per printer, through go2rtc, only while someone is watching; it decodes the H264
and serves JPEGs to every browser (plain <img>, works everywhere, including remotely).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from fractions import Fraction
from typing import TYPE_CHECKING, AsyncIterator, Callable

import av

from .go2rtc import go2rtc

if TYPE_CHECKING:
    from .printer import Printer

log = logging.getLogger(__name__)

MAX_FPS = 10
IDLE_CLOSE_SECS = 20  # keep the session open for a while after the last viewer leaves
FIRST_FRAME_TIMEOUT = 20
RETRY_MIN_SECS = 10
RETRY_MAX_SECS = 300  # each failed attempt can leave a half-open session in the printer
BOUNDARY = "frame"


def grey_ratio(frame: av.VideoFrame) -> float:
    """Share of perfectly grey 16 px blocks (luma ~128, no variation): these are the never-received
    areas of an incomplete frame (lost packets), which the decoder does not always flag.
    Samples one row out of 8; a few milliseconds per frame."""
    gray = frame.reformat(format="gray")
    plane = gray.planes[0]
    data, stride, w, h = bytes(plane), plane.line_size, gray.width, gray.height
    flat = total = 0
    for y in range(0, h, 8):
        row = data[y * stride: y * stride + w]
        for x in range(0, w - 15, 16):
            seg = row[x:x + 16]
            lo, hi = min(seg), max(seg)
            total += 1
            if hi - lo <= 2 and 125 <= lo and hi <= 131:
                flat += 1
    return flat / total if total else 0.0


GREY_LIMIT = 0.03  # above 3 % of flat grey blocks, the frame is considered incomplete


class CameraRelay:
    def __init__(self, printer: Printer, on_change: Callable[[], None] = lambda: None):
        self.printer = printer
        self.jpeg: bytes | None = None
        self._status = "idle"  # idle | connecting | live | error
        self.error: str | None = None
        self.retry_at: float | None = None  # time.time() of the next attempt after a failure
        self._on_change = on_change
        self._seq = 0
        self._cond = asyncio.Condition()
        self._viewers = 0
        self._last_viewer = 0.0
        self._task: asyncio.Task | None = None
        self._encoder: av.CodecContext | None = None
        self._pts = 0

    # ---------- viewers ----------
    async def stream(self) -> AsyncIterator[bytes]:
        """multipart/x-mixed-replace parts, one per frame."""
        self._viewers += 1
        self._ensure_running()
        seen = -1
        try:
            while True:
                async with self._cond:
                    await self._cond.wait_for(lambda: self._seq != seen)
                    seen, jpeg = self._seq, self.jpeg
                if jpeg:
                    yield (
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n".encode()
                        + jpeg
                        + b"\r\n"
                    )
        finally:
            self._viewers -= 1
            self._last_viewer = time.monotonic()

    async def snapshot(self, timeout: float = 6) -> bytes | None:
        """Latest frame (for thumbnails); keeps the session open for a while."""
        self._last_viewer = time.monotonic()
        self._ensure_running()
        if self.jpeg is None and self.status != "error":
            # Camera just opened: wait for the 2nd clean frame, the 1st one is sometimes badly exposed.
            start = self._seq
            with contextlib.suppress(asyncio.TimeoutError):
                async with self._cond:
                    await asyncio.wait_for(self._cond.wait_for(lambda: self._seq >= start + 2), timeout)
            self._last_viewer = time.monotonic()  # connecting may have taken several seconds
        return self.jpeg

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        if value != self._status:
            self._status = value
            self._on_change()

    def info(self) -> dict:
        return {"status": self.status, "error": self.error, "retry_at": self.retry_at}

    RESET_COOLDOWN = 30  # s

    async def reset(self) -> None:
        """Close the session (cleanly), forget the frame and errors, then reconnect right away.
        Refused while connecting: cutting the camera off mid-negotiation can lock it."""
        if self.status == "connecting":
            raise RuntimeError("camera is connecting, try again in a few seconds")
        wait = self.RESET_COOLDOWN - (time.monotonic() - getattr(self, "_last_reset", -1e9))
        if wait > 0:
            raise RuntimeError(f"camera was reset recently, try again in {wait:.0f} s")
        self._last_reset = time.monotonic()
        await self.close()
        await go2rtc.reset_stream(self.printer.id, self.printer.host)
        self.jpeg, self.error, self.retry_at = None, None, None
        self.status = "idle"
        self._last_viewer = time.monotonic()  # so the relay restarts even with no open stream
        self._ensure_running()

    def _ensure_running(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"camera-{self.printer.id}")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _wanted(self) -> bool:
        return self._viewers > 0 or time.monotonic() - self._last_viewer < IDLE_CLOSE_SECS

    # ---------- WebRTC session ----------
    async def _run(self) -> None:
        delay = RETRY_MIN_SECS
        while self._wanted():
            try:
                await self._session()
                delay = RETRY_MIN_SECS  # the session worked, then stopped
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.jpeg = None
                self.error = str(e) or type(e).__name__
                self.retry_at = time.time() + delay
                self.status = "error"
                self._on_change()
                log.warning("%s: camera: %s (retrying in %ds)", self.printer.name, self.error, delay)
                await asyncio.sleep(delay)
                self.retry_at = None
                delay = min(delay * 2, RETRY_MAX_SECS)
                continue
            if self._wanted():
                await asyncio.sleep(RETRY_MIN_SECS)
        self.status = "idle"
        self.jpeg = None  # no stale frame on the next open
        log.info("%s: camera closed (no more viewers)", self.printer.name)

    async def _session(self) -> None:
        self.status = "connecting"
        self._encoder = None
        url = await go2rtc.ensure_stream(self.printer.id, self.printer.host)
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        stop = False

        def publish(jpeg: bytes) -> None:
            asyncio.ensure_future(self._publish(jpeg))

        def run() -> None:
            # Thread: PyAV reads and decodes the stream in a blocking way.
            container = av.open(url, options={"rtsp_transport": "tcp"}, timeout=(FIRST_FRAME_TIMEOUT, 10))
            try:
                last = 0.0
                keyed = False
                for frame in container.decode(video=0):
                    if stop:
                        return
                    # Before the first key frame, H264 decoding yields corrupted frames.
                    keyed = keyed or frame.key_frame
                    # Lost packets (Wi-Fi): incomplete frame, often a grey band. Skip it.
                    if not keyed or frame.is_corrupt:
                        continue
                    if not first.done():
                        loop.call_soon_threadsafe(lambda f=frame: first.done() or first.set_result((f.width, f.height)))
                    now = time.monotonic()
                    # The H264 decoder must see every frame, even those we don't encode.
                    if now - last >= 1 / MAX_FPS:
                        if grey_ratio(frame) > GREY_LIMIT:
                            continue  # incomplete frame: keep the previous one, try the next one
                        last = now
                        loop.call_soon_threadsafe(publish, self._encode(frame))
            finally:
                container.close()

        reader = asyncio.ensure_future(asyncio.to_thread(run))
        try:
            done, _ = await asyncio.wait({first, reader}, timeout=FIRST_FRAME_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            if first not in done:
                if reader in done and reader.exception():
                    raise RuntimeError(f"stream unavailable: {reader.exception()}")
                raise RuntimeError("no image from the camera (in use by another application, or locked: restart the printer)")
            w, h = first.result()
            log.info("%s: camera connected (%dx%d)", self.printer.name, w, h)
            self.error = None
            self.status = "live"
            while self._wanted() and not reader.done():
                await asyncio.sleep(0.5)
            if reader.done() and reader.exception():
                raise RuntimeError(f"stream interrupted: {reader.exception()}")
        finally:
            stop = True  # the thread stops at the next frame (or at the read timeout)
            if not first.done():
                first.cancel()

    def _encode(self, frame: av.VideoFrame) -> bytes:
        enc = self._encoder
        if not enc or enc.width != frame.width or enc.height != frame.height:
            enc = av.CodecContext.create("mjpeg", "w")
            enc.width, enc.height = frame.width, frame.height
            enc.pix_fmt = "yuvj420p"
            enc.time_base = Fraction(1, MAX_FPS)
            enc.options = {"q:v": "5"}
            self._encoder = enc
            self._pts = 0
        img = frame.reformat(format="yuvj420p")
        # Encoder-specific timestamps: the stream's restart from 0 on every session.
        self._pts += 1
        img.pts, img.time_base = self._pts, enc.time_base
        packets = enc.encode(img)
        return b"".join(bytes(p) for p in packets)

    async def _publish(self, jpeg: bytes) -> None:
        async with self._cond:
            self.jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()
