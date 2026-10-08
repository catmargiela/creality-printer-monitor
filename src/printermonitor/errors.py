"""Readable error messages from the printer's raw state.

Code source: Creality Print's device manager (resources/web/deviceMgr,
`printerCode` / `errorJson` tables), extracted into creality_errors.json
(official English text + severity level). Common cases get a clearer title
and practical advice here; the others show the official text.

Like Creality Print, we look up `err.key` (the dictionary index) first, then
`errcode`. Filament runout and power loss are not reported as errors: they are
inferred from `materialStatus` and `repoPlrStatus`.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

# code -> (title, advice, category)
MESSAGES: dict[int, tuple[str, str, str]] = {
    # Filament
    116: ("Out of filament", "Load a new spool, then resume the print.", "filament"),
    20010: ("Out of filament", "Load a new spool, then resume the print.", "filament"),
    2839: ("Out of filament", "The spool is empty: reload it, then resume the print.", "filament"),
    30000: ("No filament loaded", "Load filament before starting a print.", "filament"),
    30010: ("Spool holder empty", "Put a spool on the holder.", "filament"),
    # Extrusion
    2845: ("Extruder may be clogged", "Check the nozzle and the filament, then try again.", "extrusion"),
    2789: ("Abnormal extrusion", "Possible clog: check the nozzle and that the filament feeds properly.", "extrusion"),
    528: ("Printing without extrusion", "Filament is no longer feeding: clog, tangled spool or broken filament.", "extrusion"),
    2111: ("Nozzle too cold to extrude", "Let the nozzle heat up before extruding.", "extrusion"),
    # AI / camera
    500: ("AI detected a print defect", "Check the camera: spaghetti, detachment… Resume if everything looks fine, otherwise stop.", "ai"),
    101: ("Print defect detected, paused", "Check the camera, then resume or stop.", "ai"),
    206: ("AI detected a print defect", "Check the camera, then resume or stop.", "ai"),
    503: ("Defective first layer", "Clean the bed (IPA), check the leveling, then try again.", "ai"),
    104: ("Foreign object on the bed", "Remove whatever is on the bed before continuing.", "ai"),
    117: ("Warping detected", "Clean the bed, raise its temperature or add a brim.", "ai"),
    523: ("Warping detected", "Clean the bed, raise its temperature or add a brim.", "ai"),
    509: ("Camera disabled", "Re-enable the camera in the printer settings.", "camera"),
    510: ("Camera failure", "Restart the printer; if it persists, check the camera cable.", "camera"),
    529: ("Camera failure", "Restart the printer; if it persists, check the camera cable.", "camera"),
    # Power loss
    115: ("Print interrupted (power loss)", "You can resume where it stopped, or cancel it.", "power"),
    # Heating
    4: ("Not heating", "Check the print head and bed connections.", "heating"),
    2564: ("Nozzle not heating", "Check the print head cable; restart the printer.", "heating"),
    2565: ("Bed not heating", "Check the bed cable; restart the printer.", "heating"),
    209: ("Abnormal bed temperature", "Let it cool down and check the bed cable.", "heating"),
    2509: ("Nozzle thermistor disconnected", "Check the nozzle thermistor connector.", "heating"),
    2515: ("Nozzle thermistor short circuit", "Check the nozzle thermistor cable.", "heating"),
    2510: ("Bed thermistor disconnected", "Check the bed thermistor connector.", "heating"),
    2516: ("Bed thermistor short circuit", "Check the bed thermistor cable.", "heating"),
    # Axes
    2573: ("Homing failed (X)", "Make sure nothing blocks the X axis, then home again.", "axes"),
    2577: ("Homing failed (Y)", "Make sure nothing blocks the bed (Y axis).", "axes"),
    2581: ("Homing failed (Z)", "Make sure nothing blocks the Z axis.", "axes"),
    2588: ("Z offset out of range", "Redo the bed calibration (leveling).", "axes"),
    800: ("Axis movement error", "Make sure nothing blocks the print head or the bed.", "axes"),
    # Fans
    501: ("Hotend fan too slow", "Make sure no wire or filament is blocking the hotend fan.", "fans"),
    502: ("Mainboard fan fault", "Check the fan under the printer.", "fans"),
    526: ("Part cooling fan fault", "Check the part cooling fan (on the print head).", "fans"),
    10: ("Fan abnormality", "Make sure the fans spin freely.", "fans"),
    204: ("Fan abnormality", "Make sure the fans spin freely.", "fans"),
    # File / storage
    7: ("File coordinates outside the bed", "Re-slice the model for this printer.", "file"),
    208: ("Abnormal G-code file", "Re-slice and send the file again.", "file"),
    2571: ("Corrupted G-code file", "Send the file again.", "file"),
    508: ("Storage almost full", "Delete old files from the printer.", "file"),
}

LEVELS = {1: "warning", 2: "error", 3: "error"}


@lru_cache(maxsize=1)
def _table() -> dict[str, dict[str, Any]]:
    return json.loads((Path(__file__).parent / "creality_errors.json").read_text(encoding="utf-8"))


def _int(v: Any) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def describe(data: dict[str, Any]) -> dict[str, Any] | None:
    """Active error described for the UI, or None."""
    err = data.get("err")
    key = errcode = 0
    if isinstance(err, dict):
        key, errcode = _int(err.get("key")), _int(err.get("errcode"))
    else:
        errcode = _int(err)
    code = key or errcode
    if not code:
        # Situations the printer does not report as errors.
        if _int(data.get("materialStatus")) == 1:
            code = 2839
        elif _int(data.get("repoPlrStatus")) == 1:
            code = 115
        else:
            return None
    entry = _table().get(str(code), {})
    if code in MESSAGES:
        title, advice, category = MESSAGES[code]
    else:
        title = entry.get("en") or "Unknown error"
        advice, category = "Check the printer screen for details.", "other"
    return {
        "code": code,
        "title": title,
        "advice": advice,
        "category": category,
        "level": LEVELS.get(entry.get("level") or 0, "error"),
        "official": entry.get("en"),
        "resumable": code == 115,
    }
