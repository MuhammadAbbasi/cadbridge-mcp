"""MCP server that edits the drawing open in AutoCAD 2025+ or CAD&PILLAR 11 live, as DWG (no DXF round trip).

It talks to the CadBridge plugin (src/CadBridge.cs, autoloaded inside each CAD) over the per-process named
pipe \\\\.\\pipe\\cadbridge-<exe>-<pid>; no COM, which some OEMs (CAD&PILLAR) disable.
Coordinates are drawing units (WCS), angles in degrees CCW.
"""
import atexit
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.utilities.types import Image

_HERE = Path(__file__).resolve().parent
_target: str | None = None  # pipe name chosen with cad_use; None = the only running CAD
CAD_EXES = ("acad.exe", "cadp.exe")


def _read_file(name: str, default: str | None = None) -> str | None:
    """A one-line setting file beside this script when installed, one folder up in the repository."""
    for f in (_HERE / name, _HERE.parent / name):
        if f.is_file():
            return f.read_text(encoding="utf-8-sig").strip() or default
    return default


__version__ = _read_file("VERSION", "unknown")


# ---------------- usage log ----------------
# One JSON line per event, always in %LOCALAPPDATA%\CadBridge\server.log and, when a shared folder is configured
# (CADBRIDGE_LOG_DIR, or the LOG_DIR file the installer writes beside this script), also in
# <LOG_DIR>\<user>\<user>_<YYYY-MM>.jsonl - one folder and one file per user (the PC is in every line).
# Logging never fails a tool. Arguments are summarised (op names, file extensions), never copied.

_SESSION = {"id": uuid.uuid4().hex[:8], "client": None, "started": time.time(), "calls": 0, "errors": 0}
_LOCAL_LOG = Path(os.environ.get("LOCALAPPDATA", str(_HERE))) / "CadBridge" / "server.log"
_shared_down = False


def _shared_log() -> Path | None:
    d = os.environ.get("CADBRIDGE_LOG_DIR") or _read_file("LOG_DIR")
    if not d:
        return None
    user = re.sub(r"[^\w.-]", "_", os.environ.get("USERNAME", "unknown"))
    return Path(d) / user / f"{user}_{datetime.now():%Y-%m}.jsonl"  # the PC is in every line


def log_event(event: str, **fields) -> None:
    global _shared_down
    rec = {"t": datetime.now().isoformat(timespec="seconds"), "event": event, "user": os.environ.get("USERNAME"),
           "pc": os.environ.get("COMPUTERNAME"), "session": _SESSION["id"], "version": __version__,
           "client": _SESSION["client"], **fields}
    line = json.dumps(rec, ensure_ascii=False, default=str) + "\n"
    try:
        _LOCAL_LOG.parent.mkdir(parents=True, exist_ok=True)
        if _LOCAL_LOG.exists() and _LOCAL_LOG.stat().st_size > 5_000_000:  # ponytail: one rollover, no rotation scheme
            _LOCAL_LOG.replace(_LOCAL_LOG.with_suffix(".old.log"))
        with _LOCAL_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    shared = _shared_log()
    if shared:
        try:
            shared.parent.mkdir(parents=True, exist_ok=True)
            with shared.open("a", encoding="utf-8") as f:
                f.write(line)
            if _shared_down:  # back after an outage
                _shared_down = False
        except OSError as e:
            if not _shared_down:
                _shared_down = True
                log_event("shared_log_unreachable", path=str(shared.parent.parent), error=str(e)[:200])


# ---------------- errors, by kind (the kind is what the log counts) ----------------
class CadBridgeError(RuntimeError):
    kind = "cad_error"  # the CAD answered with an error


class CadNotRunning(CadBridgeError):
    kind = "cad_not_running"


class PluginNotLoaded(CadBridgeError):
    kind = "plugin_not_loaded"


class SeveralCads(CadBridgeError):
    kind = "several_cads"


class CadGone(CadBridgeError):
    kind = "cad_gone"


class PipeUnreachable(CadBridgeError):
    kind = "pipe_unreachable"


class Interrupted(CadBridgeError):
    kind = "interrupted"


def _cad_processes() -> list[str]:
    """Names of running AutoCAD / CAD&PILLAR processes (to tell 'CAD closed' from 'plugin not loaded')."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({row.split('","')[0].strip('"').lower() for row in out.splitlines()} & set(CAD_EXES))


def _recent_crash(exe: str) -> str | None:
    """Exception code of an 'Application Error' event for `exe` in the last 10 minutes, else None."""
    q = "*[System[Provider[@Name='Application Error'] and TimeCreated[timediff(@SystemTime) <= 600000]]]"
    try:
        out = subprocess.run(["wevtutil", "qe", "Application", f"/q:{q}", "/f:text", "/rd:true", "/c:5"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for event in out.split("Event["):
        if exe.lower() in event.lower():
            m = re.search(r"0x[cC][0-9a-fA-F]{7}", event)
            return m.group(0).lower() if m else "unknown"
    return None


# ---------------- pipe ----------------
_seen: dict[str, dict] = {}  # pipe -> {"since": time, "cad": ping result}; for connected / cad_gone events


def pipes() -> list[str]:
    """Running CadBridge instances: pipe names like cadbridge-acad-1234 / cadbridge-cadp-5678."""
    return sorted(p for p in os.listdir("\\\\.\\pipe\\") if re.fullmatch(r"cadbridge-[a-z0-9_.]+-\d+", p, re.I))


def _note_gone(found: list[str]) -> None:
    for name in [p for p in _seen if p not in found]:
        info = _seen.pop(name)
        exe = name.split("-")[1] + ".exe"
        crash = _recent_crash(exe)
        log_event("cad_gone", pipe=name, connected_s=round(time.time() - info["since"]),
                  crashed=bool(crash), exception=crash)


def pipe_path() -> str:
    found = pipes()
    _note_gone(found)
    if _target:
        if _target not in found:
            raise CadGone(f"{_target} is no longer running; running: {found or 'none'} (use cad_use)")
        return "\\\\.\\pipe\\" + _target
    if not found:
        running = _cad_processes()
        if running:
            raise PluginNotLoaded(f"{', '.join(running)} is running but CadBridge is not loaded in it: run Install.cmd "
                                  "again (with the CAD closed), then restart the CAD")
        raise CadNotRunning("No CAD with CadBridge is running: start AutoCAD or CAD&PILLAR (the plugin loads at startup)")
    if len(found) > 1:
        raise SeveralCads(f"Several CADs are running: {found}. Pick one with cad_use.")
    return "\\\\.\\pipe\\" + found[0]


def _exchange(path: str, req: dict, retries: int = 20):
    """One request/response over the pipe. Retries while the pipe is busy."""
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
                raise PipeUnreachable(f"CadBridge pipe {path} not reachable: is the CAD still running?")
            time.sleep(0.1)
    if not line.endswith(b"\n"):  # not retried: the request may already have been applied
        raise Interrupted("CadBridge closed the connection without a reply (the CAD closed or crashed?); check the "
                          "drawing and %APPDATA%\\CadBridge\\cadbridge.log")
    resp = json.loads(line)
    if not resp["ok"]:
        raise CadBridgeError(resp["error"])
    return resp["result"]


def call(req: dict):
    path = pipe_path()
    name = path.rsplit("\\", 1)[1]
    if name not in _seen:  # first contact with this CAD: record who it is
        cad = _exchange(path, {"op": "ping"})
        _seen[name] = {"since": time.time(), "cad": cad}
        log_event("connected", pipe=name, cad=cad.get("product"), acadver=cad.get("acadver"),
                  plugin=cad.get("version"), match=cad.get("version") == __version__)
        if req.get("op") == "ping":
            return cad
    return _exchange(path, req)


# ---------------- per-call logging ----------------
def _summary(tool: str, args: dict) -> dict:
    """What a call did, without its content: op names for cad_ops, the command verb, file extensions."""
    if tool == "cad_ops":
        ops = args.get("ops") or []
        counts: dict[str, int] = {}
        for o in ops:
            k = str(o.get("op", "?")) if isinstance(o, dict) else "?"
            counts[k] = counts.get(k, 0) + 1
        return {"ops": counts}
    if tool in ("cad_command", "cad_lisp"):  # the command verb, or the first LISP function called
        m = re.search(r"[(]\s*(?:progn\s*[(]\s*)?([^\s()]+)|(\S+)", str(args.get("text") or args.get("expr") or ""))
        return {"command": ((m.group(1) or m.group(2)) if m else "?")[:40]}
    if tool in ("cad_open", "cad_save", "cad_new"):
        p = args.get("path") or args.get("template") or ""
        return {"ext": Path(p).suffix.lower() if p else None}
    return {}


class _UsageLog(Middleware):
    async def on_call_tool(self, context, call_next):
        if _SESSION["client"] is None:  # the AI app's name: known only once a request arrives (no handshake hook)
            try:
                info = context.fastmcp_context.session.client_params.client_info
                _SESSION["client"] = f"{info.name} {info.version or ''}".strip()
            except AttributeError:
                _SESSION["client"] = "unknown"
            log_event("client_connected")
        tool = context.message.name
        t0 = time.perf_counter()
        _SESSION["calls"] += 1
        try:
            result = await call_next(context)
        except Exception as e:  # noqa: BLE001 - logged, then re-raised unchanged
            _SESSION["errors"] += 1
            cause = e.__cause__ or e
            log_event("tool", tool=tool, ok=False, ms=round((time.perf_counter() - t0) * 1000),
                      kind=getattr(cause, "kind", type(cause).__name__), error=str(cause)[:300],
                      **_summary(tool, context.message.arguments or {}))
            raise
        log_event("tool", tool=tool, ok=True, ms=round((time.perf_counter() - t0) * 1000),
                  **_summary(tool, context.message.arguments or {}))
        return result


mcp = FastMCP("cadbridge", version=__version__)
mcp.add_middleware(_UsageLog())
log_event("server_start", python=sys.version.split()[0], shared_log=str(_shared_log() or ""))
atexit.register(lambda: log_event("server_stop", calls=_SESSION["calls"], errors=_SESSION["errors"],
                                  minutes=round((time.time() - _SESSION["started"]) / 60, 1)))


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
      {"op":"insert","name","x","y","z"?,"rotation"?,"scale"?,"xscale"?,"yscale"?,"zscale"?}  (block must exist;
         a negative xscale mirrors it, e.g. a left-hand door from a right-hand one)
      {"op":"block_define","name","entities":[create ops in block coords, base 0,0]}  (redefines in place)
      {"op":"block_import","path":"x.dwg","name"?,"overwrite"?}  -> {name, min, max, ...}: a DWG as a block
      {"op":"block_info","name"}  -> size/content of a block (use min/max to pick an insert scale)
      {"op":"get","handle"}  {"op":"delete","handles":[...]}  {"op":"set","handle","layer"?,"color"?,"text"?}
      {"op":"move"|"copy","handles":[...],"dx","dy","dz"?}  {"op":"rotate","handles":[...],"angle","bx","by"}  (about Z)
    3D (always WCS, whatever the current UCS; each returns {handle, min, max}; points are [x,y] or [x,y,z]):
      {"op":"box","min":[x,y,z],"max":[x,y,z],"rotation"? (degrees about Z, around min)}
      {"op":"cylinder","base":[x,y,z],"r","h","axis"? ([0,0,1]),"r2"? (top radius: a cone)}
      {"op":"sphere","center":[x,y,z],"r"}
      {"op":"extrude","points":[[x,y],...],"z"? (elevation),"height"} or {"op":"extrude","handle","height","erase_profile"?}
      {"op":"union"|"subtract"|"intersect","target":handle,"tools":[handles]}  (tools are consumed; returns the target)
      {"op":"face3d","points":[3 or 4 [x,y,z]]}  {"op":"poly3d","points":[[x,y,z],...],"closed"?}
    get/list report "volume" for solids and "scale" [x,y,z] for inserts.
      {"op":"getvar","name"}  {"op":"setvar","name","value"}  {"op":"info"}  {"op":"list",...}
    Linetypes missing from the drawing are loaded from the CAD's own .lin files (acad/acadiso, cadp/cadpiso; DASHED etc.
    are found under their Italian names in CAD&PILLAR). CAD&PILLAR refuses setvar from the plugin: use cad_lisp
    '(setvar "NAME" value)' there.
    """
    return call({"op": "batch", "ops": ops})


def _job(req: dict) -> dict:
    res = call(req)
    if res.get("status") not in ("done", "idle"):  # timeouts, cancels, busy: worth seeing in the usage log
        log_event("command_end", status=res.get("status"), command=res.get("command"), ms=res.get("ms"))
    return res


@mcp.tool
def cad_command(text: str, timeout_s: float = 30, on_timeout: str = "cancel", wait: bool = True) -> dict | str:
    """Run a command line string in the active drawing (any AutoCAD / CAD&PILLAR command, e.g. "_.ZOOM _E") and
    WAIT until the CAD is idle again. Returns {status: done|timeout|cancelled|busy, ms, command, prompt,
    created: {count, handles}}. A command still waiting for input at timeout_s is cancelled (on_timeout="cancel")
    or left open ("leave"). "busy": the CAD was already inside a command (the user's?): cad_cancel or ask the user.
    Prefer cad_ops (exact, WCS, one undo step) and cad_lisp (returns values); wait=False only queues it."""
    if not wait:
        return call({"op": "command", "text": text, "async": True})
    return _job({"op": "command", "text": text, "timeout_ms": int(timeout_s * 1000), "on_timeout": on_timeout})


@mcp.tool
def cad_lisp(expr: str, timeout_s: float = 30) -> dict:
    """Evaluate AutoLISP in the active drawing and return its value as JSON: {status: done|error|timeout|cancelled|busy,
    value, error, created, ms}. expr may hold several forms (the last one's value is returned). Lists -> arrays,
    points -> [x,y,z], enames and selection sets -> handles, nil -> null, T -> true. Use it to read anything
    (sysvars, tables, dictionaries, files) instead of writing USERI/USERS variables and polling."""
    return _job({"op": "lisp", "expr": expr, "timeout_ms": int(timeout_s * 1000)})


@mcp.tool
def cad_cancel() -> dict:
    """Cancel whatever command is active in the CAD (ESC ESC) and wait until it is idle:
    {status: idle (nothing was running) | cancelled | still_busy, cancelled: <command names>}."""
    return _job({"op": "cancel"})


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
        call({"op": "command", "text": "_.ZOOM _E", "timeout_ms": 15000})
        time.sleep(0.3)  # the zoom has finished; let the window repaint
    return Image(data=screenshot_png(), format="png")


if __name__ == "__main__":
    mcp.run()
