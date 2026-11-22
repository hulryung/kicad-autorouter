"""Reported gaps from a headless base-board run (Nov 22): USB_DP/USB_DM as a
pair, a per-net width, open pour nets in `failed`, pair lengths in the log.

  <KiCad python3> tests/test_report_fixes.py
"""
import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
print = functools.partial(print, flush=True)

try:
    import pcbnew
except ImportError:
    print("test_report_fixes: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions, NetInfo, NetRules

HERE = Path(__file__).resolve().parent


def test_dm_pairs():
    names = {"USB_DP", "USB_DM", "SWDIO", "STOP", "STON"}
    assert extract.pair_partner("USB_DM", names) == "USB_DP"
    assert extract.pair_partner("USB_DP", names) == "USB_DM"
    assert extract.pair_partner("USB_DP", {"USB_DP", "USB_DN", "USB_DM"}) == "USB_DN"   # DN keeps its place
    assert extract.pair_partner("STOP", names) is None
    print("USB_DP/USB_DM pair: ok")


def test_net_width():
    board = pcbnew.LoadBoard(str(HERE / "fixtures" / "obd2-adapter.kicad_pcb"))
    net = next(n for n in board.GetNetsByNetcode().values() if n.GetNetCode() > 0)
    name = str(net.GetNetname())
    o = NativeOptions()
    base = extract.net_rules(board, net, o).width
    o.net_widths = {name: base + 123_000}
    assert extract.net_rules(board, net, o).width == base + 123_000
    o.net_widths = {name[:1] + "*": base + 77_000}                 # a wildcard
    assert extract.net_rules(board, net, o).width == base + 77_000
    cls = str(net.GetNetClassName() or "Default")
    o.class_widths = {cls: base + 50_000}
    o.net_widths = {name: base + 10_000}                           # the net's own beats its class's
    assert extract.net_rules(board, net, o).width == base + 10_000
    print("net width override: ok")


def test_open_pour_nets_and_pair_lengths():
    board = pcbnew.LoadBoard(str(HERE / "fixtures" / "obd2-adapter.kicad_pcb"))
    # every net with two or more pads and no copper is open: as a pour net it must be reported
    codes = {}
    for pad in board.GetPads():
        codes[pad.GetNetCode()] = codes.get(pad.GetNetCode(), 0) + 1
    multi = sorted(c for c, n in codes.items() if c > 0 and n >= 2)
    assert multi
    pick = multi[:3]
    got = pipeline._open_pour_nets(board, set(pick))
    open_names = {d["net"] for d in got}
    for d in got:
        assert d["pour"] and d["groups"] >= 2 and d["pads"] and d["reason"], d
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    from autorouter.engine.native import apply as native_apply
    for c in pick:
        name = str(board.FindNet(c).GetNetname())
        assert (name in open_names) == (len(native_apply._pad_groups(board, conn, c)) > 1), name
    skipped = pipeline._open_pour_nets(board, set(pick), [str(board.FindNet(pick[0]).GetNetname())])
    assert str(board.FindNet(pick[0]).GetNetname()) not in {d["net"] for d in skipped}
    # two nets made a pair: their lengths and vias as the board has them
    a, b = pick[0], pick[1]
    rules = NetRules(width=200_000, clearance=200_000, via_diameter=600_000, via_drill=300_000)
    nets = {a: NetInfo(a, "X_P", rules, partner=b), b: NetInfo(b, "X_N", rules, partner=a)}
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(0, 0))
    t.SetEnd(pcbnew.VECTOR2I(3_000_000, 4_000_000))
    t.SetNetCode(a)
    board.Add(t)
    rows = pipeline._pair_lengths(board, nets)
    assert len(rows) == 1 and rows[0]["pair"] == "X_N / X_P", rows
    assert rows[0]["length_mm"][1] >= 5.0 and rows[0]["skew_mm"] >= 5.0, rows
    print(f"open pour nets ({len(got)} of {len(pick)}) and pair lengths: ok")


if __name__ == "__main__":
    test_dm_pairs()
    test_net_width()
    test_open_pour_nets_and_pair_lengths()
    print("test_report_fixes: ok")
