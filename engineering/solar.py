"""Utility-scale PV: string sizing, cable voltage drop, SLD geometry, terrain check.

Pure functions except `run_terrain_check` / `run_sld_draw`, which read or draw
through a backend. Civil work the CAD already does (DTM build, earthworks,
roads, sections - CADPILLAR, Civil 3D) is deliberately not re-implemented:
this module consumes the DTM those tools produce, as 3DFACE / polyface mesh.

Formulas (IEC 62548 / CEI 82-25 practice, IEC 60364-5-52 voltage drop):
  V(T)  = V_stc * (1 + beta/100 * (T - 25))
  dV dc = 2 L I rho_T / S
  dV ac = k L I (rho_T/S * cos(phi) + x * sin(phi)),  k = 2 (1-ph) | sqrt(3) (3-ph)
  rho_T = rho_20 * (1 + alpha (T - 20))
"""

from __future__ import annotations

import asyncio
import csv
import math
import shutil
from pathlib import Path

__all__ = [
    "string_sizing",
    "cable_sizing",
    "sld_geometry",
    "terrain_check",
    "run_terrain_check",
    "run_sld_draw",
]

# ── string sizing ─────────────────────────────────────────────────────────────


def _num(spec: dict, key: str, where: str, *, default=None, positive=True) -> float:
    value = spec.get(key, default)
    if value is None:
        raise ValueError(f"{where}.{key}: required")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}.{key}: expected a number, got {value!r}") from None
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{where}.{key}: must be a finite number{' > 0' if positive else ''}")
    return value


def _at(v_stc: float, beta_pct: float, t: float) -> float:
    return v_stc * (1 + beta_pct / 100.0 * (t - 25.0))


def string_sizing(
    module: dict, inverter: dict, *, t_cell_min: float = -10.0, t_cell_max: float = 70.0
) -> dict:
    """Modules per string and strings per MPPT for one module / inverter pair.

    module: voc, vmp, isc, imp [V/A at STC], beta_voc [%/°C, negative],
            beta_vmp [%/°C, default = beta_voc], alpha_isc [%/°C, default 0.05],
            bifacial_gain [0-0.3, default 0] - raises the current used for the MPPT check.
    inverter: vdc_max, mppt_min, mppt_max [V], isc_max_mppt [A per MPPT input],
              system_v_max [V, default 1500 - the module/BOS insulation class].
    """
    voc = _num(module, "voc", "module")
    vmp = _num(module, "vmp", "module")
    isc = _num(module, "isc", "module")
    b_voc = _num(module, "beta_voc", "module", positive=False)
    b_vmp = _num(module, "beta_vmp", "module", default=b_voc, positive=False)
    a_isc = _num(module, "alpha_isc", "module", default=0.05, positive=False)
    gain = _num(module, "bifacial_gain", "module", default=0.0, positive=False)
    vdc_max = _num(inverter, "vdc_max", "inverter")
    mppt_min = _num(inverter, "mppt_min", "inverter")
    mppt_max = _num(inverter, "mppt_max", "inverter")
    isc_mppt = _num(inverter, "isc_max_mppt", "inverter", default=math.inf)
    sys_v = _num(inverter, "system_v_max", "inverter", default=1500.0)
    if t_cell_min >= t_cell_max:
        raise ValueError("t_cell_min must be below t_cell_max")
    if mppt_min >= mppt_max:
        raise ValueError("inverter.mppt_min must be below mppt_max")

    voc_cold = _at(voc, b_voc, t_cell_min)
    vmp_cold = _at(vmp, b_vmp, t_cell_min)
    vmp_hot = _at(vmp, b_vmp, t_cell_max)
    isc_hot = isc * (1 + a_isc / 100.0 * (t_cell_max - 25.0)) * (1 + gain)

    n_max_voc = math.floor(min(vdc_max, sys_v) / voc_cold)
    n_max_mppt = math.floor(mppt_max / vmp_cold)
    n_min = math.ceil(mppt_min / vmp_hot)
    n_max = min(n_max_voc, n_max_mppt)
    warnings = []
    if n_min > n_max:
        warnings.append(f"no feasible string length: needs >= {n_min} modules but <= {n_max}")
    if n_max_mppt < n_max_voc:
        warnings.append(
            f"MPPT upper bound limits the string ({n_max_mppt}) before Voc does ({n_max_voc}): "
            "cold-day operation would leave the MPPT window"
        )
    strings_per_mppt = math.floor(isc_mppt / isc_hot) if math.isfinite(isc_mppt) else None
    return {
        "modules_per_string": {"min": n_min, "max": n_max, "recommended": n_max},
        "limits": {"by_voc": n_max_voc, "by_mppt_max": n_max_mppt, "by_mppt_min": n_min},
        "voc_cold_v": round(voc_cold, 2),
        "vmp_cold_v": round(vmp_cold, 2),
        "vmp_hot_v": round(vmp_hot, 2),
        "isc_design_a": round(isc_hot, 2),
        "string_voc_cold_v": round(voc_cold * n_max, 1) if n_max > 0 else None,
        "strings_per_mppt_max": strings_per_mppt,
        "t_cell_min": t_cell_min,
        "t_cell_max": t_cell_max,
        "warnings": warnings,
    }


# ── cable voltage drop ────────────────────────────────────────────────────────

SECTIONS_MM2 = (1.5, 2.5, 4, 6, 10, 16, 25, 35, 50, 70, 95, 120, 150, 185, 240, 300, 400, 500, 630)
_RHO20 = {"cu": 0.017241, "al": 0.028264}  # ohm mm2 / m
_ALPHA = {"cu": 0.00393, "al": 0.00403}
_SYSTEMS = {
    "dc": (2.0, 2),
    "ac1": (2.0, 2),
    "ac3": (math.sqrt(3), 3),
}  # (k drop, loaded conductors)


def _drop(run: dict, s: float, rho: float) -> tuple[float, float]:
    """(drop %, loss W) for section s mm2 per conductor."""
    k, n_cond = _SYSTEMS[run["system"]]
    par = run["parallel"]
    r = rho / (s * par)  # ohm/m of the parallel bundle
    i, length = run["current_a"], run["length_m"]
    if run["system"] == "dc":
        dv = k * length * i * r
    else:
        phi = math.acos(run["cos_phi"])
        dv = k * length * i * (r * math.cos(phi) + run["x_ohm_km"] / 1000.0 / par * math.sin(phi))
    return dv / run["voltage_v"] * 100.0, n_cond * i * i * r * length


def cable_sizing(
    runs: list[dict], *, max_drop_pct: float = 1.5, conductor_temp_c: float = 90.0
) -> dict:
    """Voltage drop, loss and the smallest standard section meeting ``max_drop_pct``.

    Each run: id, system dc|ac1|ac3, length_m, voltage_v (line-line for ac3),
    current_a or power_kw (+cos_phi for ac), section_mm2 (optional - checked if given),
    material cu|al (cu), parallel (1), x_ohm_km (0.08), ampacity_a (optional, Iz of
    the installed section, from your installation-method table), max_drop_pct (per run).
    """
    if not runs:
        raise ValueError("runs: give at least one cable run")
    rows, total_loss, problems = [], 0.0, 0
    for idx, raw in enumerate(runs):
        where = f"runs[{idx}]"
        system = str(raw.get("system", "dc")).lower()
        if system not in _SYSTEMS:
            raise ValueError(f"{where}.system: one of dc | ac1 | ac3, got {system!r}")
        material = str(raw.get("material", "cu")).lower()
        if material not in _RHO20:
            raise ValueError(f"{where}.material: cu | al, got {material!r}")
        v = _num(raw, "voltage_v", where)
        cos_phi = _num(raw, "cos_phi", where, default=1.0)
        if cos_phi > 1:
            raise ValueError(f"{where}.cos_phi: must be <= 1")
        if raw.get("current_a") is not None:
            i = _num(raw, "current_a", where)
        else:
            p = _num(raw, "power_kw", where) * 1000.0
            i = (
                p / v
                if system == "dc"
                else p / (v * cos_phi)
                if system == "ac1"
                else p / (math.sqrt(3) * v * cos_phi)
            )
        run = {
            "system": system,
            "length_m": _num(raw, "length_m", where),
            "voltage_v": v,
            "current_a": i,
            "cos_phi": cos_phi,
            "parallel": int(_num(raw, "parallel", where, default=1)),
            "x_ohm_km": _num(raw, "x_ohm_km", where, default=0.08, positive=False),
        }
        limit = _num(raw, "max_drop_pct", where, default=max_drop_pct)
        rho = _RHO20[material] * (1 + _ALPHA[material] * (conductor_temp_c - 20.0))
        minimum = next((s for s in SECTIONS_MM2 if _drop(run, s, rho)[0] <= limit), None)
        row = {
            "id": raw.get("id", f"run{idx + 1}"),
            "system": system,
            "material": material,
            "current_a": round(i, 2),
            "length_m": run["length_m"],
            "parallel": run["parallel"],
            "min_section_mm2": minimum,
            "issues": [],
        }
        if minimum is None:
            row["issues"].append(
                f"no section up to {SECTIONS_MM2[-1]} mm2 meets {limit} % - add parallel runs"
            )
        section = raw.get("section_mm2")
        check = float(section) if section is not None else minimum
        if check:
            pct, loss = _drop(run, check, rho)
            row.update(section_mm2=check, drop_pct=round(pct, 3), loss_w=round(loss, 1))
            total_loss += loss
            if pct > limit:
                row["issues"].append(f"drop {pct:.2f} % > {limit} %")
        if raw.get("ampacity_a") is not None:
            iz = _num(raw, "ampacity_a", where) * run["parallel"]
            if i > iz:
                row["issues"].append(f"current {i:.1f} A > ampacity {iz:.1f} A")
        problems += bool(row["issues"])
        rows.append(row)
    return {
        "rows": rows,
        "summary": {
            "runs": len(rows),
            "with_issues": problems,
            "total_loss_kw": round(total_loss / 1000, 3),
        },
        "method": f"rho at {conductor_temp_c} °C; ampacity only checked where ampacity_a is given "
        "(installation-method tables per IEC 60364-5-52 / CEI-UNEL 35024 are not built in)",
    }


# ── single-line diagram ───────────────────────────────────────────────────────

NODE_TYPES = (
    "pod",
    "switchgear",
    "breaker",
    "transformer",
    "cabin",
    "inverter",
    "combiner",
    "strings",
)
_MAX_NODES = 5000


def _kwp(node: dict) -> float:
    if node.get("type") == "strings":
        return (
            float(node.get("count", 1))
            * float(node.get("modules", 0))
            * float(node.get("wp", 0))
            / 1000
        )
    return sum(_kwp(c) for c in node.get("children", ()))


def sld_geometry(root: dict, *, x0: float = 0.0, y0: float = 0.0, scale: float = 1.0) -> list[dict]:
    """Batch-create specs for a top-down single-line diagram of the ``root`` tree.

    Node: {name, type (NODE_TYPES), rating (free text, optional), kva (inverters,
    optional), children: [...]}; a ``strings`` leaf carries count, modules, wp.
    kWp rolls up from the strings; an inverter with kva shows its DC/AC ratio.
    """
    w, h, gap_x, gap_y, th = 24 * scale, 10 * scale, 8 * scale, 18 * scale, 1.8 * scale
    seen = [0]

    def check(node, path):
        seen[0] += 1
        if seen[0] > _MAX_NODES:
            raise ValueError(f"spec: more than {_MAX_NODES} nodes")
        if not isinstance(node, dict) or not node.get("name"):
            raise ValueError(f"{path}: every node needs a name")
        if node.get("type", "breaker") not in NODE_TYPES:
            raise ValueError(f"{path}.type: one of {', '.join(NODE_TYPES)}")
        for i, child in enumerate(node.get("children", ())):
            check(child, f"{path}.children[{i}]")

    check(root, "spec")

    def width(node) -> float:
        kids = node.get("children", ())
        return max(w, sum(width(c) for c in kids) + gap_x * (len(kids) - 1)) if kids else w

    specs: list[dict] = []

    def line(a, b, c, d):
        specs.append({"type": "line", "x1": a, "y1": b, "x2": c, "y2": d, "layer": "SLD-WIRE"})

    def text(s, x, y, k=1.0):
        specs.append(
            {"type": "text", "text": s, "x": x, "y": y, "height": th * k, "layer": "SLD-TEXT"}
        )

    def symbol(node, cx, top):
        kind = node.get("type", "breaker")
        left, bottom = cx - w / 2, top - h
        if kind == "transformer":
            r = h * 0.32
            for dy in (r * 0.7, -r * 0.7):
                specs.append(
                    {
                        "type": "circle",
                        "cx": cx,
                        "cy": top - h / 2 + dy,
                        "radius": r,
                        "layer": "SLD-SYMBOL",
                    }
                )
        else:
            box = [[left, bottom], [left + w, bottom], [left + w, top], [left, top]]
            specs.append({"type": "polyline", "points": box, "closed": True, "layer": "SLD-SYMBOL"})
            if kind == "inverter":
                line(left, bottom, left + w, top)
                text("=", left + w * 0.15, top - h * 0.4)
                text("~", left + w * 0.7, bottom + h * 0.15)
        label_x = cx + w / 2 + th * 0.5
        text(node["name"], label_x, top - th * 1.2)
        info = [node.get("rating", "")]
        kwp = _kwp(node)
        if kwp:
            info.append(f"{kwp:,.1f} kWp")
        if kind == "inverter" and node.get("kva") and kwp:
            info.append(f"DC/AC {kwp / float(node['kva']):.2f}")
        if kind == "strings":
            info.append(
                f"{node.get('count', 1)} x ({node.get('modules', '?')} x {node.get('wp', '?')} Wp)"
            )
        info = " | ".join(s for s in info if s)
        if info:
            text(info, label_x, top - th * 2.8, 0.75)

    def place(node, left, top):
        span = width(node)
        cx = left + span / 2
        symbol(node, cx, top)
        kids = node.get("children", ())
        if kids:
            bus_y = top - h - gap_y / 2
            line(cx, top - h, cx, bus_y)
            x, centres = left, []
            for child in kids:
                cw = width(child)
                centres.append(x + cw / 2)
                place(child, x, top - h - gap_y)
                x += cw + gap_x
            line(min(centres), bus_y, max(centres), bus_y)
            for c in centres:
                line(c, bus_y, c, top - h - gap_y)

    place(root, x0, y0)
    return specs


async def run_sld_draw(backend, spec: dict, *, x0=0.0, y0=0.0, scale=1.0) -> dict:
    specs = sld_geometry(spec, x0=x0, y0=y0, scale=scale)
    for name, color in (("SLD-SYMBOL", 7), ("SLD-WIRE", 1), ("SLD-TEXT", 2)):
        try:
            await backend.layer_create(name, color=color)
        except Exception:  # noqa: BLE001 - the layer already exists
            pass
    result = await backend.entity_batch_create(specs)
    return {
        "created": result["created"],
        "errors": result["errors"],
        "total_kwp": round(_kwp(spec), 2),
        "layers": ["SLD-SYMBOL", "SLD-WIRE", "SLD-TEXT"],
    }


# ── terrain (DTM) check ───────────────────────────────────────────────────────


class _Tin:
    """3D triangles with a uniform bucket grid for point-in-triangle lookup."""

    def __init__(self, tris: list[tuple]):
        if not tris:
            raise ValueError("no DTM triangles (3DFACE / polyface mesh) found on the given layer")
        self.tris = tris
        xs = [p[0] for t in tris for p in t]
        ys = [p[1] for t in tris for p in t]
        area = (max(xs) - min(xs)) * (max(ys) - min(ys)) or 1.0
        self.cell = math.sqrt(area / len(tris)) * 2 or 1.0
        self.grid: dict[tuple[int, int], list[int]] = {}
        for k, t in enumerate(tris):
            i0, i1 = (
                int(math.floor(min(p[0] for p in t) / self.cell)),
                int(math.floor(max(p[0] for p in t) / self.cell)),
            )
            j0, j1 = (
                int(math.floor(min(p[1] for p in t) / self.cell)),
                int(math.floor(max(p[1] for p in t) / self.cell)),
            )
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    self.grid.setdefault((i, j), []).append(k)

    def z(self, x: float, y: float) -> float | None:
        for k in self.grid.get(
            (int(math.floor(x / self.cell)), int(math.floor(y / self.cell))), ()
        ):
            (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = self.tris[k]
            d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
            if d == 0:
                continue
            a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
            b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
            c = 1 - a - b
            if min(a, b, c) >= -1e-9:
                return a * z1 + b * z2 + c * z3
        return None


def _fit_plane(pts):
    """Least-squares z = a + b x + c y; returns (a, b, c)."""
    n = len(pts)
    sx = sum(p[0] for p in pts) / n
    sy = sum(p[1] for p in pts) / n
    sz = sum(p[2] for p in pts) / n
    xx = sum((p[0] - sx) ** 2 for p in pts)
    yy = sum((p[1] - sy) ** 2 for p in pts)
    xy = sum((p[0] - sx) * (p[1] - sy) for p in pts)
    xz = sum((p[0] - sx) * (p[2] - sz) for p in pts)
    yz = sum((p[1] - sy) * (p[2] - sz) for p in pts)
    det = xx * yy - xy * xy
    if abs(det) < 1e-12:  # collinear piles (a tracker): fit along the line only
        b, c = (xz / xx, 0.0) if xx >= yy and xx else (0.0, yz / yy if yy else 0.0)
    else:
        b = (xz * yy - yz * xy) / det
        c = (yz * xx - xz * xy) / det
    return sz - b * sx - c * sy, b, c


def _frame(points, axis_angle):
    """(centre, unit long axis, unit cross axis, length, width) of a footprint."""
    ux, uy = math.cos(axis_angle), math.sin(axis_angle)
    s = [x * ux + y * uy for x, y in points]
    t = [-x * uy + y * ux for x, y in points]
    ls, lt = max(s) - min(s), max(t) - min(t)
    cs, ct = (max(s) + min(s)) / 2, (max(t) + min(t)) / 2
    centre = (cs * ux - ct * uy, cs * uy + ct * ux)
    if lt > ls:  # the other axis is the long one
        return centre, (-uy, ux), (ux, uy), lt, ls
    return centre, (ux, uy), (-uy, ux), ls, lt


def terrain_check(
    tris: list[tuple],
    tables: list[dict],
    *,
    structure: str = "tracker",
    piles: int = 7,
    max_slope_long_pct: float | None = None,
    max_slope_cross_pct: float | None = None,
    max_deviation_m: float | None = None,
) -> dict:
    """Per-table ground slope and pile-to-ground deviation from a TIN.

    tables: {id, points: [(x, y), ...], axis_angle (rad)}. Piles are sampled
    along the long axis: one line for a tracker (torque tube), two (front/rear
    posts, at 1/4 and 3/4 of the width) for fixed tilt. Deviation is ground minus
    the best-fit structure line/plane at each pile: the reveal spread the
    pile cut-lengths or grading must absorb.
    """
    if structure not in ("tracker", "fixed"):
        raise ValueError("structure: tracker | fixed")
    if piles < 2:
        raise ValueError("piles: at least 2 per line")
    tin = _Tin(tris)
    rows, flagged = [], 0
    for tab in tables:
        (cx, cy), (ux, uy), (vx, vy), length, wid = _frame(tab["points"], tab["axis_angle"])
        offsets = (0.0,) if structure == "tracker" else (-wid / 4, wid / 4)
        sample = []
        for off in offsets:
            for k in range(piles):
                s = -length / 2 + length * k / (piles - 1)
                sample.append((cx + s * ux + off * vx, cy + s * uy + off * vy))
        ground = [(x, y, tin.z(x, y)) for x, y in sample]
        row = {
            "id": tab["id"],
            "x": round(cx, 3),
            "y": round(cy, 3),
            "length_m": round(length, 2),
            "issues": [],
        }
        on = [g for g in ground if g[2] is not None]
        if len(on) < len(ground):
            row["issues"].append(f"{len(ground) - len(on)} piles outside the DTM")
        if len(on) >= 2:
            # slopes in the table's own frame: project the fitted plane's gradient
            footprint = [(x, y, tin.z(x, y)) for x, y in tab["points"]] + on
            plane_pts = [p for p in footprint if p[2] is not None]
            _, gx, gy = _fit_plane(plane_pts)
            slope_long = abs(gx * ux + gy * uy) * 100
            slope_cross = abs(gx * vx + gy * vy) * 100
            a, b, c = _fit_plane(on)
            dev = [g[2] - (a + b * g[0] + c * g[1]) for g in on]
            row.update(
                z_min=round(min(g[2] for g in on), 3),
                z_max=round(max(g[2] for g in on), 3),
                slope_long_pct=round(slope_long, 2),
                slope_cross_pct=round(slope_cross, 2),
                deviation_max_m=round(max(abs(d) for d in dev), 3),
            )
            if max_slope_long_pct is not None and slope_long > max_slope_long_pct:
                row["issues"].append(f"long-axis slope {slope_long:.1f} % > {max_slope_long_pct} %")
            if max_slope_cross_pct is not None and slope_cross > max_slope_cross_pct:
                row["issues"].append(f"cross slope {slope_cross:.1f} % > {max_slope_cross_pct} %")
            if max_deviation_m is not None and row["deviation_max_m"] > max_deviation_m:
                row["issues"].append(
                    f"pile deviation {row['deviation_max_m']} m > {max_deviation_m} m"
                )
        flagged += bool(row["issues"])
        rows.append(row)
    slopes = [r["slope_long_pct"] for r in rows if "slope_long_pct" in r]
    return {
        "rows": rows,
        "summary": {
            "tables": len(rows),
            "flagged": flagged,
            "triangles": len(tris),
            "slope_long_pct_max": max(slopes) if slopes else None,
        },
    }


def _extract(doc, dtm_layer: str | None, table_layer: str, table_block: str | None):
    """(triangles, tables) from an ezdxf document's modelspace."""
    msp = doc.modelspace()
    up = str.upper
    tris, tables = [], []

    def add_face(face):
        pts = [tuple(face.dxf.get(f"vtx{i}")) for i in range(4)]
        tris.append(tuple(pts[:3]))
        if pts[3] != pts[2]:
            tris.append((pts[0], pts[2], pts[3]))

    for e in msp:
        kind, layer = e.dxftype(), up(e.dxf.get("layer", "0"))
        if dtm_layer is None or layer == up(dtm_layer):
            if kind == "3DFACE":
                add_face(e)
            elif kind == "POLYLINE" and e.is_poly_face_mesh:
                for face in e.virtual_entities():
                    add_face(face)
        if layer != up(table_layer):
            continue
        if kind == "LWPOLYLINE" and e.closed:
            pts = [(p[0], p[1]) for p in e.get_points("xy")]
            edges = [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
            (x1, y1), (x2, y2) = max(edges, key=lambda ed: math.dist(*ed))
            tables.append(
                {"id": e.dxf.handle, "points": pts, "axis_angle": math.atan2(y2 - y1, x2 - x1)}
            )
        elif kind == "INSERT" and (table_block is None or up(e.dxf.name) == up(table_block)):
            pts = []
            for v in e.virtual_entities():
                if v.dxftype() == "LINE":
                    pts += [tuple(v.dxf.start)[:2], tuple(v.dxf.end)[:2]]
                elif v.dxftype() == "LWPOLYLINE":
                    pts += [(p[0], p[1]) for p in v.get_points("xy")]
            if len(pts) >= 3:
                tables.append(
                    {
                        "id": e.dxf.handle,
                        "points": pts,
                        "axis_angle": math.radians(e.dxf.get("rotation", 0.0)),
                    }
                )
    return tris, tables


async def _load_doc(backend, path: str | None):
    """An ezdxf document of `path` or of the current drawing (live: exported to a temp DXF)."""
    import ezdxf

    if path and path.lower().endswith(".dxf"):
        return await asyncio.to_thread(ezdxf.readfile, path)
    if backend.name == "com":
        from engineering.understand.snapshot import _com_export

        written, folder, _ = await backend._run(_com_export, backend, path)
        if path:
            await backend._ensure_document_state()
        try:
            return await asyncio.to_thread(ezdxf.readfile, written)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
    if path:
        raise ValueError(f"{path}: the headless engine reads .dxf only; export the DWG to DXF")
    return backend._doc


async def run_terrain_check(
    backend,
    *,
    drawing_path: str | None,
    dtm_layer: str | None,
    table_layer: str,
    table_block: str | None,
    output: str | None,
    mark: bool,
    **limits,
) -> dict:
    doc = await _load_doc(backend, drawing_path)
    tris, tables = await asyncio.to_thread(_extract, doc, dtm_layer, table_layer, table_block)
    if not tables:
        raise ValueError(
            f"no tables found on layer {table_layer!r} (closed polylines or block references)"
        )
    result = await asyncio.to_thread(terrain_check, tris, tables, **limits)
    if output:
        cols = [
            "id",
            "x",
            "y",
            "length_m",
            "z_min",
            "z_max",
            "slope_long_pct",
            "slope_cross_pct",
            "deviation_max_m",
            "issues",
        ]
        with Path(output).open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore", delimiter=";")
            writer.writeheader()
            for r in result["rows"]:
                writer.writerow({**r, "issues": " | ".join(r["issues"])})
        result["files"] = {"csv": output}
    if mark and drawing_path is None:
        try:
            await backend.layer_create("PV-TERRAIN-FLAG", color=1)
        except Exception:  # noqa: BLE001 - the layer already exists
            pass
        specs = [
            {
                "type": "circle",
                "cx": r["x"],
                "cy": r["y"],
                "radius": max(r["length_m"] / 2, 1.0),
                "layer": "PV-TERRAIN-FLAG",
            }
            for r in result["rows"]
            if r["issues"]
        ]
        result["marked"] = (await backend.entity_batch_create(specs))["created"] if specs else 0
    return result
