"""Smart plugs (Shelly, Tasmota): state, on/off, power off after a print.

Local APIs used (no cloud account):
  - Shelly Plus / Pro / Gen3 ("shelly2"): /rpc/Switch.GetStatus?id=0, /rpc/Switch.Set?id=0&on=true
  - Shelly Gen1 ("shelly1"): /relay/0, /relay/0?turn=on
  - Tasmota: /cm?cmnd=Power, /cm?cmnd=Power%20On
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)

TYPES = {"shelly2": "Shelly Plus / Pro / Gen3", "shelly1": "Shelly Gen1", "tasmota": "Tasmota"}
BUSY = {"printing", "paused", "processing", "self_test"}


async def get_state(cfg: dict[str, Any]) -> bool:
    kind, host = cfg["type"], cfg["host"]
    async with httpx.AsyncClient(timeout=4) as c:
        if kind == "shelly2":
            return bool((await c.get(f"http://{host}/rpc/Switch.GetStatus", params={"id": 0})).json()["output"])
        if kind == "shelly1":
            return bool((await c.get(f"http://{host}/relay/0")).json()["ison"])
        if kind == "tasmota":
            data = (await c.get(f"http://{host}/cm", params={"cmnd": "Power"})).json()
            return str(data.get("POWER", data.get("POWER1", ""))).upper() == "ON"
    raise ValueError(f"unknown plug type: {kind}")


async def set_state(cfg: dict[str, Any], on: bool) -> None:
    kind, host = cfg["type"], cfg["host"]
    async with httpx.AsyncClient(timeout=4) as c:
        if kind == "shelly2":
            r = await c.get(f"http://{host}/rpc/Switch.Set", params={"id": 0, "on": "true" if on else "false"})
        elif kind == "shelly1":
            r = await c.get(f"http://{host}/relay/0", params={"turn": "on" if on else "off"})
        elif kind == "tasmota":
            r = await c.get(f"http://{host}/cm", params={"cmnd": f"Power {'On' if on else 'Off'}"})
        else:
            raise ValueError(f"unknown plug type: {kind}")
        r.raise_for_status()


class AutoOff:
    """After a print finishes: wait for the nozzle to cool down, then switch the plug off.
    Cancelled if a new print starts in the meantime."""

    def __init__(self, settings: Callable[[], dict[str, Any]], printer_state: Callable[[str], tuple[str, float | None]]):
        self._settings = settings
        self._state = printer_state  # id -> (state, nozzle temperature)
        self._tasks: dict[str, asyncio.Task] = {}
        self.pending: dict[str, str] = {}  # id -> message shown to the user

    def schedule(self, printer_id: str) -> None:
        cfg = self._settings()["plugs"].get(printer_id)
        if not cfg or not cfg.get("auto_off") or not cfg.get("host"):
            return
        self.cancel(printer_id)
        self._tasks[printer_id] = asyncio.create_task(self._run(printer_id, cfg))

    def cancel(self, printer_id: str) -> None:
        if t := self._tasks.pop(printer_id, None):
            t.cancel()
        self.pending.pop(printer_id, None)

    async def _run(self, printer_id: str, cfg: dict[str, Any]) -> None:
        limit = float(cfg.get("off_below") or 50)
        try:
            await asyncio.sleep(float(cfg.get("delay_min") or 0) * 60)
            while True:
                state, nozzle = self._state(printer_id)
                if state in BUSY:
                    log.info("%s: power off cancelled, new print started", printer_id)
                    return
                if nozzle is not None and nozzle < limit:
                    break
                self.pending[printer_id] = f"Plug will switch off once the nozzle is below {limit:.0f} °C"
                await asyncio.sleep(15)
            await set_state(cfg, False)
            log.info("%s: plug switched off after the print", printer_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s: could not switch off the plug: %s", printer_id, e)
        finally:
            self.pending.pop(printer_id, None)
            self._tasks.pop(printer_id, None)
