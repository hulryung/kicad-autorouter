"""The board's custom clearance rules (.kicad_dru) as the router reads them:
conditions, KiCad's precedence, and the grid's row-span rasterizers used for
wide (isolation) clearances.

  python3 tests/test_dru_rules.py
"""
import functools
import importlib.util
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
print = functools.partial(print, flush=True)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dru = _load("dru", "plugins/autorouter/engine/native/dru.py")
grid_mod = _load("grid", "plugins/autorouter/engine/native/grid.py")

RULES = """(version 1)
# a comment
(rule "HV to logic"
  (condition "A.NetClass == 'HV' && B.NetClass != 'HV' && B.NetName != '/PE'")
  (constraint clearance (min 8mm)))
(rule "HV to PE"
  (condition "A.NetClass == 'HV' && B.NetName == '/PE'")
  (constraint clearance (min 5.5mm)))
(rule "fine HV part"
  (condition "(A.memberOfFootprint('U4*') || B.memberOfFootprint('U4*')) && A.NetClass == 'HV' && B.NetClass == 'HV'")
  (constraint clearance (min 0.14mm)))
(rule "width only"
  (condition "A.Type == 'Track' && A.intersectsCourtyard('U9')")
  (constraint track_width (min 0.2mm) (opt 0.3mm)))
(rule "unknown function"
  (condition "A.isPlated()")
  (constraint clearance (min 1mm)))
"""

CLASSES = {"Default": 200_000, "HV": 500_000}
I = dru.Item


def test_rules():
    r = dru.ClearanceRules.parse(RULES, CLASSES, floor=100_000)
    assert [x.name for x in r.rules] == ["HV to logic", "HV to PE", "fine HV part"], [x.name for x in r.rules]
    assert len(r.skipped) == 1 and "isPlated" in r.skipped[0], r.skipped
    hv, logic = I("HV", "/HV1"), I("Default", "/SIG")
    assert r.between(hv, logic) == 8_000_000
    assert r.between(logic, hv) == 8_000_000                      # both orders tried
    assert r.between(hv, I("Default", "/PE")) == 5_500_000
    assert r.between(hv, I("HV", "/HV2")) == 500_000              # no rule: the larger class clearance
    assert r.between(logic, I("Default", "/B")) == 200_000
    pad = I("HV", "/HV2", "U401", "Pad")
    assert r.between(hv, pad) == 140_000                          # a later rule wins, lower than the class
    assert r.between(hv, I("HV", "/HV2", "R1", "Pad")) == 500_000
    assert r.normalise(I("HV", "/X", "R1", "Pad")).ref == ""      # a footprint no rule names is dropped
    assert r.normalise(I("HV", "/X", "U402", "Pad")).ref == "U402"
    print("ok   conditions, both orders, precedence, net-class fallback")


def test_floor_and_units():
    r = dru.ClearanceRules.parse('(rule "x" (condition "A.NetName == \'/A*\'") (constraint clearance (min 2mil)))',
                                 CLASSES, floor=100_000)
    assert r.between(I("Default", "/AB"), I("Default", "/C")) == 100_000     # 2 mil is below the board floor
    print("ok   board minimum stays the floor; mil units; wildcards")


def test_wide_rasterizers():
    g = grid_mod.Grid(0, 0, 100_000, 300, 300, 1)
    for x1, y1, x2, y2, r in [(5e6, 5e6, 20e6, 9e6, 3e6), (10e6, 10e6, 10e6, 25e6, 1.5e6), (8e6, 8e6, 8e6, 8e6, 2e6)]:
        x1, y1, x2, y2, r = map(int, (x1, y1, x2, y2, r))
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy

        def hit(px, py):
            t = 0 if L2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / L2))
            ex, ey = px - (x1 + t * dx), py - (y1 + t * dy)
            return ex * ex + ey * ey <= r * r
        exact = set(g.cells_where(0, (min(x1, x2) - r, min(y1, y2) - r, max(x1, x2) + r, max(y1, y2) + r), hit))
        assert set(g.segment_cells(0, x1, y1, x2, y2, r)) == exact
    a, b = g.new_owner_map(), g.new_owner_map()
    for k, (x, y) in enumerate([(8e6, 8e6), (11e6, 9e6), (20e6, 20e6), (9e6, 9e6)]):
        cells = g.rounded_box_cells(0, (int(x), int(y), int(x) + 400_000, int(y) + 200_000), 2_000_000)
        grid_mod.Grid.mark(a, cells, k % 3 + 1)
        grid_mod.Grid.mark(b, list(cells), k % 3 + 1)
    assert a == b
    assert grid_mod.BLOCKED in a
    print("ok   row-span capsule and box rings match the exact cells; span marking matches")


if __name__ == "__main__":
    test_rules()
    test_floor_and_units()
    test_wide_rasterizers()
    print("test_dru_rules: all passed")
