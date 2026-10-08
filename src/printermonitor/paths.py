"""Where the app keeps its data (printer list, SQLite database, downloaded tools).

Defaults to the per-user data directory of the OS, so the app works the same
whether it is started from a git checkout, `uvx` or Docker:
  - Linux   : $XDG_DATA_HOME/creality-printer-monitor (~/.local/share/…)
  - macOS   : ~/Library/Application Support/creality-printer-monitor
  - Windows : %APPDATA%\\creality-printer-monitor
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "creality-printer-monitor"


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


DATA_DIR = Path(os.environ.get("PM_DATA_DIR") or _default_data_dir())
CONFIG = Path(os.environ.get("PM_CONFIG") or DATA_DIR / "printers.json")
