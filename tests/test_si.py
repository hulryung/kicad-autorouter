"""Design intent: stackup impedance math, the intent file, routing groups,
KiCad rules and length matching (meanders). The pcbnew parts skip without
KiCad's Python.

  python3 tests/test_si.py
"""
import functools
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from autorouter.engine import si, stackup as stk

print = functools.partial(print, flush=True)
HERE = Path(__file__).resolve().parent


def close(a, b, rel):
    return abs(a - b) <= rel * abs(b)


def test_formulas():
    # textbook checks: a 50 ohm microstrip on 1.6 mm FR4 is ~3 mm wide; a
    # wide track has a lower impedance; coupling lowers the pair's impedance
    assert close(stk.microstrip(3.0, 1.6, 0.035, 4.5), 50, 0.06), stk.microstrip(3.0, 1.6, 0.035, 4.5)
    assert stk.microstrip(0.2, 0.2, 0.035, 4.4) > stk.microstrip(0.4, 0.2, 0.035, 4.4)
    assert close(stk.stripline(0.2, 0.25, 0.25, 0.0152, 4.4), 50, 0.12), stk.stripline(0.2, 0.25, 0.25, 0.0152, 4.4)
    g = stk.geometry(stk.TEMPLATES["JLC04161H-7628"], 0)
    assert stk.differential(g, 0.2, 0.1) < stk.differential(g, 0.2, 0.5) < 2 * stk.single_ended(g, 0.2)
    # the JLC 7628 4-layer build: 50 ohm outer ~0.33-0.38 mm, 90 ohm pairs come out near target
    st = stk.TEMPLATES["JLC04161H-7628"]
    t = stk.size_for(st, 0, single_ohm=50)
    assert 330_000 <= t.width_nm <= 390_000, t
    assert close(t.ohm, 50, 0.02)
    p = stk.size_for(st, 0, diff_ohm=90)
    assert close(p.ohm, 90, 0.03) and p.gap_nm >= 100_000, p
    inner = stk.size_for(st, 1, single_ohm=50)
    assert inner.width_nm < t.width_nm                  # stripline is narrower for the same impedance
    for name, s in stk.TEMPLATES.items():
        assert 1.5 < s.thickness_mm < 1.7, (name, s.thickness_mm)
        assert len(s.dielectrics) == s.layers - 1
    print("test_si: formulas ok")


STACKUP_TEXT = """(kicad_pcb
	(setup
		(stackup
			(layer "F.Mask"
				(type "Top Solder Mask")
				(thickness 0.01)
			)
			(layer "F.Cu"
				(type "copper")
				(thickness 0.035)
			)
			(layer "dielectric 1"
				(type "prepreg")
				(thickness 0.2)
				(material "FR4")
				(epsilon_r 4.4)
				(loss_tangent 0.02)
			)
			(layer "B.Cu"
				(type "copper")
				(thickness 0.035)
			)
		)
	)
)
"""


def test_parse_and_config():
    st = stk.parse_stackup(STACKUP_TEXT)
    assert st is not None and st.layers == 2 and st.dielectrics[0].thickness_mm == 0.2 and st.mask_mm == 0.01
    cfg = si.SiConfig.from_dict({"stackup": "JLC04161H-3313",
                                 "impedance": [{"name": "USB", "nets": ["*USB_D?"], "diff_ohm": 90}],
                                 "length_groups": [{"name": "Lane", "nets": ["DQ*"], "tolerance_mm": 0.5}],
                                 "route_order": ["power", "memory"], "junk": 1})
    assert cfg.route_order[:2] == ["power", "memory"] and set(cfg.route_order) == set(si.DEFAULT_ORDER)
    again = si.SiConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert again == cfg
    assert si.matches("/USB_DP", ["USB_D?"]) and si.matches("/mcu/DQ3", ["dq*"]) and not si.matches("/X", ["Y*"])
    print("test_si: stackup file and intent file ok")


try:
    import pcbnew
except ImportError:
    pcbnew = None


def _track(board, net, x1, y1, x2, y2, w=200_000, layer=None):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(int(x1 * 1e6), int(y1 * 1e6)))
    t.SetEnd(pcbnew.VECTOR2I(int(x2 * 1e6), int(y2 * 1e6)))
    t.SetWidth(w)
    t.SetLayer(pcbnew.F_Cu if layer is None else layer)
    t.SetNetCode(net.GetNetCode())
    board.Add(t)
    return t


def test_tuning(tmp: Path):
    """Three nets of a bus, 20, 26 and 30 mm long, with an obstacle track
    beside the shortest: after tuning all are within 0.2 mm of 30 mm and
    no meander comes near the obstacle or the other nets."""
    from autorouter.engine.native import tuning
    board = pcbnew.BOARD()
    # outline 0..60 x 0..40 mm
    for (x1, y1, x2, y2) in ((0, 0, 60, 0), (60, 0, 60, 40), (60, 40, 0, 40), (0, 40, 0, 0)):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pcbnew.VECTOR2I(int(x1 * 1e6), int(y1 * 1e6)))
        s.SetEnd(pcbnew.VECTOR2I(int(x2 * 1e6), int(y2 * 1e6)))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(100_000)
        board.Add(s)
    nets = []
    for name in ("D0", "D1", "D2", "OTHER"):
        n = pcbnew.NETINFO_ITEM(board, name)
        board.Add(n)
        nets.append(n)
    _track(board, nets[0], 5, 10, 25, 10)                        # 20 mm
    _track(board, nets[1], 5, 20, 31, 20)                        # 26 mm
    _track(board, nets[2], 5, 30, 35, 30)                        # 30 mm
    _track(board, nets[3], 5, 11.2, 25, 11.2)                    # just below D0: D0 must bump upward
    path = tmp / "tune.kicad_pcb"
    board.SetFileName(str(path))
    report = tuning.tune(board, [{"name": "bus", "nets": ["D0", "D1", "D2"], "tolerance": 200_000, "target": 0}],
                         spacing_nm=600_000)
    lengths = tuning.track_lengths(board)
    got = {n.GetNetname(): lengths[n.GetNetCode()] for n in nets[:3]}
    assert all(abs(v - 30_000_000) <= 200_000 for v in got.values()), (got, report.lines())
    assert report.groups[0]["ok"], report.lines()
    # every new segment is 0/45/90 degrees and keeps clear of the other nets
    for t in board.GetTracks():
        dx, dy = t.GetEnd().x - t.GetStart().x, t.GetEnd().y - t.GetStart().y
        assert dx == 0 or dy == 0 or abs(abs(dx) - abs(dy)) <= 2, (dx, dy)
    d0 = [t for t in board.GetTracks() if t.GetNetCode() == nets[0].GetNetCode()]
    other = [t for t in board.GetTracks() if t.GetNetCode() == nets[3].GetNetCode()][0]
    for t in d0:
        assert t.GetStart().y <= 10_000_000 and t.GetEnd().y <= 10_000_000, "a bump went toward OTHER"
        assert not t.GetEffectiveShape().Collide(other.GetEffectiveShape(), 150_000)
    print(f"test_si: length matching ok ({'; '.join(report.lines())})")


def test_board_intent(tmp: Path):
    """The fixture board with a template stackup: per-net sizes, net classes,
    DRC rules text, and the routing groups the extractor assigns."""
    import shutil
    from autorouter.engine.native import extract
    from autorouter.engine.native.problem import NativeOptions
    src = HERE / "fixtures" / "obd2-adapter.kicad_pcb"
    dst = tmp / "obd2-adapter.kicad_pcb"
    shutil.copy(src, dst)
    board = pcbnew.LoadBoard(str(dst))
    names = [n.GetNetname() for c, n in board.GetNetsByNetcode().items() if c > 0]
    target = sorted(names)[1]
    cfg = si.SiConfig(stackup="JLC 2L 1.6mm" if board.GetCopperLayerCount() == 2 else "JLC04161H-7628",
                      impedance=[si.ImpedanceClass("Z50", [target], single_ohm=50)],
                      length_groups=[si.LengthGroup("pair", sorted(names)[2:4], 0.5)],
                      route_order=["power", "signal", "pairs", "memory", "high_speed", "clock"])
    plan = si.plan(board, cfg)
    assert target in plan.impedance, plan.warnings
    widths = plan.impedance[target]["widths"]
    assert all(w > 100_000 for w in widths.values()), widths
    o = si.apply_to_options(NativeOptions(), cfg, plan)
    problem = extract.extract(board, o)
    net = next(n for n in problem.nets.values() if n.name == target)
    assert net.layer_widths and net.rules.width == max(net.layer_widths.values()), (net.layer_widths, net.rules)
    assert net.group == "high_speed", net.group
    ranks = {n.group: n.rank for n in problem.nets.values()}
    if "power" in ranks and "signal" in ranks:
        assert ranks["power"] < ranks["signal"], ranks
    order = [c.rank for c in sorted(problem.connections, key=lambda c: (c.rank, c.priority, c.estimate))]
    assert order == sorted(order)
    done = si.apply_net_classes(board, cfg, plan)
    assert done and board.GetDesignSettings().m_NetSettings.GetEffectiveNetClass(target).GetName() == "Z50", done
    rules = si.drc_rules(board, cfg, plan, {n: 10_000_000 for n in names})
    assert rules.count("(rule ") >= 2 and "track_width" in rules and "length" in rules, rules
    p = si.write_drc_rules(board, rules)
    p2 = si.write_drc_rules(board, rules)                     # rewritten in place, not appended
    assert p == p2 and p.read_text().count(si.BEGIN) == 1 and p.read_text().startswith("(version 1)")
    print(f"test_si: board intent ok ({len(plan.impedance)} controlled nets, groups {problem.stats['groups']})")


if __name__ == "__main__":
    test_formulas()
    test_parse_and_config()
    if pcbnew is None:
        print("test_si: board parts skipped (needs KiCad's Python)")
        sys.exit(0)
    with tempfile.TemporaryDirectory() as d:
        test_tuning(Path(d))
        test_board_intent(Path(d))
    print("test_si: ok")
