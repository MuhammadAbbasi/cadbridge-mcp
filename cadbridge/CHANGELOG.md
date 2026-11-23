# CadBridge changelog

Versions follow `MAJOR.MINOR.PATCH`: MAJOR when an existing tool or op changes in a way that breaks callers,
MINOR for new tools, ops or supported CADs, PATCH for fixes. The number lives only in `VERSION`.

## 1.3.2 - 2026-11-23

- Installer: the Python download and the library install show uv's own progress bars and the packages installed,
  instead of running silently for a minute or more.

## 1.3.1 - 2026-11-23

- Installer: on a PC without Python, the install stopped at `Downloading cpython-3.12...` with a `NativeCommandError`.
  uv writes its progress to stderr, and Windows PowerShell 5.1 turned those lines into fatal errors. uv now runs with
  errors non-fatal and is judged by its exit code and the Python it leaves behind. Earlier test PCs had Python
  already, so the download never ran there.

## 1.3.0 - 2026-11-23

From the 23 Nov test session (a 3D house with PV on CAD&PILLAR 11): about 40 polling calls and 4 manual ESC presses.

- `cad_command` waits: the command line is polled until the CAD is idle again and the call returns `status`
  (`done` / `timeout` / `cancelled` / `busy`), `ms`, the active command and last prompt, and the handles created. A
  command still waiting for input at `timeout_s` is cancelled (`on_timeout="leave"` keeps it). A CAD already inside a
  command answers `busy` instead of receiving keystrokes. Jobs run one at a time. `wait=False` keeps the old queueing.
- New `cad_lisp`: evaluates AutoLISP and returns its value as JSON (lists, dotted pairs, enames and selection sets as
  handles, nil, T), or its error message. The LISP encodes the value itself and the plugin reads it back from a temp
  file: CAD&PILLAR does not register a plugin's .NET commands or LISP functions (measured: even `CADBRIDGE` is
  "Comando sconosciuto").
- Fix: LISP text sent to the command line is broken into lines of about 200 characters (at spaces outside strings and
  comments). CAD&PILLAR's LISP reader crashes on one long line: a 1,021-character line gave an access violation, then
  took the whole CAD down; the same text in short lines runs every time. Applies to `cad_lisp` and to `cad_command`
  text that starts with `(`.
- A job waits (up to 10 s) while a LISP or script runs without a command name, e.g. CAD&PILLAR's own `CDLoadMenu` in
  every new drawing; an open command still answers `busy` at once.
- New `cad_cancel`: ESC ESC, then waits until no command is active.
- `cad_ops` 3D ops, through the database (WCS whatever the UCS, no command prompt): `box`, `cylinder` (any axis,
  cone with `r2`), `sphere`, `extrude` (an outline or a closed curve), `union` / `subtract` / `intersect`, `face3d`,
  `poly3d`; each returns `{handle, min, max}`. `move` / `copy` take `dz`.
- `insert`: `z` and `xscale` / `yscale` / `zscale` (negative = mirrored). `get` / `list` report an insert's `scale`
  and a solid's `volume`.
- `cad_info` extents are recomputed (`UpdateExt`): erased objects no longer count.
- A missing linetype is loaded from the CAD's own files: CAD&PILLAR has `cadp.lin` / `cadpiso.lin`, not
  `acadiso.lin` (the `eFileSystemErr` of the test), with Italian names: `DASHED` is found as `TRATTEGGIATA`, and
  `BORDER`, `CENTER`, `DASHDOT`, `DIVIDE`, `DOT`, `HIDDEN`, `PHANTOM` (and their `2` / `X2` sizes) likewise.
- `setvar`: CAD&PILLAR refuses `SetSystemVariable` from a plugin for every variable (`eInvalidInput`; measured with and
  without the document lock and the transaction), so the op now fails with the `cad_lisp` call that works:
  `(setvar "ORTHOMODE" 0)`. The review's guess (value not coerced to the variable's type) was wrong.
- `src/live_check.py`: the live acceptance check (24 checks, run in a new drawing). 1.3.0 passed all 24 on
  CAD&PILLAR 11.
- The usage log records the first LISP function of `cad_lisp` / `cad_command` and a `command_end` event for every
  job that did not end `done`.

## 1.2.1 - 2026-11-23

- Installer: Claude Desktop from the Microsoft Store now gets the server. Its current builds read
  `%LOCALAPPDATA%\Claude-Data\claude_*\claude_desktop_config.json`, not the `%APPDATA%` or `Packages` copies.
- Installer: `-Clients found` configures the AI apps found on the PC (the interactive default) without questions.
- `AGENTS.md`: a guide for AI agents to understand CadBridge and install it for the user (non-interactive install,
  what each installer message means, verification, failure kinds). Users paste one line into their agent.

## 1.2.0 - 2026-11-23

- Usage log: the MCP server writes one JSON line per event to `%LOCALAPPDATA%\CadBridge\server.log` and, when the
  installer is given `-LogDir <folder>` (or `CADBRIDGE_LOG_DIR` is set), also to
  `<folder>\<user>\<user>_<YYYY-MM>.jsonl` (one folder per user; the PC is in every line). Events: `server_start` / `server_stop` (calls, errors, minutes),
  `client_connected` (the AI app), `connected` (CAD product and version, plugin version, `match`), `tool` (name, `ok`,
  `ms`, failure `kind` and message, a summary of what was done: op names, command verb, file extension; never drawing
  content or paths) and `cad_gone` (the CAD closed, `crashed` plus the exception code when Windows recorded an
  application error), plus `shared_log_unreachable`. Logging never fails a tool.
- Failures now have kinds: `cad_not_running`, `plugin_not_loaded` (the CAD runs but the plugin is not in it: new,
  with its own message), `several_cads`, `cad_gone`, `pipe_unreachable`, `interrupted` (no reply: the CAD closed or
  crashed mid-request), `cad_error` (the CAD refused the request).

## 1.1.0 - 2026-11-23

- Installer: two more AI apps. Google Antigravity (IDE and `agy` CLI) gets the server in
  `.gemini\config\mcp_config.json`, and also in the older `.gemini\antigravity\mcp_config.json` when that folder
  exists; the Gemini CLI in `.gemini\settings.json`. Other servers in those files are kept unchanged; uninstall
  removes only `cadbridge`.

## 1.0.0 - 2026-11-23

First numbered release (the same code as the 2026-11-18 builds, now stamped).

- Plugin `CadBridge.dll` in two builds: `bin/net8` (AutoCAD 2025-2026, CAD&PILLAR 11) and `bin/net48` (AutoCAD 2021-2024).
- MCP server `cadbridge`: `cad_status`, `cad_use`, `cad_info`, `cad_list`, `cad_ops`, `cad_command`, `cad_new`,
  `cad_open`, `cad_save`, `cad_screenshot`.
- Installer (`Install.cmd` / `Uninstall.cmd`): finds the CADs, configures the AI apps, downloads uv and Python when missing.
- Versioning: `VERSION` is stamped into both DLLs (also visible in Explorer > Properties > Details), reported by the
  plugin's `ping`, by `cad_status` (`version`, plus a `warning` when the plugin and the server differ) and by the installer.
