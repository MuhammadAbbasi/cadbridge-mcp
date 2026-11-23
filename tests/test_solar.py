"""The solar pack's engine against hand-computed values."""

import ezdxf
import pytest

from engineering.solar import _extract, cable_sizing, sld_geometry, string_sizing, terrain_check

MODULE = {"voc": 51.5, "vmp": 42.9, "isc": 14.4, "beta_voc": -0.25, "beta_vmp": -0.29}
INVERTER = {"vdc_max": 1500, "mppt_min": 500, "mppt_max": 1500, "isc_max_mppt": 60}


def test_string_sizing_by_hand():
    r = string_sizing(MODULE, INVERTER)
    # Voc(-10) = 51.5 * 1.0875 = 56.006 -> 1500 / 56.006 = 26.8 -> 26
    # Vmp(70)  = 42.9 * 0.8695 = 37.30  ->  500 / 37.30  = 13.4 -> 14
    assert r["modules_per_string"] == {"min": 14, "max": 26, "recommended": 26}
    assert r["voc_cold_v"] == pytest.approx(56.01, abs=0.01)
    assert r["strings_per_mppt_max"] == 4  # 60 / (14.4 * 1.0225) = 4.07
    assert not r["warnings"]


def test_string_sizing_refuses_missing_field():
    with pytest.raises(ValueError, match="module.voc"):
        string_sizing({}, INVERTER)


def test_dc_voltage_drop_by_hand():
    # rho90 = 0.017241 * (1 + 0.00393 * 70) = 0.021984; dV = 2*100*10*rho/6 = 7.328 V
    r = cable_sizing(
        [
            {
                "id": "S1",
                "system": "dc",
                "length_m": 100,
                "current_a": 10,
                "voltage_v": 1000,
                "section_mm2": 6,
            }
        ]
    )
    row = r["rows"][0]
    assert row["drop_pct"] == pytest.approx(0.733, abs=0.001)
    assert row["min_section_mm2"] == 4  # 4 mm2 -> 1.10 % <= 1.5 %
    assert row["issues"] == []


def test_ac3_flags_drop_and_ampacity():
    r = cable_sizing(
        [
            {
                "system": "ac3",
                "length_m": 300,
                "power_kw": 300,
                "voltage_v": 800,
                "cos_phi": 0.95,
                "section_mm2": 95,
                "ampacity_a": 200,
            }
        ],
        max_drop_pct=1.0,
    )
    issues = " ".join(r["rows"][0]["issues"])
    assert "drop" in issues and "ampacity" in issues
    assert r["rows"][0]["min_section_mm2"] > 95


def test_sld_rolls_up_kwp_and_draws_every_node():
    spec = {
        "name": "POD",
        "type": "pod",
        "children": [
            {
                "name": "TR1",
                "type": "transformer",
                "children": [
                    {
                        "name": "INV1",
                        "type": "inverter",
                        "kva": 300,
                        "children": [
                            {"name": "S", "type": "strings", "count": 20, "modules": 26, "wp": 580}
                        ],
                    },
                    {
                        "name": "INV2",
                        "type": "inverter",
                        "kva": 300,
                        "children": [
                            {"name": "S", "type": "strings", "count": 20, "modules": 26, "wp": 580}
                        ],
                    },
                ],
            }
        ],
    }
    specs = sld_geometry(spec)
    texts = " ".join(s["text"] for s in specs if s["type"] == "text")
    assert "603.2 kWp" in texts  # 2 * 20 * 26 * 580 W
    assert "DC/AC 1.01" in texts  # 301.6 / 300
    assert sum(s["type"] == "circle" for s in specs) == 2  # the transformer
    with pytest.raises(ValueError, match="type"):
        sld_geometry({"name": "X", "type": "nope"})


def _tilted_plane_doc():
    doc = ezdxf.new()
    msp = doc.modelspace()
    z = lambda x, y: 0.1 * x  # noqa: E731 - 10 % slope along X
    corners = [(-50, -50), (50, -50), (50, 50), (-50, 50)]
    p = [(x, y, z(x, y)) for x, y in corners]
    msp.add_3dface([p[0], p[1], p[2], p[2]], dxfattribs={"layer": "DTM"})
    msp.add_3dface([p[0], p[2], p[3], p[3]], dxfattribs={"layer": "DTM"})
    msp.add_lwpolyline(
        [(0, 0), (20, 0), (20, 2), (0, 2)], close=True, dxfattribs={"layer": "PV-TABLE"}
    )
    msp.add_lwpolyline(
        [(60, 0), (80, 0), (80, 2), (60, 2)], close=True, dxfattribs={"layer": "PV-TABLE"}
    )  # off the DTM
    return doc


def test_terrain_slope_on_a_known_plane():
    tris, tables = _extract(_tilted_plane_doc(), "DTM", "PV-TABLE", None)
    assert len(tris) == 2 and len(tables) == 2
    r = terrain_check(tris, tables, structure="tracker", max_slope_long_pct=8)
    on, off = r["rows"]
    assert on["slope_long_pct"] == pytest.approx(10.0, abs=0.01)
    assert on["slope_cross_pct"] == pytest.approx(0.0, abs=0.01)
    assert on["deviation_max_m"] == pytest.approx(0.0, abs=1e-6)
    assert any("long-axis slope" in i for i in on["issues"])
    assert any("outside the DTM" in i for i in off["issues"])
    fixed = terrain_check(tris, tables[:1], structure="fixed")
    assert fixed["rows"][0]["slope_long_pct"] == pytest.approx(10.0, abs=0.01)
