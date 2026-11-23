"""Differential pairs whose nets KiCad named after a pin (issue #14):
Net-(J7-TD+) / Net-(J7-TD-), Net-(J6-D+) / Net-(J6-D-), with or without a
sheet path in front. The pair suffix sits before the closing ")".

1. pair_partner: auto names pair on their pin names; Net-(R1-Pad1) /
   Net-(R1-Pad2), a battery's Net-(BT1-+) / Net-(BT1--), pins on different
   footprints and P/N names the rule already refuses (INP/INN) do not.
2. A board in memory: both nets of Net-(J1-D+) / Net-(J1-D-) get each
   other as partner and the pairs group; the Pad1/Pad2 nets do not; a
   routed job lists the pair in stats["pair_lengths"]; si.plan adds a
   pair-skew group for it; board_setup files it under USB.

Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_auto_name_pairs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_auto_name_pairs: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine import board_setup, si
from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000
FAILED = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f": {detail}"), flush=True)
    if not cond:
        FAILED.append(name)


def test_matcher():
    names = {"Net-(J7-TD+)", "Net-(J7-TD-)", "Net-(J6-D+)", "Net-(J6-D-)",
             "/eth/Net-(J8-RX_P)", "/eth/Net-(J8-RX_N)", "Net-(U1-DP)", "Net-(U1-DM)",
             "Net-(R1-Pad1)", "Net-(R1-Pad2)", "Net-(BT1-+)", "Net-(BT1--)",
             "Net-(U2-INP)", "Net-(U2-INN)", "Net-(J9-TX+)", "Net-(J10-TX-)",
             "Net-(Q1-P)", "Net-(Q1-N)", "USB_DP", "USB_DN", "STOP", "STON"}
    pp = extract.pair_partner
    for a, b in (("Net-(J7-TD+)", "Net-(J7-TD-)"), ("Net-(J6-D+)", "Net-(J6-D-)"),
                 ("/eth/Net-(J8-RX_P)", "/eth/Net-(J8-RX_N)"), ("Net-(U1-DP)", "Net-(U1-DM)"),
                 ("USB_DP", "USB_DN")):
        check(f"{a} pairs with {b}", pp(a, names) == b and pp(b, names) == a, f"{pp(a, names)} / {pp(b, names)}")
    for n in ("Net-(R1-Pad1)", "Net-(R1-Pad2)", "Net-(BT1-+)", "Net-(BT1--)", "Net-(U2-INP)",
              "Net-(U2-INN)", "Net-(J9-TX+)", "Net-(J10-TX-)", "Net-(Q1-P)", "Net-(Q1-N)", "STOP", "STON"):
        check(f"{n} has no partner", pp(n, names) is None, str(pp(n, names)))
    # a sheet path must match too: /a/Net-(J7-TD+) does not pair with the root's Net-(J7-TD-)
    check("auto names on different sheets do not pair",
          pp("/a/Net-(J7-TD+)", {"/a/Net-(J7-TD+)", "Net-(J7-TD-)"}) is None)
    check("board_setup files Net-(J6-D+) under USB", board_setup.pair_family("Net-(J6-D+)") == "USB",
          str(board_setup.pair_family("Net-(J6-D+)")))


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def add_pad(fp, number, x, y, net, w=0.6, h=0.3):
    p = pcbnew.PAD(fp)
    p.SetNumber(str(number))
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(w * MM), int(h * MM)))
    p.SetPosition(pt(x, y))
    p.SetNet(net)
    fp.Add(p)


def board_with_auto_pair():
    """30 x 20 mm, 2 layers: J1 D+/D- to U1, and R1's two pads to R2."""
    board = pcbnew.BOARD()
    ds = board.GetDesignSettings()
    ds.m_NetSettings.GetDefaultNetclass().SetTrackWidth(int(0.2 * MM))
    ds.m_NetSettings.GetDefaultNetclass().SetClearance(int(0.15 * MM))
    nets = {}
    for name in ("Net-(J1-D+)", "Net-(J1-D-)", "Net-(R1-Pad1)", "Net-(R1-Pad2)"):
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    for (x1, y1), (x2, y2) in (((0, 0), (30, 0)), ((30, 0), (30, 20)), ((30, 20), (0, 20)), ((0, 20), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)
    for ref, x, pads in (("J1", 4, (("Net-(J1-D+)", 9.6), ("Net-(J1-D-)", 10.4))),
                         ("U1", 26, (("Net-(J1-D+)", 9.6), ("Net-(J1-D-)", 10.4))),
                         ("R1", 4, (("Net-(R1-Pad1)", 4.0), ("Net-(R1-Pad2)", 16.0))),
                         ("R2", 26, (("Net-(R1-Pad1)", 4.0), ("Net-(R1-Pad2)", 16.0)))):
        fp = pcbnew.FOOTPRINT(board)
        fp.SetReference(ref)
        board.Add(fp)
        for i, (name, y) in enumerate(pads, 1):
            add_pad(fp, i, x, y, nets[name])
    board.BuildConnectivity()
    return board, nets


def test_board():
    board, nets = board_with_auto_pair()
    problem = extract.extract(board, NativeOptions(pitch_mm=0.1))
    code = {n: nets[n].GetNetCode() for n in nets}
    p, n = problem.nets[code["Net-(J1-D+)"]], problem.nets[code["Net-(J1-D-)"]]
    check("Net-(J1-D+) / Net-(J1-D-) are partners", p.partner == n.code and n.partner == p.code,
          f"{p.partner} / {n.partner}")
    check("both are in the pairs group", p.group == n.group == si.GROUP_PAIRS, f"{p.group} / {n.group}")
    r1, r2 = problem.nets[code["Net-(R1-Pad1)"]], problem.nets[code["Net-(R1-Pad2)"]]
    check("Net-(R1-Pad1) / Net-(R1-Pad2) are not a pair", not r1.partner and not r2.partner
          and r1.group != si.GROUP_PAIRS, f"{r1.partner} {r2.partner} {r1.group}")
    plan = si.plan(board, si.SiConfig())
    pair_groups = [g["nets"] for g in plan.length_groups if g.get("pair")]
    check("si.plan matches the pair's P and N", pair_groups == [["Net-(J1-D+)", "Net-(J1-D-)"]], str(pair_groups))

    board, nets = board_with_auto_pair()
    job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1))
    pairs = [d["pair"] for d in job.stats.get("pair_lengths", [])]
    check("the routed job lists the pair's lengths", len(pairs) == 1 and "Net-(J1-D+)" in pairs[0], str(pairs))
    check("everything routed", not job.failed, str(job.failed))


def main():
    test_matcher()
    test_board()
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
        sys.exit(1)
    print("test_auto_name_pairs: all ok")


if __name__ == "__main__":
    main()
