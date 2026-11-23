"""Live acceptance check for CadBridge: run with the CAD open (it works in a NEW drawing it creates, never in yours).

    %LOCALAPPDATA%\\CadBridge\\venv\\Scripts\\python.exe live_check.py [pipe name, when several CADs run]

Replays the failures of the 23 Nov 2026 test session (see CHANGELOG 1.3.0). Prints one line per check, exits 1 on a failure.
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cadbridge_mcp as m  # noqa: E402

if len(sys.argv) > 1:  # several CADs running: the pipe to test, e.g. cadbridge-cadp-1234
    m._target = sys.argv[1]

fails = 0


def check(name, ok, detail=""):
    global fails
    fails += not ok
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   {detail}"))


def ops(*o):
    return m.call({"op": "batch", "ops": list(o)})


def lisp(expr, t=10):
    return m.call({"op": "lisp", "expr": expr, "timeout_ms": int(t * 1000)})


def cmd(text, t=10, on_timeout="cancel"):
    return m.call({"op": "command", "text": text, "timeout_ms": int(t * 1000), "on_timeout": on_timeout})


ping = m.call({"op": "ping"})
check("plugin version = server version", ping.get("version") == m.__version__, f"{ping.get('version')} vs {m.__version__}")
print("  new test drawing:", m.call({"op": "new"}))

# First call in a fresh drawing, one 3,000-character line: crashed CAD&PILLAR's LISP reader before 1.3.0.
r = lisp("(length (quote (" + " ".join(["1"] * 1500) + ")))")
check("a long LISP line as the first call of a drawing", r.get("status") == "done" and r.get("value") == 1500, r)
r = cmd("(progn (setq cb:n (+ " + " ".join(["1"] * 1500) + ")) (princ))")
check("a long LISP line through cad_command", r.get("status") == "done", r)
r = lisp('(list 1 2.5 "a" nil T (list 1 2 3) (getvar "CLAYER"))')
check("cad_lisp returns values", r.get("status") == "done" and r.get("value") == [1, 2.5, "a", None, True, [1, 2, 3], "0"], r)
r = lisp("(/ 1 0)")
check("cad_lisp reports the error", r.get("status") == "error" and bool(r.get("error")) and "value" not in r, r)

try:
    ops({"op": "setvar", "name": "ORTHOMODE", "value": 1})
    sv = "set"
except Exception as e:  # noqa: BLE001 - CAD&PILLAR refuses it: the message must name the route that works
    sv = str(e)
check("setvar works, or names the cad_lisp route", sv == "set" or "cad_lisp" in sv, sv)
r = lisp('(setvar "ORTHOMODE" 0)')
check("setvar through cad_lisp", r.get("status") == "done" and r.get("value") == 0, r)

r = cmd("_.CIRCLE 0,0 5")
check("cad_command waits and reports the created entity", r.get("status") == "done" and r["created"]["count"] == 1, r)

r = cmd("_.LINE 0,0", t=2)  # one point only: LINE waits for the next one
check("a command left waiting for input is cancelled at the timeout", r.get("status") == "cancelled", r)
check("  ... and the CAD is idle afterwards", ops({"op": "getvar", "name": "CMDACTIVE"})[0] == "0")

r = cmd("_.LINE 0,0", t=1, on_timeout="leave")
check("on_timeout=leave keeps the command open", r.get("status") == "timeout", r)
r = cmd("_.CIRCLE 0,0 7")
check("a call into an open command answers busy (no keystrokes sent)", r.get("status") == "busy", r)
r = m.call({"op": "cancel"})
check("cad_cancel cancels it", r.get("status") == "cancelled", r)

res = []
th = [threading.Thread(target=lambda i=i: res.append(cmd(f"_.CIRCLE {100 * i},100 5"))) for i in range(4)]
[t.start() for t in th]
[t.join() for t in th]
check("4 concurrent commands run one after another", sorted(x.get("status") for x in res) == ["done"] * 4, res)

cmd("_.UCS _Z 30")  # rotated UCS: database ops must not care
b = ops({"op": "box", "min": [0, 0, 0], "max": [1000, 500, 300]})[0]
cmd("_.UCS _W")
check("box is placed in WCS under a rotated UCS", b["min"] == [0, 0, 0] and b["max"] == [1000, 500, 300], b)

o = ops({"op": "cylinder", "base": [0, 0, 0], "r": 100, "h": 500, "axis": [1, 0, 0]},
        {"op": "sphere", "center": [0, 0, 1000], "r": 50},
        {"op": "extrude", "points": [[0, 0], [400, 0], [400, 200], [0, 200]], "z": 100, "height": 50},
        {"op": "face3d", "points": [[0, 0, 0], [100, 0, 50], [100, 100, 50], [0, 100, 0]]},
        {"op": "poly3d", "points": [[0, 0, 0], [500, 0, -800], [500, 300, -800]]})
cyl, sph, ext = o[0], o[1], o[2]
check("cylinder along X", abs(cyl["max"][0] - 500) < 1e-6 and abs(cyl["max"][2] - 100) < 1e-6, cyl)
check("sphere", abs(sph["max"][2] - 1050) < 1e-6, sph)
check("extrude at elevation", ext["min"][2] == 100 and ext["max"][2] == 150, ext)
check("face3d / poly3d", bool(o[3]["handle"]) and o[4]["min"][2] == -800, o[3:])
cut = ops({"op": "box", "min": [100, 100, -10], "max": [200, 200, 400]})[0]
s = ops({"op": "subtract", "target": b["handle"], "tools": [cut["handle"]]})[0]
check("subtract: volume drops by the hole", abs(s["volume"] - (1000 * 500 * 300 - 100 * 100 * 300)) < 1, s)

ops({"op": "block_define", "name": "CB_TEST_DOOR", "entities": [{"op": "line", "x1": 0, "y1": 0, "x2": 900, "y2": 0}]})
ins = ops({"op": "insert", "name": "CB_TEST_DOOR", "x": 0, "y": 0, "z": 250, "xscale": -1})[0]
g = ops({"op": "get", "handle": ins})[0]
check("mirrored insert at z", g["scale"][0] == -1 and g["at"][2] == 250, g)

before = m.call({"op": "info"})
h = ops({"op": "circle", "cx": 90000, "cy": 90000, "r": 10})[0]
ops({"op": "delete", "handles": [h]})
after = m.call({"op": "info"})
check("extents ignore erased objects", after["extmax"] == before["extmax"], (before["extmax"], after["extmax"]))

r = lisp('(ssget "_X" (list (cons 0 "CIRCLE")))')
check("cad_lisp returns a selection set as handles", r.get("status") == "done" and len(r.get("value") or []) == 5, r)

try:
    ops({"op": "layer", "name": "CB_TEST_DASHED", "linetype": "DASHED"})
    check("linetype DASHED loads from the CAD's own .lin", True)
except Exception as e:  # noqa: BLE001
    check("linetype DASHED loads from the CAD's own .lin", False, e)

print(f"\n{'ALL PASSED' if not fails else f'{fails} FAILED'} - the test drawing is unsaved: close it without saving")
sys.exit(1 if fails else 0)
