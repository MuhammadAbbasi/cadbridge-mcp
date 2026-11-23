"""MCP server that edits the drawing open in AutoCAD 2025+ or CAD&PILLAR 11 live, as DWG (no DXF round trip).

It talks to the CadBridge plugin (src/CadBridge.cs, autoloaded inside each CAD) over the per-process named
pipe \\\\.\\pipe\\cadbridge-<exe>-<pid>; no COM, which some OEMs (CAD&PILLAR) disable.
Coordinates are drawing units (WCS), angles in degrees CCW.
"""
import json
import os
import re
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.utilities.types import Image

_target: str | None = None  # pipe name chosen with cad_use; None = the only running CAD


def pipes() -> list[str]:
    """Running CadBridge instances: pipe names like cadbridge-acad-1234 / cadbridge-cadp-5678."""
    return sorted(p for p in os.listdir("\\\\.\\pipe\\") if re.fullmatch(r"cadbridge-[a-z0-9_.]+-\d+", p, re.I))


def pipe_path() -> str:
    found = pipes()
    if _target:
        if _target not in found:
            raise RuntimeError(f"{_target} is no longer running; running: {found or 'none'} (use cad_use)")
        return "\\\\.\\pipe\\" + _target
    if not found:
        raise RuntimeError("No CAD with CadBridge is running: start AutoCAD or CAD&PILLAR (the plugin loads at startup)")
    if len(found) > 1:
        raise RuntimeError(f"Several CADs are running: {found}. Pick one with cad_use.")
    return "\\\\.\\pipe\\" + found[0]


def call(req: dict, retries: int = 20):
    """One request/response over the pipe. Retries while the pipe is busy."""
    path = pipe_path()
    for i in range(retries):
        try:
            with open(path, "r+b", buffering=0) as f:
                f.write((json.dumps(req) + "\n").encode("utf-8"))
                line, empty = b"", 0
                while not line.endswith(b"\n") and empty < 50:
                    chunk = f.read(65536)
                    if chunk:
                        line += chunk
                    else:  # zero-byte read; a real EOF keeps returning b""
                        empty += 1
                        time.sleep(0.01)
            break
        except OSError:  # pipe busy, or briefly absent while the server re-creates it
            if i == retries - 1:
                raise RuntimeError(f"CadBridge pipe {path} not reachable: is the CAD still running?")
            time.sleep(0.1)
    if not line.endswith(b"\n"):  # not retried: the request may already have been applied
        raise RuntimeError("CadBridge closed the connection without a reply; check the drawing and %APPDATA%\\CadBridge\\cadbridge.log")
    resp = json.loads(line)
    if not resp["ok"]:
        raise RuntimeError(resp["error"])
    return resp["result"]


def _read_version() -> str:
    """VERSION sits beside this file when installed, one folder up in the repository."""
    here = Path(__file__).resolve().parent
    for f in (here / "VERSION", here.parent / "VERSION"):
        if f.is_file():
            return f.read_text(encoding="utf-8").strip()
    return "unknown"


__version__ = _read_version()
mcp = FastMCP("cadbridge", version=__version__)


@mcp.tool
def cad_status() -> dict:
    """Running CADs (AutoCAD / CAD&PILLAR) with CadBridge, which one is targeted, and its open drawings."""
    running = pipes()
    out = {"version": __version__, "running": running, "target": _target or (running[0] if len(running) == 1 else None)}
    if out["target"]:
        out["cad"] = call({"op": "ping"})
        plugin = out["cad"].get("version")
        if plugin != __version__:
            out["warning"] = (f"plugin {plugin or '< 1.0.0'} != server {__version__}: run Install.cmd again, "
                              "then restart the CAD")
        out["docs"] = call({"op": "docs"})
    return out


@mcp.tool
def cad_use(name: str) -> dict:
    """Target one running CAD when several run: a pipe name from cad_status, or just 'acad' / 'cadp'."""
    global _target
    match = [p for p in pipes() if p == name or p.split("-")[1] == name.lower()]
    if len(match) != 1:
        raise RuntimeError(f"'{name}' matches {match or 'nothing'}; running: {pipes()}")
    _target = match[0]
    return {"target": _target, "cad": call({"op": "ping"})}


@mcp.tool
def cad_info() -> dict:
    """Active drawing: name, units, entity count, extents, layers."""
    return call({"op": "info"})


@mcp.tool
def cad_list(layer: str | None = None, type: str | None = None, limit: int = 500) -> list:
    """Model-space entities (handle, DXF type, layer, extents, geometry). Filter by layer / DXF type (LINE, LWPOLYLINE, ...)."""
    req = {"op": "list", "limit": limit}
    if layer:
        req["layer"] = layer
    if type:
        req["type"] = type
    return call(req)


@mcp.tool
def cad_ops(ops: list[dict]) -> list:
    """Run edit operations on the live drawing in ONE transaction (all or nothing). Returns one result per op
    (new entity handle for creates). Every create accepts optional "layer" (created if missing) and "color" (ACI).

    ops:
      {"op":"layer","name","color"?,"lineweight"? (1/100 mm, 35=0.35),"linetype"?}
      {"op":"line","x1","y1","x2","y2"}
      {"op":"pline","points":[[x,y] or [x,y,bulge],...],"closed"?}
      {"op":"circle","cx","cy","r"}   {"op":"arc","cx","cy","r","start","end"}  (degrees, CCW)
      {"op":"text","x","y","height","text","rotation"?,"width"?,"attach"? (5=middle centre, 1=top left)}
      {"op":"dim","x1","y1","x2","y2","dx","dy","scale"?}  aligned dimension through (dx,dy); scale = DIMSCALE (e.g. 50 for 1:50 in mm)
      {"op":"hatch","points":[[x,y],...],"pattern"? ("SOLID"),"scale"?}
      {"op":"insert","name","x","y","rotation"?,"scale"?}  (block must exist in the drawing)
      {"op":"block_define","name","entities":[create ops in block coords, base 0,0]}  (redefines in place)
      {"op":"block_import","path":"x.dwg","name"?,"overwrite"?}  -> {name, min, max, ...}: a DWG as a block
      {"op":"block_info","name"}  -> size/content of a block (use min/max to pick an insert scale)
      {"op":"get","handle"}  {"op":"delete","handles":[...]}  {"op":"set","handle","layer"?,"color"?,"text"?}
      {"op":"move"|"copy","handles":[...],"dx","dy"}  {"op":"rotate","handles":[...],"angle","bx","by"}
      {"op":"getvar","name"}  {"op":"setvar","name","value"}  {"op":"info"}  {"op":"list",...}
    """
    return call({"op": "batch", "ops": ops})


@mcp.tool
def cad_command(text: str) -> str:
    """Queue a command line string in the active drawing (any AutoCAD / CAD&PILLAR command, e.g. "_.ZOOM _E").
    Asynchronous: it runs after this returns; read results back with cad_list / cad_info."""
    return call({"op": "command", "text": text})


@mcp.tool
def cad_open(path: str, read_only: bool = False) -> str:
    """Open a DWG in the CAD (or activate it if already open)."""
    return call({"op": "open", "path": path, "read_only": read_only})


@mcp.tool
def cad_new(template: str = "") -> str:
    """Create a new drawing (optionally from a .dwt template) and make it active."""
    return call({"op": "new", "template": template})


@mcp.tool
def cad_save(path: str | None = None) -> str:
    """Save the active drawing as DWG (in place, or to `path`)."""
    return call({"op": "save", "path": path} if path else {"op": "save"})


def screenshot_png(path: str | None = None, max_px: int = 1600) -> bytes:
    """PNG of the targeted CAD's main window, captured with PrintWindow (no focus change)."""
    import ctypes
    import io

    import win32gui
    import win32process
    import win32ui
    from PIL import Image as PILImage

    pid = int(pipe_path().rsplit("-", 1)[1])
    found = []  # (area, hwnd) of the process's visible titled top-level windows; the main frame is the largest

    def visit(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) and win32process.GetWindowThreadProcessId(h)[1] == pid:
            l, t, r, b = win32gui.GetWindowRect(h)
            found.append(((r - l) * (b - t), h))

    win32gui.EnumWindows(visit, None)
    if not found:
        raise RuntimeError(f"no window found for CAD process {pid}")
    hwnd = max(found)[1]
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    try:
        ctypes.windll.user32.PrintWindow(hwnd, mem.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        info = bmp.GetInfo()
        img = PILImage.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True), "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(hwnd, hdc)
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    if path:
        Path(path).write_bytes(buf.getvalue())
    return buf.getvalue()


@mcp.tool
def cad_screenshot(zoom_extents: bool = True) -> Image:
    """Image of the CAD window, to show the user the current drawing (e.g. a sketch to accept)."""
    if zoom_extents:
        call({"op": "command", "text": "_.ZOOM _E"})
        time.sleep(1.0)  # the command is queued; let it run
    return Image(data=screenshot_png(), format="png")


if __name__ == "__main__":
    mcp.run()
