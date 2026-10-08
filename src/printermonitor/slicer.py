"""STL/3MF -> G-code slicing with the OrcaSlicer command line.

OrcaSlicer is installed on demand into data/orca (official AppImage extracted,
checksum verified), or taken from the system. Profiles come from its system
profiles (e.g. "Creality Hi 0.4 nozzle", "0.20mm Standard @Creality Hi",
"Creality Generic PLA @Hi-all"): nothing to export by hand.

System profiles only store their differences from a parent (`inherits`), and
the command line does NOT resolve that inheritance (density 0, wrong
temperatures…): so we "flatten" them before passing them on.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import httpx

from .paths import DATA_DIR

ORCA_DIR = DATA_DIR / "orca"
VERSION = "v2.4.2"
ASSET = "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage"
DIGEST = "d12fb8c8eac1aecd2dfb6377acd48f994f8fa439ed5292fa532dd82880f029fd"
VENDOR = "Creality"

install_state: dict[str, Any] = {"step": None, "progress": 0, "error": None}


# ---------- installation ----------
def find_binary() -> str | None:
    if env := os.environ.get("ORCA_SLICER"):
        return env if Path(env).exists() or shutil.which(env) else None
    local = ORCA_DIR / "squashfs-root" / "AppRun"
    if local.exists():
        return str(local)
    for b in ("orca-slicer", "OrcaSlicer", "orcaslicer"):
        if p := shutil.which(b):
            return p
    for p in (
        Path("/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "OrcaSlicer" / "orca-slicer.exe",
    ):
        if p.exists():
            return str(p)
    return None


def can_install() -> bool:
    """The one-click install uses the official Linux x86-64 AppImage; elsewhere, install OrcaSlicer yourself."""
    return sys.platform.startswith("linux") and platform.machine().lower() in ("x86_64", "amd64")


def profiles_dir() -> Path | None:
    candidates = [
        ORCA_DIR / "squashfs-root" / "resources" / "profiles",
        Path("/usr/share/OrcaSlicer/resources/profiles"),
        Path("/usr/share/orca-slicer/resources/profiles"),
        Path("/opt/orca-slicer/resources/profiles"),
    ]
    if b := find_binary():
        exe = Path(b).resolve()
        candidates[:0] = [
            exe.parent / "resources" / "profiles",
            exe.parent.parent / "resources" / "profiles",
            exe.parent.parent / "Resources" / "profiles",  # macOS .app bundle
        ]
    return next((c for c in candidates if (c / VENDOR).is_dir()), None)


async def install(on_change: Callable[[], None] = lambda: None) -> None:
    """Download the official AppImage, verify its checksum and extract it (~380 MB)."""
    if install_state["step"] in ("download", "extract"):
        return
    if not can_install():
        install_state.update(step="error", progress=0, error="automatic install is Linux x86-64 only: install OrcaSlicer from orcaslicer.com")
        on_change()
        return
    install_state.update(step="download", progress=0, error=None)
    on_change()
    ORCA_DIR.mkdir(parents=True, exist_ok=True)
    image = ORCA_DIR / ASSET
    try:
        url = f"https://github.com/OrcaSlicer/OrcaSlicer/releases/download/{VERSION}/{ASSET}"
        h = hashlib.sha256()
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=120)) as c:
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done, last = 0, -1
                with image.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 20):
                        fh.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        pct = int(done * 100 / total) if total else 0
                        if pct != last:
                            install_state["progress"], last = pct, pct
                            on_change()
        if h.hexdigest() != DIGEST:
            raise RuntimeError("invalid checksum: file rejected")
        install_state.update(step="extract", progress=100)
        on_change()
        image.chmod(0o755)
        shutil.rmtree(ORCA_DIR / "squashfs-root", ignore_errors=True)
        proc = await asyncio.create_subprocess_exec(
            str(image), "--appimage-extract", cwd=ORCA_DIR,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not (ORCA_DIR / "squashfs-root" / "AppRun").exists():
            raise RuntimeError(f"extraction failed: {err.decode(errors='replace')[-300:]}")
        _index.cache_clear()
        install_state.update(step="done")
    except Exception as e:
        install_state.update(step="error", error=str(e) or type(e).__name__)
    finally:
        image.unlink(missing_ok=True)
        on_change()


# ---------- profiles ----------
@lru_cache(maxsize=1)
def _index() -> dict[tuple[str, str], Path]:
    """(type, name) -> file, for the Creality profiles and Orca's filament library."""
    root = profiles_dir()
    idx: dict[tuple[str, str], Path] = {}
    if not root:
        return idx
    for d in (root / "OrcaFilamentLibrary", root / VENDOR):  # Creality wins on duplicates
        for f in d.rglob("*.json"):
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(j, dict) and "name" in j and "type" in j:
                idx[(j["type"], j["name"])] = f
    return idx


def flatten(kind: str, name: str) -> dict[str, Any]:
    """Full profile: start from the root ancestor, each child overrides its parent's keys."""
    idx = _index()
    if (kind, name) not in idx:
        raise ValueError(f"profile not found: {name}")
    leaf = json.loads(idx[(kind, name)].read_text(encoding="utf-8"))
    chain, cur = [leaf], leaf
    while parent := cur.get("inherits"):
        if (kind, parent) not in idx:
            raise ValueError(f"parent profile not found: {parent}")
        cur = json.loads(idx[(kind, parent)].read_text(encoding="utf-8"))
        chain.append(cur)
    merged: dict[str, Any] = {}
    for p in reversed(chain):
        merged.update(p)
    merged.pop("inherits", None)
    for k in ("name", "from", "instantiation", "setting_id"):
        if k in leaf:
            merged[k] = leaf[k]
    return merged


def machine_name(model: str | None, nozzle: str = "0.4") -> str | None:
    """"Creality Hi" -> "Creality Hi 0.4 nozzle", "K1 Max" -> "Creality K1 Max (0.4 nozzle)", if Orca has it."""
    if not model:
        return None
    # Orca names most Creality machines "Creality K2 0.4 nozzle", but some "Creality K1 (0.4 nozzle)".
    for prefix in ("", "Creality "):
        for candidate in (f"{prefix}{model} {nozzle} nozzle", f"{prefix}{model} ({nozzle} nozzle)"):
            if ("machine", candidate) in _index():
                return candidate
    return None


def catalog(model: str | None) -> dict[str, Any]:
    """Process (quality) and filament profiles compatible with this model (0.4 nozzle)."""
    machine = machine_name(model)
    if not machine:
        return {"machine": None, "processes": [], "filaments": []}
    processes, filaments = [], []
    for (kind, name), path in _index().items():
        if kind not in ("process", "filament"):
            continue
        try:
            j = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Only "final" profiles that declare this machine model.
        if j.get("instantiation") != "true" or machine not in (j.get("compatible_printers") or []):
            continue
        (processes if kind == "process" else filaments).append(name)

    def layer(n: str) -> float:
        try:
            return float(n.split("mm")[0])
        except ValueError:
            return 9.0

    processes.sort(key=layer)
    filaments.sort(key=lambda n: (not n.startswith(("Creality Generic", "Generic")), n))
    return {"machine": machine, "processes": processes, "filaments": filaments}


def status() -> dict[str, Any]:
    return {"available": find_binary() is not None, "binary": find_binary(), "can_install": can_install(), "install": install_state}


# ---------- slicing ----------
def inject_thumbnails(gcode: Path, pngs: list[bytes]) -> None:
    """Orca's CLI cannot render thumbnails headless: we insert the ones rendered
    by the browser, in the format Klipper/Creality expect."""
    import struct

    blocks = []
    for png in pngs:
        w, h = struct.unpack(">II", png[16:24])  # IHDR header
        b64 = base64.b64encode(png).decode()
        lines = [b64[i:i + 78] for i in range(0, len(b64), 78)]
        blocks.append(
            f"; thumbnail begin {w}x{h} {len(b64)}\n" + "".join(f"; {l}\n" for l in lines) + "; thumbnail end\n;\n"
        )
    text = gcode.read_text(encoding="utf-8", errors="replace")
    marker = "; HEADER_BLOCK_END\n"
    i = text.find(marker)
    i = i + len(marker) if i >= 0 else 0
    gcode.write_text(text[:i] + "\n" + "".join(blocks) + text[i:], encoding="utf-8")


async def slice_model(model_file: Path, machine: str, process: str, filament: str, workdir: Path) -> Path:
    binary = find_binary()
    if not binary:
        raise RuntimeError("OrcaSlicer is not installed: install it in Settings, or upload a .gcode.")
    paths = {}
    for kind, name in (("machine", machine), ("process", process), ("filament", filament)):
        p = workdir / f"{kind}.json"
        p.write_text(json.dumps(flatten(kind, name)), encoding="utf-8")
        paths[kind] = p
    outdir, datadir = workdir / "out", workdir / "datadir"
    outdir.mkdir()
    datadir.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    proc = await asyncio.create_subprocess_exec(
        binary, "--datadir", str(datadir), "--slice", "0", "--arrange", "1", "--orient", "1", "--allow-newer-file",
        "--load-settings", f"{paths['machine']};{paths['process']}",
        "--load-filaments", str(paths["filament"]),
        "--outputdir", str(outdir), str(model_file),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=env, cwd=workdir,
    )
    await asyncio.wait_for(proc.wait(), timeout=900)
    result = {}
    with_result = outdir / "result.json"
    if with_result.exists():
        result = json.loads(with_result.read_text(encoding="utf-8"))
    gcodes = sorted(outdir.glob("*.gcode"))
    if not gcodes or result.get("return_code", 0) != 0:
        msg = result.get("error_string") or f"code {proc.returncode}"
        if "empty" in msg or "fully inside" in msg:
            msg = "the model doesn't fit on the bed (or it is empty)"
        raise RuntimeError(f"slicing failed: {msg}")
    return gcodes[0]


def new_workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="pm-slice-"))
