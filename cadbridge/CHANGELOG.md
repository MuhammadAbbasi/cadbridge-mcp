# CadBridge changelog

Versions follow `MAJOR.MINOR.PATCH`: MAJOR when an existing tool or op changes in a way that breaks callers,
MINOR for new tools, ops or supported CADs, PATCH for fixes. The number lives only in `VERSION`.

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
