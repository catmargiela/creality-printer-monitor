"""Discovery of Creality OS printers on the local network.

We try the WebSocket port (9999) on every address of the subnet, then request
`http://<ip>/info` (model + MAC address). The MAC lets us find a printer again
after the router changed its IP address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket

import httpx

from . import printer as printer_mod

log = logging.getLogger(__name__)


def local_network() -> ipaddress.IPv4Network:
    if env := os.environ.get("PM_SCAN_SUBNET"):
        return ipaddress.IPv4Network(env, strict=False)
    # Address of the network interface serving the default route (no packet is sent).
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    return ipaddress.IPv4Network(f"{ip}/24", strict=False)


async def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


async def scan(timeout: float = 1.2) -> list[dict]:
    """Printers found: [{host, mac, model}], sorted by address."""
    net = local_network()
    sem = asyncio.Semaphore(128)

    async def probe(host: str) -> dict | None:
        async with sem:
            if not await _port_open(host, printer_mod.WS_PORT, timeout):
                return None
        info: dict = {}
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                info = (await c.get(f"http://{host}:{printer_mod.HTTP_PORT}/info")).json()
        except Exception:
            pass
        model = info.get("model")
        return {
            "host": host,
            "mac": (info.get("mac") or "").upper() or None,
            "model": model,
            "model_name": printer_mod.MODEL_NAMES.get(model, model),
        }

    results = await asyncio.gather(*(probe(str(h)) for h in net.hosts()))
    found = [r for r in results if r]
    log.info("scan of %s: %d printer(s)", net, len(found))
    return sorted(found, key=lambda r: ipaddress.IPv4Address(r["host"]))
