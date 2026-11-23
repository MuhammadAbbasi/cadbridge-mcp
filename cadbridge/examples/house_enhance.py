"""Stage 2 of the house (after the sketch was accepted): real blocks for fixtures and furniture,
CADPILLAR's own 2D tree symbol, shrubs, a garden bench, lawn hatch following the curved plot edge,
paved terrace and entrance path. Live through the cadbridge MCP; units mm.
"""

import asyncio
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cadbridge_mcp  # noqa: E402
from fastmcp import Client  # noqa: E402

X0, Y0 = 2500.0, 3950.0  # house origin (same as the sketch)
PLOT_X0, PLOT_X1, PLOT_Y0 = 1517.0966564987903, 23526.301567081922, 2952.4628224087282
ALBERO = (
    r"C:\Program Files\CADPILLAR 11.0\Blocks\ALBERO.DWG"  # CADPILLAR's 2D tree, radius 1.727 (m)
)


def top(x):
    """Plot rear boundary y(x): arc 2F9, then arc 2FA (read from the drawing), then straight."""
    if x <= 10173.162247:
        cx, cy, r = 5816.843823755044, 4928.002765908923, 6889.718427773488
        return cy + math.sqrt(r * r - (x - cx) ** 2)
    if x <= 20564.32734968037:
        cx, cy, r = 15340.67847975209, 16673.63078354778, 8231.95987847786
        return cy - math.sqrt(r * r - (x - cx) ** 2)
    return 10311.346922813012


def rect(x1, y1, x2, y2, **kw):
    return {"op": "pline", "closed": True, "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]], **kw}


def line(x1, y1, x2, y2):
    return {"op": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2}


def circle(cx, cy, r):
    return {"op": "circle", "cx": cx, "cy": cy, "r": r}


def rounded_bowl(w, y0, y1):
    """Closed outline: straight back at y0, semicircular front reaching y1 (+y)."""
    h = w / 2
    return {
        "op": "pline",
        "closed": True,
        "points": [[-h, y0], [h, y0], [h, y1 - h, 1.0], [-h, y1 - h]],
    }


def bed(w, length):
    """Base point = head centre against the wall; the bed extends to -y."""
    pw = (w - 300) / 2
    return [
        rect(-w / 2, -length, w / 2, 0),
        rect(-w / 2 + 100, -380, -w / 2 + 100 + pw, -80),
        rect(w / 2 - 100 - pw, -380, w / 2 - 100, -80),
        line(-w / 2, -650, w / 2, -650),
        line(w / 2 - 350, -650, w / 2, -1000),
    ]


BLOCKS = {
    # bathroom (base = back centre on the wall face, fixture extends to +y)
    "WC": [rect(-200, 0, 200, 180), rounded_bowl(340, 180, 620), rounded_bowl(260, 230, 580)],
    "BASIN": [
        rect(-250, 0, 250, 420),
        rounded_bowl(360, 80, 360),
        circle(0, 40, 18),
        circle(0, 200, 15),
    ],
    "SHOWER_900": [
        rect(0, 0, 900, 900),
        rect(50, 50, 850, 850),
        line(50, 50, 850, 850),
        line(50, 850, 850, 50),
        circle(450, 450, 40),
    ],
    # kitchen (base = centre)
    "KITCHEN_SINK": [
        rect(-400, -250, 400, 250),
        rect(-370, -200, -30, 200),
        rect(30, -200, 370, 200),
        circle(-200, 0, 25),
        circle(200, 0, 25),
    ],
    "HOB": [
        rect(-300, -300, 300, 300),
        circle(-150, -150, 90),
        circle(150, -150, 70),
        circle(-150, 150, 70),
        circle(150, 150, 100),
    ],
    "FRIDGE": [rect(-350, -350, 350, 350), rect(-320, -320, 320, 320), line(-320, 250, 320, 250)],
    # bedrooms
    "BED_1400x1900": bed(1400, 1900),
    "BED_1600x2000": bed(1600, 2000),
    "NIGHTSTAND": [rect(-225, -200, 225, 200), circle(0, 0, 120), circle(0, 0, 40)],
    "WARDROBE_600x1600": [rect(-300, -800, 300, 800), line(0, -760, 0, 760)]
    + [line(-180, y - 60, 180, y + 60) for y in range(-600, 601, 200)],
    # living (sofa base = back centre, seat towards -y)
    "SOFA_3": [
        rect(-1000, -850, 1000, 0),
        rect(-1000, -200, 1000, 0),
        rect(-1000, -850, -800, -200),
        rect(800, -850, 1000, -200),
        rect(-800, -800, -267, -200),
        rect(-267, -800, 267, -200),
        rect(267, -800, 800, -200),
    ],
    "COFFEE_TABLE": [rect(-300, -500, 300, 500), rect(-260, -460, 260, 460)],
    # table 1200x800 + 4 chairs (450 seat, 60 back) on the long sides
    "DINING_4": [
        rect(-600, -400, 600, 400),
        rect(-525, 425, -75, 875),
        rect(-525, 815, -75, 875),
        rect(75, 425, 525, 875),
        rect(75, 815, 525, 875),
        rect(-525, -875, -75, -425),
        rect(-525, -875, -75, -815),
        rect(75, -875, 525, -425),
        rect(75, -875, 525, -815),
    ],
    # garden
    "BENCH": [rect(-900, -225, 900, 225), line(-900, -75, 900, -75), line(-900, 75, 900, 75)],
    "SHRUB": [
        {
            "op": "pline",
            "closed": True,
            "points": [
                [350 * math.cos(math.radians(a)), 350 * math.sin(math.radians(a)), 0.45]
                for a in range(0, 360, 45)
            ],
        },
        circle(0, 0, 60),
    ],
}

ops: list[dict] = [
    {"op": "layer", "name": n, "color": c, "lineweight": lw}
    for n, c, lw in [
        ("A-FURN", 9, 13),
        ("A-FIXT", 150, 13),
        ("L-PLNT", 3, 18),
        ("L-LAWN", 84, 9),
        ("L-PAVE", 33, 9),
        ("L-SITE", 30, 13),
    ]
]
ops += [{"op": "block_define", "name": n, "entities": ents} for n, ents in BLOCKS.items()]
ops.append({"op": "block_import", "path": ALBERO, "name": "TREE_CP"})


def ins(name, x, y, rot=0, scale=1, layer="A-FURN"):
    ops.append(
        {
            "op": "insert",
            "name": name,
            "x": X0 + x,
            "y": Y0 + y,
            "rotation": rot,
            "scale": scale,
            "layer": layer,
        }
    )


# ---- bathroom (interior x 150..2400, y 150..2850) ----
ins("SHOWER_900", 150, 1950, layer="A-FIXT")
ins("WC", 1750, 150, layer="A-FIXT")
ins("BASIN", 700, 150, layer="A-FIXT")
# ---- kitchen (x 4350..7050, y 150..2850): counters + appliances ----
ops.append(rect(X0 + 4350, Y0 + 150, X0 + 7050, Y0 + 750, layer="A-FURN"))
ops.append(rect(X0 + 5000, Y0 + 2250, X0 + 6350, Y0 + 2850, layer="A-FURN"))
ins("KITCHEN_SINK", 5700, 450, layer="A-FIXT")
ins("HOB", 6650, 450, layer="A-FIXT")
ins("FRIDGE", 6700, 2500, layer="A-FIXT")
# ---- bedroom 1 (x 150..3225, y 3000..5850) ----
ins("BED_1400x1900", 1600, 5850)
ins("NIGHTSTAND", 650, 5650)
ins("NIGHTSTAND", 2550, 5650)
ins("WARDROBE_600x1600", 2925, 4500)
# ---- bedroom 2 (x 3375..7050, y 3000..5850) ----
ins("BED_1600x2000", 5800, 5850)
ins("NIGHTSTAND", 4750, 5650)
ins("NIGHTSTAND", 6825, 5650)
ins("WARDROBE_600x1600", 3675, 4900)
# ---- living / dining (x 7200..12050, y 150..3450) ----
ins("SOFA_3", 12050, 1700, rot=270)
ins("COFFEE_TABLE", 10200, 1700)
ins("DINING_4", 8500, 2550)

# ---- site: paving, lawn zones (never overlapping the house, path or terrace), plants ----
PATH = (5500, 6100)  # entrance path x-range (abs), front edge -> house
TERRACE = (11300, 13300)  # terrace x-range (abs), in front of the patio door
HOUSE_Y = Y0  # front face of the house (abs)
ops.append(
    {
        "op": "hatch",
        "layer": "L-PAVE",
        "pattern": "ANSI37",
        "scale": 20,
        "keep_boundary": True,
        "points": [[PATH[0], PLOT_Y0], [PATH[1], PLOT_Y0], [PATH[1], HOUSE_Y], [PATH[0], HOUSE_Y]],
    }
)
ops.append(
    {
        "op": "hatch",
        "layer": "L-PAVE",
        "pattern": "ANSI37",
        "scale": 40,
        "keep_boundary": True,
        "points": [
            [TERRACE[0], 3000],
            [TERRACE[1], 3000],
            [TERRACE[1], HOUSE_Y],
            [TERRACE[0], HOUSE_Y],
        ],
    }
)


def under_top(x0, x1, y0, step=250):
    """Closed polygon from y0 up to the curved plot edge between x0 and x1."""
    xs = [x0 + i * step for i in range(int((x1 - x0) // step) + 1)] + [x1]
    return [[x0, y0], [x1, y0]] + [[x, top(x)] for x in reversed(xs)]


lawns = [
    [[PLOT_X0, PLOT_Y0], [PATH[0], PLOT_Y0], [PATH[0], HOUSE_Y], [PLOT_X0, HOUSE_Y]],  # front west
    [
        [PATH[1], PLOT_Y0],
        [TERRACE[0], PLOT_Y0],
        [TERRACE[0], HOUSE_Y],
        [PATH[1], HOUSE_Y],
    ],  # front middle
    [
        [TERRACE[1], PLOT_Y0],
        [X0 + 12200, PLOT_Y0],
        [X0 + 12200, HOUSE_Y],
        [TERRACE[1], HOUSE_Y],
    ],  # front east
    under_top(PLOT_X0, X0, HOUSE_Y),  # west strip
    under_top(X0, X0 + 7200, Y0 + 6000),  # behind the bedrooms
    under_top(X0 + 7200, X0 + 12200, Y0 + 3600),  # behind the living wing
    under_top(X0 + 12200, PLOT_X1, PLOT_Y0),  # east garden
]
for pts in lawns:
    ops.append({"op": "hatch", "layer": "L-LAWN", "pattern": "GRASS", "scale": 40, "points": pts})

for x, y, s in [(16800, 5200, 1000), (21500, 5000, 1000), (19200, 7600, 700), (22300, 8500, 700)]:
    ops.append({"op": "insert", "name": "TREE_CP", "x": x, "y": y, "scale": s, "layer": "L-PLNT"})
for x, y in [
    (3600, 10450),
    (5200, 10550),
    (6800, 10500),
    (8400, 10400),
    (2010, 6000),
    (2010, 8000),
    (3000, 3450),
    (4500, 3450),
    (7200, 3450),
    (9800, 3450),
    (10900, 8600),
    (12600, 8000),
]:
    ops.append({"op": "insert", "name": "SHRUB", "x": x, "y": y, "layer": "L-PLNT"})
ops.append({"op": "insert", "name": "BENCH", "x": 18800, "y": 3500, "layer": "L-SITE"})
for txt, x, y, h in [("GARDEN", 19150, 5300, 250), ("TERRACE", 12300, 3450, 180)]:
    ops.append(
        {
            "op": "text",
            "layer": "A-ANNO-TEXT",
            "x": x,
            "y": y,
            "height": h,
            "text": txt,
            "attach": 5,
        }
    )


async def main():
    async with Client(cadbridge_mcp.mcp) as c:
        # the sketch's placeholder furniture and path outline are replaced
        old = [
            e["handle"]
            for e in cadbridge_mcp.call({"op": "list", "limit": 5000})
            if e["layer"] in ("A-FURN", "A-SITE-PATH")
        ]
        await c.call_tool("cad_ops", {"ops": [{"op": "delete", "handles": old}] + ops})
        print(f"deleted {len(old)} sketch items; {len(ops)} ops applied in one transaction")
        # drop the 3D CDBIM blocks imported while surveying the library (unused)
        await c.call_tool(
            "cad_command", {"text": "_.-PURGE _B CP_* _N _.ZOOM _E"}
        )  # one string: runs in order


if __name__ == "__main__":
    asyncio.run(main())
