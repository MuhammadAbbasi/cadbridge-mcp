# AutoCAD & CAD&PILLAR MCP: CadBridge

**AI agents edit the drawing open in AutoCAD or CAD&PILLAR, live, as DWG.** There is no DXF conversion, no copy of
the file, and no COM.

Claude Code, Claude Desktop, Codex, Cursor, Windsurf, VS Code Copilot, Google Antigravity, Gemini CLI, or any other
[MCP](https://modelcontextprotocol.io) client gets tools to read the open drawing, change it in transactions, show
it as an image and save it.

```
 AI app ──MCP (stdio)──▶ cadbridge_mcp.py ──named pipe──▶ CadBridge.dll inside the CAD ──▶ open DWG
                          (Python, local)    (this user     (runs every request on the
                                              only)          CAD's main thread, .NET API)
```

## Install

1. [Download the ZIP](https://github.com/MuhammadAbbasi/AutoCad-CadPiller-MCP/archive/refs/heads/main.zip) (or clone the repository) and unzip it on the PC. Only the [`cadbridge/`](cadbridge) folder is needed.
2. If this Windows user has never started the CAD, start it once and close it.
3. Double-click **[`cadbridge/Install.cmd`](cadbridge/Install.cmd)**. It works in five steps:

   | Step | What happens |
   |---|---|
   | 1. CAD programs | finds AutoCAD / AutoCAD-based verticals / CAD&PILLAR on the PC and asks which to set up |
   | 2. AI apps | asks which apps get the tools (the ones found on the PC are suggested) |
   | 3. Close programs | asks you to close the CAD and those apps, and waits until they are closed |
   | 4. Install | plugin + autoload at every CAD start; **uv, Python and the libraries are downloaded automatically when missing**; each app's MCP config is updated (with a backup) |
   | 5. Done | start the CAD, open a drawing, ask your AI app: *"use cad_status, then list the layers of the open drawing"* |

No administrator rights are needed: everything is per Windows user. **[`Uninstall.cmd`](cadbridge/Uninstall.cmd)**
removes it all. For automation: `installer.ps1 -Cads all -Clients found -Yes` (or `-Clients claude-code,cursor`).

**Or let your AI agent install it.** Open Claude Code, Codex, Gemini CLI, Cursor... in the unzipped folder and paste:

> Install CadBridge for me: read `cadbridge/AGENTS.md` and follow it.

[`cadbridge/AGENTS.md`](cadbridge/AGENTS.md) tells the agent what CadBridge is, how to run the installer without
questions, what each error means, how to verify the connection, and never to close your CAD itself.

## Supported

| CAD | Plugin build | Status |
|---|---|---|
| CAD&PILLAR 11 (AutoCAD 2025 OEM platform) | `net8` | used and tested live |
| AutoCAD 2025, 2026 and AutoCAD-based verticals (Civil 3D, Map 3D...) | `net8`, AutoCAD.NET 25.0 | built for them, not yet run live |
| AutoCAD 2021, 2022, 2023, 2024 | `net48`, AutoCAD.NET 24.0 | built for them, not yet run live (the .NET Framework code path is tested) |
| AutoCAD LT, AutoCAD 2020 and older | none | not supported (LT has no .NET API) |

| AI app | Configured in |
|---|---|
| Claude Code | `claude mcp add cadbridge -s user` |
| Claude Desktop | `%LOCALAPPDATA%\Claude-Data\claude_*\claude_desktop_config.json` (current Microsoft Store builds), `%APPDATA%\Claude\claude_desktop_config.json` (older builds) |
| OpenAI Codex | `%USERPROFILE%\.codex\config.toml` |
| Cursor | `%USERPROFILE%\.cursor\mcp.json` |
| Windsurf | `%USERPROFILE%\.codeium\windsurf\mcp_config.json` |
| VS Code (Copilot agent) | `%APPDATA%\Code\User\mcp.json` |
| Google Antigravity (IDE / `agy`) | `%USERPROFILE%\.gemini\config\mcp_config.json` (and `.gemini\antigravity\mcp_config.json` when present) |
| Gemini CLI | `%USERPROFILE%\.gemini\settings.json` |
| anything else | stdio: `%LOCALAPPDATA%\CadBridge\venv\Scripts\python.exe %LOCALAPPDATA%\CadBridge\cadbridge_mcp.py` |

## Tools

| Tool | Does |
|---|---|
| `cad_status` | the CADs running with CadBridge, which one is targeted, its open drawings |
| `cad_use` | target one CAD when several run (`acad`, `cadp` or a pipe name) |
| `cad_info` | the active drawing: units, extents, entity count, layers, blocks |
| `cad_list` | model-space entities with geometry (filter by layer / DXF type) |
| `cad_ops` | a batch of edits in **one transaction: all or nothing** (see below) |
| `cad_command` | any command line of the CAD, including the OEM's own commands (`_.ZOOM _E`, ...); **waits** until the CAD is idle and returns `status` (`done` / `timeout` / `cancelled` / `busy`), the prompt and the created handles; a command left waiting for input is cancelled at the timeout |
| `cad_lisp` | evaluates AutoLISP and **returns its value** as JSON (lists, points, handles, selection sets), or the error; also the way to set variables in CAD&PILLAR |
| `cad_cancel` | ESC ESC: cancels whatever command is active and waits until the CAD is idle |
| `cad_new`, `cad_open`, `cad_save` | new drawing, open a DWG (or activate it if already open), save as DWG |
| `cad_screenshot` | picture of the CAD window, to show the user the result |

`cad_ops` operations (coordinates WCS in drawing units, angles in degrees CCW; every create takes `layer` (created
when missing) and `color` (ACI)):

| Group | Ops |
|---|---|
| geometry | `line`, `pline` (points with optional bulge, `closed`), `circle`, `arc` |
| annotation | `text` (MTEXT; `\P` is a new line), `dim` (aligned; `scale` = DIMSCALE) |
| fills | `hatch` (any pattern; `scale`, `angle`; `keep_boundary`) |
| 3D (always WCS, whatever the UCS) | `box`, `cylinder` (any axis; `r2` = cone), `sphere`, `extrude` (an outline or a closed curve), `union` / `subtract` / `intersect`, `face3d`, `poly3d`; each returns `{handle, min, max}` |
| blocks | `block_define` (a block from create ops), `block_import` (a DWG file becomes a block; returns its size), `block_info`, `insert` (`z`, `xscale` / `yscale` / `zscale`: negative mirrors) |
| editing | `move`, `copy` (`dz` too), `rotate`, `delete`, `set` (layer / color / text), `layer` (a missing linetype is loaded from the CAD's own `.lin` files) |
| reading / settings | `info`, `list`, `get`, `getvar`, `setvar` |

## Working principles

These are the rules the current design follows, each learnt the hard way:

1. **No COM.** Some OEMs switch off AutoCAD's ActiveX completely: in CAD&PILLAR every call returns `E_FAIL`, even
   from inside the process. CadBridge uses the managed .NET API, which every AutoCAD-platform product keeps.
2. **No DXF.** Edits go straight into the open drawing's database; `cad_save` writes the DWG. The user keeps working
   on the same file.
3. **Main thread only.** Requests arrive on a pipe thread and run on the CAD's main thread through its WPF
   dispatcher. Idle-event polling looked equivalent and hung after the first drawing was opened.
4. **One batch, one transaction.** `cad_ops` either applies every op or none; a failing op names its index and nothing
   is written.
5. **One pipe per CAD process,** readable only by the same Windows user: `\\.\pipe\cadbridge-<exe>-<pid>`. AutoCAD
   and CAD&PILLAR can run side by side.
6. **Compile against the oldest release of a platform generation.** A plugin referencing AutoCAD.NET 25.0 loads in
   2025, 2026 and OEMs on them; one referencing 25.1 (built from CAD&PILLAR's own DLLs) would not load in 2025.
7. **Autoload through the CAD's own registry key** (`...\Applications\CadBridge`, `LOADCTRLS=2`). OEMs ignore the
   `ApplicationPlugins` bundle folders, and the CAD's trusted-path security setting is never changed.
8. **The MCP server runs from local disk.** Started from a network share, imports took 38 s, longer than the 30 s
   MCP connect timeout. Locally it starts in about 3 s.
9. **Sketch, accept, enhance.** Agents should draw a sketch, show it with `cad_screenshot`, get the user's approval,
   then refine it with real blocks (fixtures, furniture, trees, lawn hatch).
10. **Wait on the pipe thread, never on the main thread.** `cad_command` / `cad_lisp` queue the text, then poll the CAD
    (through the dispatcher) until it is idle; a command still waiting for input at the timeout is cancelled.
11. **Short LISP lines, plain LISP.** CAD&PILLAR's LISP reader crashes on one long line (a 1,021-character line took
    the CAD down), and it registers none of a plugin's .NET commands or LISP functions, nor accepts `setvar` from
    one. So LISP is sent in lines of about 200 characters and returns its value as JSON written by the LISP itself.
    `src/live_check.py` checks all of it on a running CAD.

## Example: a 2-bedroom house on a plot

[`cadbridge/examples/`](cadbridge/examples) contains a test drawn live in CAD&PILLAR on a plot plan, in millimetres:
- `house_sketch.py`: the sketch, with 15 cm walls, 60 cm doors, room areas and dimensions.
- `house_enhance.py`: the enhancement. It defines blocks (WC, basin, shower, kitchen sink, hob, fridge, beds,
  wardrobes, sofa, dining set, bench, shrub), uses CAD&PILLAR's own tree symbol, and adds a lawn hatch that follows
  the curved plot edge, plus a paved terrace and path.

Both run through the MCP layer (`fastmcp.Client` calling `cad_ops`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No CAD with CadBridge is running` | start the CAD; `%APPDATA%\CadBridge\cadbridge.log` should end with `pipe server started on ...` |
| `timeout: CAD main thread busy` | a dialog is open in the CAD or a command waits for input: close it |
| `Several CADs are running` | `cad_use("acad")` or `cad_use("cadp")` |
| the AI app shows no tools | restart the app: apps read their MCP config only at start |
| hatch looks solid | pass `scale`: patterns are defined in mm or inches, and drawings in mm need large scales |

## Development

```
cadbridge/
  Install.cmd, Uninstall.cmd, installer.ps1   one-click installer (Windows PowerShell 5.1)
  AGENTS.md                                   what an AI agent needs to install, verify and use it
  bin/net8/, bin/net48/                       prebuilt plugin
  src/CadBridge.cs                            the plugin: pipe server, dispatcher, ops, MiniJson
  src/build.ps1                               pwsh src/build.ps1 [-Target net8|net48|all]
  src/cadbridge_mcp.py                        the MCP server (fastmcp)
  src/live_check.py                           live acceptance check on a running CAD (works in a new drawing)
  examples/                                   the house test
```

`build.ps1` uses the C# compiler bundled in PowerShell 7 and downloads Autodesk's AutoCAD.NET reference packages
from nuget.org, so no .NET SDK or CAD installation is needed to build. Close the CAD before installing a new build:
the DLL is locked while the CAD runs.

## The original COM / ezdxf server

This repository started as a fork of [U-C4N/Autocad-MCP](https://github.com/U-C4N/Autocad-MCP) (MIT), *AutoCAD
MCP Pro*: about 260 tools over COM (live, full AutoCAD only) or ezdxf (headless DXF), plus engineering packs
(mechanical, P&ID, architecture, solar). It is still here (`server.py`, `backends/`, `engineering/`) and documented in
[docs/autocad-mcp-pro.md](docs/autocad-mcp-pro.md). It cannot attach to CAD&PILLAR, whose COM is disabled; use
CadBridge for that.

License: MIT.
