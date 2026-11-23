# CadBridge: guide for AI agents

You are an AI agent (Claude Code, Codex, Cursor, Gemini CLI, Antigravity, Copilot...) and the user asked you to
understand or install CadBridge. Read this whole file first. The user's prompt is usually just:

> Install CadBridge for me: read AGENTS.md in the CadBridge folder and follow it.

## What it is

CadBridge lets AI apps read and edit the drawing that is open in **AutoCAD 2021-2026** (and AutoCAD-based verticals)
or **CAD&PILLAR 11**, live, as DWG, through MCP:

```
AI app --MCP stdio--> %LOCALAPPDATA%\CadBridge\cadbridge_mcp.py --named pipe--> CadBridge.dll inside the CAD --> open DWG
```

- `bin/net8/CadBridge.dll`: plugin for AutoCAD 2025-2026 and CAD&PILLAR 11. `bin/net48/CadBridge.dll`: AutoCAD 2021-2024.
  The CAD loads it at every start (per-user registry autoload). It serves JSON on `\\.\pipe\cadbridge-<exe>-<pid>`.
- `src/cadbridge_mcp.py`: the MCP server (fastmcp, Python 3.12). Tools: `cad_status`, `cad_use`, `cad_info`,
  `cad_list`, `cad_ops`, `cad_command`, `cad_lisp`, `cad_cancel`, `cad_new`, `cad_open`, `cad_save`, `cad_screenshot`.
- `installer.ps1` (started by `Install.cmd`): finds the CADs, installs the plugin and its autoload, downloads uv,
  Python and the libraries when missing, and writes the MCP entry `cadbridge` into each AI app's config (with a backup).
- `VERSION` is the installed version; `CHANGELOG.md` says what changed. Everything is per Windows user: no admin rights.

COM is not used (CAD&PILLAR disables it), so nothing else needs to be enabled in the CAD.

## Rules

- **Never close, kill or restart the CAD or the user's apps yourself** (no `Stop-Process`, `taskkill`). Ask the user
  to save their work and close them, then continue when they say it is done.
- Do not edit the AI apps' config files by hand: the installer keeps the other servers in them and makes a backup.
- Do not change CAD settings (TRUSTEDPATHS, SECURELOAD): the registry autoload does not need them.

## Install

1. **Get the files.** If you do not have this folder yet, download the latest release and unzip it (any folder works:
   the installer copies everything it needs to `%LOCALAPPDATA%\CadBridge`):

   ```
   Invoke-WebRequest https://github.com/MuhammadAbbasi/cadbridge-mcp/releases/latest/download/cadbridge.zip -OutFile cadbridge.zip
   Expand-Archive cadbridge.zip -DestinationPath . -Force
   ```

   The ZIP holds this `cadbridge` folder. A clone of the repository works too: use its `cadbridge` folder.
   **Check the folder.** It must contain `installer.ps1`, `VERSION`, `bin\net8\CadBridge.dll`, `bin\net48\CadBridge.dll`
   and `src\cadbridge_mcp.py`. In the full repository this is the `cadbridge\` folder.
2. **Check the CAD is closed.** `tasklist /FI "IMAGENAME eq acad.exe"` and `tasklist /FI "IMAGENAME eq cadp.exe"`.
   If one runs, ask the user to save and close it, and wait.
3. **Run the installer without questions** (Windows PowerShell; run it from this folder):

   ```
   powershell -NoProfile -ExecutionPolicy Bypass -File .\installer.ps1 -Cads all -Clients found -Yes
   ```

   - `-Clients found` configures every AI app found on this PC. To choose: `-Clients claude-code,claude-desktop`
     (keys: `claude-code`, `claude-desktop`, `codex`, `cursor`, `windsurf`, `vscode`, `antigravity`, `gemini-cli`).
     Ask the user only if they said they want a specific app.
   - **Shared usage log:** if `Install.cmd` in this folder passes `-LogDir "<folder>"`, pass the same `-LogDir` here
     (an organisation's kit collects every user's log in that folder).
   - It needs internet the first time (uv, Python 3.12, fastmcp, pywin32, pillow). It takes 1-3 minutes.
4. **Read the output.** Success ends with `Step 5/5  Done`. Exit code 1 means it stopped; the last lines say why:

   | Output | Do |
   |---|---|
   | `Close the CAD first` | ask the user to close the CAD, then run step 3 again |
   | `Start these once, close them, then run the installer again: ...` | that CAD was never started by this Windows user: ask the user to start it once, close it, then run step 3 again |
   | `No AutoCAD or CAD&PILLAR found on this PC` | nothing to install into: tell the user (AutoCAD LT is not supported: it has no .NET API) |
   | `uv / Python / libraries could not be installed (internet access?)` | check the internet or proxy, then run again |
   | `! <file> is not plain JSON (comments?); left untouched.` | that app's config has comments: show the user the line the installer printed, to add by hand |
   | `still running: ...` then `continuing` | fine: those AI apps must be restarted to see the tools |

5. **Tell the user what to do next:**
   1. Start the CAD and open a drawing (the plugin loads with the CAD).
   2. Restart the AI apps that were configured, including the one you run in, and close every open terminal session
      of them: apps read their MCP config only when they start.
   3. In a new chat, ask: *"use cad_status, then list the layers of the open drawing"*.

## Verify

After the user restarted the CAD and the app, if you have the `cad_status` tool, call it. Expected: `connected`
with the CAD's `product` and `acadver`, and `version` equal to `VERSION`. A `warning` about versions means the CAD
still runs an older plugin: the user must close and restart the CAD.

Files to check when something does not work:

| Where | What |
|---|---|
| `%APPDATA%\CadBridge\cadbridge.log` | the plugin; it should end with `pipe server started on \\.\pipe\cadbridge-...` after the CAD starts |
| `%LOCALAPPDATA%\CadBridge\server.log` | the MCP server, one JSON line per event (`server_start`, `client_connected`, `connected`, `tool` with `ok` and `kind`) |
| `%LOCALAPPDATA%\CadBridge\venv\Scripts\python.exe` | the Python the apps start: `python.exe %LOCALAPPDATA%\CadBridge\cadbridge_mcp.py` (stdio) |
| `claude mcp list` | Claude Code: `cadbridge` should be `Connected` |
| `%LOCALAPPDATA%\Claude-Data\claude_*\claude_desktop_config.json` | Claude Desktop (current Microsoft Store builds); older builds: `%APPDATA%\Claude\claude_desktop_config.json`. Desktop logs: `%LOCALAPPDATA%\Claude\Logs\mcp-server-cadbridge.log` |
| `%USERPROFILE%\.codex\config.toml`, `.cursor\mcp.json`, `.gemini\settings.json`, `.gemini\config\mcp_config.json`, `%APPDATA%\Code\User\mcp.json` | the other apps |

Failure kinds returned by the tools (and logged): `cad_not_running` (start the CAD), `plugin_not_loaded` (the CAD runs
without the plugin: it was not restarted after the install, or the autoload is missing: run the installer again),
`several_cads` (`cad_use("acad")` or `cad_use("cadp")`), `cad_gone`, `interrupted` (the CAD closed or crashed during
the request), `pipe_unreachable`, `cad_error` (the CAD refused the request; the message says why). `timeout: CAD main
thread busy`: a dialog is open in the CAD or a command waits for input.

Claude chats on the web, on a phone or from another device do not get local MCP servers: use the Desktop app or a
CLI on the PC where the CAD runs.

## Using the tools

- Start with `cad_status`, then `cad_info` (units, extents, layers, blocks) before drawing anything.
- `cad_ops` runs a batch of ops in one undoable transaction: all-or-nothing. The user can `UNDO` it in the CAD.
  Prefer it to commands: its coordinates are WCS whatever the user's UCS, and it has 3D ops (`box`, `cylinder`,
  `extrude`, `union` / `subtract`, `face3d`, `poly3d`...).
- `cad_command` waits for the command to finish and returns `status`. `busy` means the CAD is already inside a
  command (often the user's): ask the user, or `cad_cancel`. A command still waiting for input at `timeout_s` is
  cancelled, so never send keystrokes (`^C`, ESC) as command text.
- `cad_lisp` returns values: read sysvars, tables, files with it instead of storing results in USERI / USERS
  variables and polling them. Set variables with it too (`(setvar "ORTHOMODE" 0)`): CAD&PILLAR refuses the `setvar`
  op. AutoLISP calls take at most 255 arguments.
- Do not save over the user's drawing unless asked: `cad_save` without a path saves in place.
- Drawings are often in millimetres: check `cad_info` units before giving sizes.

## Uninstall

`powershell -NoProfile -ExecutionPolicy Bypass -File .\installer.ps1 -Uninstall -Cads all -Clients all -Yes`
(the CAD must be closed). Without `-Yes` it asks whether to delete the plugin and Python environment too.

## Changing CadBridge (developers)

See `README.md` in the repository root and `CLAUDE.md`: build with `pwsh src/build.ps1`, commit the rebuilt `bin/`,
bump `VERSION` and `CHANGELOG.md` for every release, keep `installer.ps1` Windows PowerShell 5.1 and ASCII-only.
