# Creality Printer Monitor

[![CI](https://github.com/catmargiela/creality-printer-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/catmargiela/creality-printer-monitor/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-26%20tools-8A2BE2)](https://modelcontextprotocol.io)
[![Creality](https://img.shields.io/badge/Creality-Hi%20%7C%20K1%20%7C%20K2-1E90FF)](https://www.creality.com)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](#docker)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)](#requirements)

A local web dashboard **and an MCP server** for Creality OS 3D printers (**Creality Hi, K1 family, K2 family**).
It talks to the printers directly on your LAN with the same protocol as Creality Print, with **no Creality cloud account**.

- **Finds your printers on the network automatically**, and finds them again if your router gives them a new IP.
- One dashboard for all printers: live camera, progress, temperatures, time left, loaded filament.
- Pause / resume / stop, light, temperatures, fans, speed, axis moves, G-code console.
- Drag & drop a file to upload and print it, or send one file to several printers at once.
- Built-in slicing of `.stl` / `.3mf` / `.obj` / `.step` with OrcaSlicer's official Creality profiles, with a 3D preview.
- Print history with costs (filament + power), monthly stats, maintenance reminders.
- Smart plug support (Shelly, Tasmota): auto power-off once the print is done and the nozzle has cooled.
- Clear error messages with advice (filament runout, AI spaghetti detection, clogs, power loss…).
- **Control everything from Claude** (or any MCP client): status, camera snapshots Claude can look at, start/stop prints, upload files…

## Supported printers

| Printer                          | Status & control | Camera            |
|----------------------------------|------------------|-------------------|
| Creality Hi                      | ✅               | ✅ WebRTC (go2rtc) |
| K2 / K2 Plus / K2 Pro            | ✅               | ✅ WebRTC (go2rtc) |
| K1 / K1C / K1 SE / K1 Max        | ✅               | ✅ MJPEG           |
| Other Creality OS printers (WebSocket on port 9999) | should work | depends on model |

Classic Marlin printers (Ender-3, Ender-3 V2…) have no network API and are not supported.

## Requirements

- Python **3.12+** and [uv](https://docs.astral.sh/uv/getting-started/installation/) (or Docker).
- The computer running the app must be on the **same network** as the printers.
- Linux, macOS or Windows. The camera gateway ([go2rtc](https://github.com/AlexxIT/go2rtc)) is downloaded automatically
  for your OS/CPU, with its checksum verified.
- Optional: [OrcaSlicer](https://orcaslicer.com) to slice STL files from the app. On Linux x86-64 it installs in one click
  from **Settings → Slicing**; on macOS / Windows install it normally and the app will find it.

## Quick start

Run it straight from GitHub, no clone needed:

```bash
uvx --from git+https://github.com/catmargiela/creality-printer-monitor printermonitor
```

Or from a clone:

```bash
git clone https://github.com/catmargiela/creality-printer-monitor
cd creality-printer-monitor
uv run printermonitor
```

Open <http://localhost:8000> (or `http://<computer-ip>:8000` from a phone on the same Wi-Fi).

On first launch the app **scans your local network** and adds every Creality printer it finds.
You can add more later with **+ Printer** (by IP, or with the network scan).
Tip: give each printer a fixed IP (DHCP reservation) in your router. The app also follows a printer by its MAC address if its IP changes.

### Docker

Host networking is needed so the app can scan the LAN and reach the cameras (Linux hosts):

```bash
docker compose up -d
```

Data (printer list, history, downloaded tools) is stored in `./data`.

## Use it from Claude (MCP)

The app serves an MCP server at `http://<computer-ip>:8000/mcp/` (Streamable HTTP). The app must be running.

**Claude Code**

```bash
claude mcp add --transport http creality-printers http://127.0.0.1:8000/mcp/
```

**Claude Desktop** (needs Node.js). Edit `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`) and restart Claude Desktop:

```json
{
  "mcpServers": {
    "creality-printers": {
      "command": "npx",
      "args": ["-y", "mcp-remote@0.14.3", "http://127.0.0.1:8000/mcp/", "--allow-http", "--transport", "http-only"]
    }
  }
}
```

Replace `127.0.0.1` with the IP of the computer running the app if Claude runs on another machine.
In that case also allow that name: `PM_MCP_HOSTS=my-pc.local`.

**stdio** (any MCP client, same machine):

```json
{ "command": "uvx", "args": ["--from", "git+https://github.com/catmargiela/creality-printer-monitor", "printermonitor-mcp"] }
```

Set `PM_URL` if the app is not on `http://127.0.0.1:8000`, and `PM_PASSWORD` if you set one.

### Tools

| Category     | Tools |
|--------------|-------|
| Printers     | `list_printers`, `printer_status`, `discover_printers`, `add_printer` |
| Camera       | `camera_snapshot` (returns an image Claude can inspect: spaghetti, empty bed…) |
| Printing     | `start_print`, `pause_print`, `resume_print`, `stop_print`, `clear_error` |
| Files        | `list_files`, `send_file` (local path or URL; STL/3MF are sliced), `delete_file`, `slicer_profiles` |
| Control      | `set_temperatures`, `set_fan`, `set_light`, `set_speed`, `move_axis`, `home_axes`, `send_gcode` |
| Tracking     | `print_history`, `print_stats`, `maintenance`, `mark_maintenance_done` |
| Power        | `plug_power` |

Example prompts: *"Is anything printing right now?"*, *"Take a picture of the K1 and tell me if the print looks OK"*,
*"Slice ~/Downloads/benchy.stl in PLA and print it on the Hi if the bed is empty"*.

## Remote access

Do **not** open a port on your router: the printers' API has no authentication.
Use [Tailscale](https://tailscale.com) (or another VPN) on the computer running the app and on your phone,
then open `http://<computer-name>:8000` from anywhere. Cameras go through the app too.
You can also protect the app with a password (HTTP Basic auth, any username):

```bash
PM_PASSWORD='change-me' uv run printermonitor
```

## Configuration

| Variable              | Default                 | Purpose |
|-----------------------|-------------------------|---------|
| `PM_PORT`             | `8000`                  | HTTP port |
| `PM_HOST`             | `0.0.0.0`               | Listen address |
| `PM_PASSWORD`         | *(none)*                | Password (HTTP Basic auth) for the UI, API and MCP |
| `PM_DATA_DIR`         | OS user data dir¹       | SQLite database, photos, go2rtc, OrcaSlicer |
| `PM_CONFIG`           | `$PM_DATA_DIR/printers.json` | Printer list |
| `PM_SCAN_SUBNET`      | auto (`/24` of the default interface) | Subnet to scan, e.g. `192.168.0.0/23` |
| `PM_MCP_HOSTS`        | *(none)*                | Extra host names/IPs allowed to reach `/mcp/` |
| `PM_URL`              | `http://127.0.0.1:8000` | App URL used by the stdio MCP server |
| `ORCA_SLICER`         | auto                    | Path to the OrcaSlicer executable |
| `GO2RTC`              | auto (downloaded)       | Path to a go2rtc binary |
| `PM_GO2RTC_API_PORT`  | `19840`                 | go2rtc local API port |
| `PM_GO2RTC_RTSP_PORT` | `19554`                 | go2rtc local RTSP port |

¹ Linux `~/.local/share/creality-printer-monitor`, macOS `~/Library/Application Support/creality-printer-monitor`,
Windows `%APPDATA%\creality-printer-monitor`.

## How it works

The app speaks the local protocol used by Creality Print / OrcaSlicer:

| Feature              | Printer endpoint |
|----------------------|------------------|
| Status + commands    | WebSocket `ws://<ip>:9999` |
| Discovery            | TCP 9999 on every address of the subnet, then `GET http://<ip>/info` (model + MAC) |
| File upload          | `POST http://<ip>/upload/<name>` |
| Start a print        | `{"opGcodeFile": "printprt:<gcode dir>/<name>"}` |
| Camera (Hi / K2)     | WebRTC `http://<ip>:8000/call/webrtc_local`, opened by go2rtc and re-encoded to JPEG by the app |
| Camera (K1)          | MJPEG `http://<ip>:8080/?action=stream`, relayed by the app |
| Thumbnails           | `http://<ip>/downloads/humbnail/<name>.png` (sic) |
| Error messages       | `err.key` / `errcode`, mapped with Creality Print's error table (`creality_errors.json`) |

Code layout (`src/printermonitor/`): `printer.py` (printer connection), `server.py` (web API), `mcp_server.py` (MCP tools),
`discovery.py` (network scan), `camera.py` + `go2rtc.py` (camera relay), `slicer.py` (OrcaSlicer), `history.py` + `store.py`
(history, settings, SQLite), `plugs.py` (smart plugs), `errors.py` (error messages), `static/` (web UI, three.js 3D viewer).

### Camera notes

- A WebRTC camera session that is not closed cleanly can **lock the Creality Hi camera until the printer reboots**.
  The app therefore opens a single camera connection per printer, only while someone is watching, and closes it properly.
- If a camera stays unavailable, use **Reset camera** in the printer view; if that's not enough, power-cycle the printer.

## Disclaimer

This is an independent project, not affiliated with or endorsed by Creality. It sends real commands to real machines
(heating, moving, printing): use it at your own risk and never leave a printer unattended.

## License

[MIT](LICENSE). Bundles [three.js](https://threejs.org) (MIT). Downloads [go2rtc](https://github.com/AlexxIT/go2rtc) (MIT)
and optionally [OrcaSlicer](https://github.com/SoftFever/OrcaSlicer) (AGPL-3.0) at runtime; neither is redistributed.
