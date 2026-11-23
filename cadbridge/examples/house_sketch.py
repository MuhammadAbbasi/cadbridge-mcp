"""A 2-bedroom house on a plot plan, drawn live through the cadbridge MCP (no DXF).

Walls 150 mm (15 cm), doors 600 mm. Drawing units: mm. Plan origin (X0, Y0) keeps >= ~0.7 m from the
plot's curved rear boundary and 1 m from the front edge (y = 2952).
Run with the CadBridge venv python while the CAD has the plot drawing open (or pass --open).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cadbridge_mcp  # noqa: E402
from fastmcp import Client  # noqa: E402

X0, Y0 = 2500.0, 3950.0  # house origin on the plot
T = 150.0  # wall thickness
DOOR = 600.0  # door leaf
ops: list[dict] = []


def P(x, y):
    return [X0 + x, Y0 + y]


# ---------------- layers ----------------
for name, color, lw in [
    ("A-WALL", 8, 50),
    ("A-DOOR", 1, 18),
    ("A-GLAZ", 4, 18),
    ("A-FURN", 9, 13),
    ("A-ANNO-TEXT", 2, 18),
    ("A-ANNO-DIMS", 3, 13),
    ("A-SITE-PATH", 30, 13),
]:
    ops.append({"op": "layer", "name": name, "color": color, "lineweight": lw})

# ---------------- walls: (x1, y1, x2, y2, [(a, b) gaps along the long axis]) ----------------
# Segments butt against each other (never overlap), so the solid poche shows no joint lines.
doors, windows = [], []
walls = [
    (0, 0, 12200, T, [(3000, 3600), (5100, 6300), (7800, 9000), (9400, 10000)]),  # south
    (12050, T, 12200, 3450, [(900, 2700)]),  # east (wing)
    (7200, 3450, 12200, 3600, [(9000, 10800)]),  # north (wing)
    (7050, 3450, 7200, 5850, []),  # east (main, rear)
    (0, 5850, 7200, 6000, [(4600, 5800)]),  # north (main)
    (0, T, T, 5850, [(900, 1500), (3900, 5100)]),  # west
    (T, 2850, 7050, 3000, [(2580, 3180), (3500, 4100)]),  # bedrooms / day zone
    (3225, 3000, 3375, 5850, []),  # bedroom 1 | bedroom 2
    (2400, T, 2550, 2850, [(1950, 2550)]),  # bath | hall
    (4200, T, 4350, 2850, [(1950, 2550)]),  # hall | kitchen
    (7050, T, 7200, 3450, [(600, 1800)]),  # kitchen | living (open)
]


def solid(x1, y1, x2, y2):
    if x2 - x1 > 1 and y2 - y1 > 1:
        ops.append(
            {
                "op": "hatch",
                "layer": "A-WALL",
                "pattern": "SOLID",
                "points": [P(x1, y1), P(x2, y1), P(x2, y2), P(x1, y2)],
            }
        )


for x1, y1, x2, y2, gaps in walls:
    horiz = (x2 - x1) >= (y2 - y1)
    lo, hi = (x1, x2) if horiz else (y1, y2)
    cur = lo
    for a, b in sorted(gaps):
        solid(*((cur, y1, a, y2) if horiz else (x1, cur, x2, a)))
        cur = b
    solid(*((cur, y1, hi, y2) if horiz else (x1, cur, x2, hi)))

# ---------------- windows: three lines across each glazed gap ----------------
for x1, y1, x2, y2, (a, b) in [
    (0, 0, 12200, T, (5100, 6300)),
    (0, 0, 12200, T, (7800, 9000)),
    (12050, T, 12200, 3450, (900, 2700)),
    (7200, 3450, 12200, 3600, (9000, 10800)),
    (0, 5850, 7200, 6000, (4600, 5800)),
    (0, T, T, 5850, (900, 1500)),
    (0, T, T, 5850, (3900, 5100)),
]:
    horiz = (x2 - x1) >= (y2 - y1)
    for f in (0.0, 0.5, 1.0):
        if horiz:
            y = y1 + f * (y2 - y1)
            ops.append(
                {
                    "op": "line",
                    "layer": "A-GLAZ",
                    "x1": X0 + a,
                    "y1": Y0 + y,
                    "x2": X0 + b,
                    "y2": Y0 + y,
                }
            )
        else:
            x = x1 + f * (x2 - x1)
            ops.append(
                {
                    "op": "line",
                    "layer": "A-GLAZ",
                    "x1": X0 + x,
                    "y1": Y0 + a,
                    "x2": X0 + x,
                    "y2": Y0 + b,
                }
            )


# ---------------- doors: leaf + quarter-circle swing ----------------
def door(wall, a, b, hinge, side):
    """wall=(x1,y1,x2,y2); gap a..b along it; hinge 'a'|'b'; side +1 swings to +y/+x, -1 to -y/-x."""
    x1, y1, x2, y2 = wall
    horiz = (x2 - x1) >= (y2 - y1)
    h = a if hinge == "a" else b
    if horiz:
        face = y2 if side > 0 else y1
        hx, hy = h, face
        leaf_ang = 90 if side > 0 else 270
        closed_ang = 0 if hinge == "a" else 180
        lx, ly = hx, hy + side * DOOR
    else:
        face = x2 if side > 0 else x1
        hx, hy = face, h
        leaf_ang = 0 if side > 0 else 180
        closed_ang = 90 if hinge == "a" else 270
        lx, ly = hx + side * DOOR, hy
    s, e = (leaf_ang, closed_ang) if (closed_ang - leaf_ang) % 360 == 90 else (closed_ang, leaf_ang)
    ops.append(
        {
            "op": "line",
            "layer": "A-DOOR",
            "x1": X0 + hx,
            "y1": Y0 + hy,
            "x2": X0 + lx,
            "y2": Y0 + ly,
        }
    )
    ops.append(
        {
            "op": "arc",
            "layer": "A-DOOR",
            "cx": X0 + hx,
            "cy": Y0 + hy,
            "r": DOOR,
            "start": s,
            "end": e,
        }
    )


door((0, 0, 12200, T), 3000, 3600, "a", +1)  # entrance -> hall
door((0, 0, 12200, T), 9400, 10000, "b", +1)  # living -> garden
door((2400, T, 2550, 2850), 1950, 2550, "b", -1)  # hall -> bath
door((4200, T, 4350, 2850), 1950, 2550, "b", +1)  # hall -> kitchen
door((T, 2850, 7050, 3000), 2580, 3180, "b", +1)  # hall -> bedroom 1
door((T, 2850, 7050, 3000), 3500, 4100, "a", +1)  # hall -> bedroom 2


# ---------------- furniture ----------------
def rect(x1, y1, x2, y2, layer="A-FURN"):
    ops.append(
        {
            "op": "pline",
            "layer": layer,
            "closed": True,
            "points": [P(x1, y1), P(x2, y1), P(x2, y2), P(x1, y2)],
        }
    )


rect(900, 3950, 2300, 5850)
rect(1000, 5500, 2200, 5800)  # bedroom 1: double bed 1400x1900 + pillows
rect(5000, 3850, 6600, 5850)
rect(5100, 5500, 6500, 5800)  # bedroom 2: double bed 1600x2000 + pillows
rect(3375, 4000, 3975, 5850)  # bedroom 2: wardrobe
rect(T, 1950, 1050, 2850)
ops.append(
    {
        "op": "line",
        "layer": "A-FURN",
        "x1": X0 + T,
        "y1": Y0 + 1950,
        "x2": X0 + 1050,
        "y2": Y0 + 2850,
    }
)  # shower
rect(1550, T, 1950, 800)  # WC
rect(300, T, 800, 550)  # basin
rect(4350, T, 7050, 750)
rect(5000, 2250, 7050, 2850)  # kitchen counters
rect(6350, 2150, 7050, 2850)  # fridge
for cx, cy in [(6450, 350), (6750, 350), (6450, 600), (6750, 600)]:  # hob
    ops.append({"op": "circle", "layer": "A-FURN", "cx": X0 + cx, "cy": Y0 + cy, "r": 90})
rect(5400, 250, 6000, 650)  # sink
rect(11150, 700, 12000, 2700)  # sofa
rect(9900, 1200, 10500, 2200)  # coffee table
rect(7800, 2300, 9400, 3200)  # dining table 1600x900
for cx in (8200, 9000):  # dining chairs
    ops.append({"op": "circle", "layer": "A-FURN", "cx": X0 + cx, "cy": Y0 + 2050, "r": 200})

# ---------------- site path from the plot front edge to the entrance ----------------
rect(3000, -998, 3600, 0, "A-SITE-PATH")


# ---------------- room labels with interior areas ----------------
def label(name, x1, y1, x2, y2, at=None):
    area = (x2 - x1) * (y2 - y1) / 1e6
    tx, ty = at or ((x1 + x2) / 2, (y1 + y2) / 2)
    ops.append(
        {
            "op": "text",
            "layer": "A-ANNO-TEXT",
            "x": X0 + tx,
            "y": Y0 + ty,
            "height": 170,
            "text": f"{name}\\P{area:.1f} m²",
            "attach": 5,
        }
    )


label("BEDROOM 1", T, 3000, 3225, 5850)
label("BEDROOM 2", 3375, 3000, 7050, 5850)
label("BATH", T, T, 2400, 2850)
label("HALL", 2550, T, 4200, 2850)
label("KITCHEN", 4350, T, 7050, 2850)
label("LIVING / DINING", 7200, T, 12050, 3450, at=(8600, 1000))  # clear of the furniture
ops.append(
    {
        "op": "text",
        "layer": "A-ANNO-TEXT",
        "x": X0 + 6100,
        "y": Y0 - 1600,
        "height": 300,
        "text": "2-BEDROOM HOUSE  -  walls 15 cm, doors 60 cm",
        "attach": 5,
    }
)

# ---------------- dimensions ----------------
for d in [
    (0, 0, 12200, 0, 6100, -600),
    (0, 0, 0, 6000, -700, 3000),
    (12200, 0, 12200, 3600, 12900, 1800),
    (0, 6000, 7200, 6000, 3600, 6600),
    (7200, 3600, 12200, 3600, 9700, 4200),
]:
    x1, y1, x2, y2, dx, dy = d
    ops.append(
        {
            "op": "dim",
            "layer": "A-ANNO-DIMS",
            "scale": 50,
            "x1": X0 + x1,
            "y1": Y0 + y1,
            "x2": X0 + x2,
            "y2": Y0 + y2,
            "dx": X0 + dx,
            "dy": Y0 + dy,
        }
    )


async def main():
    async with Client(cadbridge_mcp.mcp) as c:
        if "--open" in sys.argv:
            await c.call_tool("cad_open", {"path": sys.argv[sys.argv.index("--open") + 1]})
        res = await c.call_tool("cad_ops", {"ops": ops})
        handles = (
            res.structured_content["result"]
            if isinstance(res.structured_content, dict)
            else res.data
        )
        print(f"{len(ops)} ops applied in one transaction, {len(handles)} results")
        await c.call_tool("cad_command", {"text": "_.ZOOM _E"})


if __name__ == "__main__":
    asyncio.run(main())
