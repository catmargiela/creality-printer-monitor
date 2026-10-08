"""Print history: the Hi doesn't keep one, so the app keeps it itself.

Every printer is tracked on each update: a print that starts creates a
"running" row, and its end closes it (completed, stopped, failed) with the
duration, filament, cost, thumbnail and a camera photo.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from . import printer as printer_mod
from .printer import HTTP_PORT, Printer, derive_state, relevant_error
from .store import Store

log = logging.getLogger(__name__)

ACTIVE = {"printing", "paused", "processing", "self_test"}
SAVE_EVERY = 60  # s: duration/progress of running prints saved to the database
FILAMENT_DIAMETER = 1.75


def parse_gcode_meta(text: str) -> dict[str, Any]:
    """Slicer info from the comments (OrcaSlicer / Creality Print / PrusaSlicer)."""
    meta: dict[str, Any] = {}

    def find(pattern: str) -> str | None:
        m = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
        return m.group(1).strip() if m else None

    if g := find(r"^;\s*(?:total )?filament used \[g\]\s*[=:]\s*([\d.]+)"):
        meta["filament_g"] = float(g)
    if t := find(r"^;\s*filament_type\s*[=:]\s*([^\n;]+)"):
        meta["material"] = t.split(";")[0].strip()
    if c := find(r"^;\s*filament_colou?r\s*[=:]\s*([^\n]+)"):
        meta["color"] = c.split(";")[0].strip()
    if t := find(r"^;\s*estimated printing time(?: \(normal mode\))?\s*[=:]\s*([^\n]+)"):
        meta["est_time_s"] = parse_duration(t)
    return meta


def parse_duration(s: str) -> int | None:
    total = 0
    for value, unit in re.findall(r"(\d+)\s*([dhms])", s):
        total += int(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total or None


def grams_from_length(mm: float, density: float) -> float:
    r = FILAMENT_DIAMETER / 2
    return mm * math.pi * r * r * density / 1000  # mm³ -> cm³ -> g


def material_family(material: str | None, known: dict[str, Any], default: str) -> str:
    """"Hyper PLA", "Generic PETG"… -> PLA / PETG (key of the price table)."""
    if not material:
        return default
    up = material.upper()
    for key in sorted(known, key=len, reverse=True):
        if key.upper() in up:
            return key
    return default


class Tracker:
    def __init__(
        self,
        store: Store,
        photo: Callable[[Printer], Awaitable[bytes | None]],
        on_finish: Callable[[Printer, dict[str, Any]], None] = lambda p, r: None,
    ):
        self.store = store
        self.photo = photo
        self.on_finish = on_finish
        self._open: dict[str, int] = {}  # printer id -> id of the running row
        self._last_save: dict[str, float] = {}
        self._last: dict[str, dict[str, Any]] = {}  # last values seen during the print

    # ---------- tracking ----------
    def update(self, p: Printer) -> None:
        if not p.online:
            return  # offline: conclude nothing, the print may still be running
        d = p.data
        state = derive_state(d, True)
        key = self._key(d)
        rec_id = self._open.get(p.id)
        if rec_id is None and (rec := self.store.running_print(p.id)):
            rec_id = self._open[p.id] = rec["id"]  # resume after an app restart

        if state in ACTIVE and key:
            if rec_id is not None and self.store.get_print(rec_id)["print_key"] != key:
                self._close(p, rec_id, "failed")  # another print replaced the previous one
                rec_id = None
            if rec_id is None:
                self._start(p, key)
            else:
                self._remember(p)
                self._save_progress(p, rec_id)
        elif rec_id is not None:
            self._remember(p)  # the final message often carries the final duration and filament
            if state == "completed":
                self._close(p, rec_id, "completed")
            elif state == "stopped":
                self._close(p, rec_id, "stopped")
            elif state in ("idle", "error"):
                self._close(p, rec_id, "failed")

    @staticmethod
    def _key(d: dict[str, Any]) -> str | None:
        name = d.get("printFileName")
        if not name:
            return None
        return f"{name}|{d.get('printId') or d.get('printStartTime') or ''}"

    def _remember(self, p: Printer) -> None:
        d = p.data
        last = self._last.setdefault(p.id, {})
        for k in ("printJobTime", "printProgress", "usedMaterialLength"):
            try:
                v = float(d.get(k))
                if v > 0:
                    last[k] = v
            except (TypeError, ValueError):
                pass
        if err := relevant_error(d, "paused"):
            last["error"] = err

    def _start(self, p: Printer, key: str) -> None:
        d = p.data
        job = float(d.get("printJobTime") or 0)
        file = Path(str(d.get("printFileName"))).name
        fil = self._loaded_filament(p)
        rec_id = self.store.insert_print(
            printer_id=p.id, printer_name=p.name, print_key=key, file=file, status="running",
            started_at=time.time() - job, progress=int(float(d.get("printProgress") or 0)),
            material=fil.get("type"), color=fil.get("color"),
        )
        self._open[p.id] = rec_id
        self._last[p.id] = {}
        self._remember(p)
        log.info("%s: print of %s recorded (#%d)", p.name, file, rec_id)
        asyncio.ensure_future(self._enrich(p, rec_id, file))

    def _save_progress(self, p: Printer, rec_id: int) -> None:
        now = time.monotonic()
        if now - self._last_save.get(p.id, 0) < SAVE_EVERY:
            return
        self._last_save[p.id] = now
        last = self._last.get(p.id, {})
        self.store.update_print(
            rec_id, duration_s=int(last.get("printJobTime", 0)), progress=int(last.get("printProgress", 0))
        )

    def _close(self, p: Printer, rec_id: int, status: str) -> None:
        self._open.pop(p.id, None)
        last = self._last.pop(p.id, {})
        rec = self.store.get_print(rec_id) or {}
        now = time.time()
        duration = int(last.get("printJobTime") or (now - (rec.get("started_at") or now)))
        progress = 100 if status == "completed" else int(last.get("printProgress", rec.get("progress") or 0))
        settings = self.store.settings()
        family = material_family(rec.get("material"), settings["filaments"], settings["default_filament"])
        density = settings["filaments"].get(family, {}).get("density", 1.24)
        grams = None
        if last.get("usedMaterialLength"):
            grams = grams_from_length(last["usedMaterialLength"], density)
        elif rec.get("est_filament_g"):
            grams = rec["est_filament_g"] * progress / 100
        fields: dict[str, Any] = {
            "status": status, "ended_at": now, "duration_s": duration, "progress": progress,
            "filament_g": round(grams, 2) if grams else None,
        }
        fields.update(self._costs(fields, family, settings))
        if status != "completed" and (err := last.get("error")):
            fields["error_code"], fields["error_title"] = err["code"], err["title"]
        self.store.update_print(rec_id, **fields)
        log.info("%s: print #%d %s (%d s)", p.name, rec_id, status, duration)
        asyncio.ensure_future(self._final_photo(p, rec_id))
        self.on_finish(p, {**rec, **fields})

    @staticmethod
    def _costs(fields: dict[str, Any], family: str, settings: dict[str, Any]) -> dict[str, Any]:
        price = settings["filaments"].get(family, {}).get("price_kg", 0)
        energy = settings["energy"]
        return {
            "cost_filament": round((fields.get("filament_g") or 0) * price / 1000, 3),
            "cost_energy": round(fields["duration_s"] / 3600 * energy["power_w"] / 1000 * energy["price_kwh"], 3),
        }

    def recompute_costs(self) -> None:
        """After a price change: recompute the whole history."""
        settings = self.store.settings()
        for rec in self.store.prints(limit=100000):
            if rec["status"] == "running" or rec["duration_s"] is None:
                continue
            family = material_family(rec.get("material"), settings["filaments"], settings["default_filament"])
            self.store.update_print(rec["id"], **self._costs(rec, family, settings))

    # ---------- enrichment (network) ----------
    @staticmethod
    def _loaded_filament(p: Printer) -> dict[str, Any]:
        for box in (p.data.get("boxsInfo") or {}).get("materialBoxs", []):
            for m in box.get("materials", []):
                if m.get("selected") or box.get("type") == 1:
                    color = re.search(r"([0-9a-fA-F]{6})$", str(m.get("color") or ""))
                    return {"type": m.get("type"), "color": f"#{color.group(1)}" if color else None}
        return {}

    async def _enrich(self, p: Printer, rec_id: int, file: str) -> None:
        """Slicer estimates (start and end of the file) and thumbnail."""
        base = f"http://{p.host}:{HTTP_PORT}/downloads"
        fields: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                text = await self._gcode_ends(c, p, file)
                meta = parse_gcode_meta(text)
                if meta.get("filament_g"):
                    fields["est_filament_g"] = meta["filament_g"]
                if meta.get("est_time_s"):
                    fields["est_time_s"] = meta["est_time_s"]
                rec = self.store.get_print(rec_id) or {}
                if not rec.get("material") and meta.get("material"):
                    fields["material"] = meta["material"]
                if not rec.get("color") and meta.get("color"):
                    fields["color"] = meta["color"]
                r = await c.get(f"{base}/humbnail/{quote(Path(file).stem)}.png")
                if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                    name = f"{rec_id}_thumb.png"
                    (self.store.media / name).write_bytes(r.content)
                    fields["thumb"] = name
        except Exception as e:
            log.warning("%s: info for file %s unavailable: %s", p.name, file, e)
        self.store.update_print(rec_id, **fields)

    @staticmethod
    async def _gcode_ends(c: httpx.AsyncClient, p: Printer, file: str) -> str:
        """Start + end of the G-code (where slicers put their estimates).
        Moonraker supports range requests; the printer's web server doesn't."""
        url = f"http://{p.host}:{printer_mod.MOONRAKER_PORT}/server/files/gcodes/{quote(file)}"
        parts = []
        try:
            for rng in ("bytes=0-65535", "bytes=-131072"):
                r = await c.get(url, headers={"Range": rng})
                if r.status_code != 206:
                    raise httpx.HTTPError("no range request support")
                parts.append(r.text)
            return "\n".join(parts)
        except httpx.HTTPError:
            r = await c.get(f"http://{p.host}:{HTTP_PORT}/downloads/gcode/{quote(file)}")
            r.raise_for_status()
            return r.text[:65536] + "\n" + r.text[-131072:]

    async def _final_photo(self, p: Printer, rec_id: int) -> None:
        try:
            jpeg = await self.photo(p)
        except Exception as e:
            log.warning("%s: could not take the final photo: %s", p.name, e)
            return
        if jpeg:
            name = f"{rec_id}_photo.jpg"
            (self.store.media / name).write_bytes(jpeg)
            self.store.update_print(rec_id, photo=name)
