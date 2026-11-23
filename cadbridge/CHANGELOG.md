# CadBridge changelog

Versions follow `MAJOR.MINOR.PATCH`: MAJOR when an existing tool or op changes in a way that breaks callers,
MINOR for new tools, ops or supported CADs, PATCH for fixes. The number lives only in `VERSION`.

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
