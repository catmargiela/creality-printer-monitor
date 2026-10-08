import logging
import os


def main() -> None:
    import faulthandler
    import signal

    import uvicorn

    # Diagnostics: `kill -USR1 <pid>` dumps every thread's stack to stderr.
    if hasattr(signal, "SIGUSR1"):  # not on Windows
        faulthandler.register(signal.SIGUSR1, all_threads=True)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        "printermonitor.server:app",
        # Standard asyncio loop, not uvloop: uvloop spawns subprocesses (go2rtc,
        # OrcaSlicer) in a way that can hang the whole server while other threads
        # are running (camera video decoding). Reproduced on 2026-10-08.
        loop="asyncio",
        host=os.environ.get("PM_HOST", "0.0.0.0"),
        port=int(os.environ.get("PM_PORT", "8000")),
    )
