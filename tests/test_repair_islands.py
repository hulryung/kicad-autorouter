"""repair_islands: a piece of a pour cut off with only its own pads in it (a
signal routed round two GND pads) gets a stub and via to the net's connected
fill. Builds a small two-layer board in memory. Needs KiCad's Python; skips
otherwise.

  <KiCad python3> tests/test_repair_islands.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_repair_islands: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import apply

MM = 1_000_000


def mm(v):
    return int(round(v * MM))


def pt(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def make_board(ring=True, extra=(), surface=True, thermal_pth=False, graphic=False, keepout=False):
    board = pcbnew.BOARD()
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    for (x1, y1), (x2, y2) in (((0, 0), (20, 0)), ((20, 0), (20, 20)), ((20, 20), (0, 20)), ((0, 20), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(mm(0.1))
        board.Add(s)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("C1")
    board.Add(fp)
    pads = []
    for n, (x, y) in enumerate(((9, 10), (11, 10), (3, 3), (17, 3), (3, 17)) + tuple(extra), 1):
        p = pcbnew.PAD(fp)
        p.SetNumber(str(n))
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(p.SMDMask())
        p.SetSize(pcbnew.VECTOR2I(mm(0.6), mm(0.6)))
        p.SetPosition(pt(x, y))
        p.SetNet(gnd)
        fp.Add(p)
        pads.append(p)
    for layer in (pcbnew.F_Cu, pcbnew.B_Cu) if surface else (pcbnew.B_Cu,):
        z = pcbnew.ZONE(board)
        z.SetLayer(layer)
        z.SetNetCode(gnd.GetNetCode())
        z.SetMinThickness(mm(0.2))
        z.SetLocalClearance(mm(0.2))
        z.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL if thermal_pth else pcbnew.ZONE_CONNECTION_FULL)
        o = z.Outline()
        o.NewOutline()
        for x, y in ((0.5, 0.5), (19.5, 0.5), (19.5, 19.5), (0.5, 19.5)):
            o.Append(mm(x), mm(y))
        board.Add(z)
    if thermal_pth:
        # the surface pour reaches the plane through a through-hole pad on
        # thermal spokes: its centre is in neither fill
        p = pcbnew.PAD(fp)
        p.SetNumber("T")
        p.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        p.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        p.SetLayerSet(p.PTHMask())
        p.SetSize(pcbnew.VECTOR2I(mm(1.6), mm(1.6)))
        p.SetDrillSize(pcbnew.VECTOR2I(mm(0.8), mm(0.8)))
        p.SetPosition(pt(5, 5))
        p.SetNet(gnd)
        fp.Add(p)
    else:
        # the surface pour reaches the plane through a via in its main part
        v = pcbnew.PCB_VIA(board)
        v.SetPosition(pt(5, 5))
        v.SetViaType(pcbnew.VIATYPE_THROUGH)
        v.SetWidth(mm(0.6))
        v.SetDrill(mm(0.3))
        v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        v.SetNetCode(gnd.GetNetCode())
        board.Add(v)
    if not surface:
        # no surface pour: one pad reaches the plane by a track to the via
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pt(3, 3))
        t.SetEnd(pt(5, 5))
        t.SetWidth(mm(0.25))
        t.SetLayer(pcbnew.F_Cu)
        t.SetNetCode(gnd.GetNetCode())
        board.Add(t)
    if graphic:
        # a copper drawing through the island, where the nearest via spots are
        g = pcbnew.PCB_SHAPE(board)
        g.SetShape(pcbnew.SHAPE_T_SEGMENT)
        g.SetStart(pt(7.6, 10.3))
        g.SetEnd(pt(12.4, 10.3))
        g.SetLayer(pcbnew.B_Cu)
        g.SetWidth(mm(0.3))
        board.Add(g)
    if keepout:
        k = pcbnew.ZONE(board)
        k.SetIsRuleArea(True)
        k.SetDoNotAllowVias(True)
        k.SetDoNotAllowTracks(False)
        k.SetLayerSet(pcbnew.LSET.AllCuMask())
        o = k.Outline()
        o.NewOutline()
        for x, y in ((7.2, 8.2), (12.8, 8.2), (12.8, 10.6), (7.2, 10.6)):
            o.Append(mm(x), mm(y))
        board.Add(k)
    if ring:
        # a signal all the way round the two pads: their pour is an island
        corners = ((7, 8), (13, 8), (13, 12), (7, 12), (7, 8))
        for (x1, y1), (x2, y2) in zip(corners, corners[1:]):
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pt(x1, y1))
            t.SetEnd(pt(x2, y2))
            t.SetWidth(mm(0.25))
            t.SetLayer(pcbnew.F_Cu)
            t.SetNetCode(sig.GetNetCode())
            board.Add(t)
    return board, gnd.GetNetCode(), sig.GetNetCode()


def unconnected(board):
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    return board.GetConnectivity().GetUnconnectedCount(True)


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    board, gnd, sig = make_board()
    check("the ring cuts the pads off", unconnected(board) > 0)
    live = []
    joined = apply.repair_islands(board, live)
    check("one island joined", joined == 1, f"joined {joined}")
    vias = [i for i in live if i.GetClass() == "PCB_VIA"]
    check("one via added", len(vias) == 1, str([i.GetClass() for i in live]))
    x, y = vias[0].GetPosition().x / MM, vias[0].GetPosition().y / MM
    check("via inside the ring", 7.3 < x < 12.7 and 8.3 < y < 11.7, f"{x},{y}")
    check("via clear of the pads", all(not p.HitTest(vias[0].GetPosition()) for p in board.GetPads()))
    check("added items on the GND net", all(i.GetNetCode() == gnd for i in live))
    check("nothing left unconnected", unconnected(board) == 0)
    again = apply.repair_islands(board, [])
    check("second call adds nothing", again == 0, f"joined {again}")

    board, gnd, sig = make_board(ring=False)
    before = len(list(board.GetTracks()))
    check("no island: nothing to do", apply.repair_islands(board, []) == 0 and len(list(board.GetTracks())) == before)

    # the island holds more pads than the connected part: still the island
    board, gnd, sig = make_board(extra=((10, 9), (10, 11), (8.5, 11), (11.5, 9)))
    live = []
    check("an island with most of the pads is joined", apply.repair_islands(board, live) == 1 and unconnected(board) == 0)
    v = [i for i in live if i.GetClass() == "PCB_VIA"][0].GetPosition()
    check("...from inside the ring", 7.3 < v.x / MM < 12.7 and 8.3 < v.y / MM < 11.7, f"{v.x / MM},{v.y / MM}")

    # the plane held by a thermal-relief through-hole pad
    board, gnd, sig = make_board(thermal_pth=True)
    check("thermal through-hole pad: the plane is found", apply.repair_islands(board, []) == 1 and unconnected(board) == 0)

    # pads no pour reaches are not islands: the router fans those out
    board, gnd, sig = make_board(ring=False, surface=False)
    before = len(list(board.GetTracks()))
    check("pads outside any pour are left alone", unconnected(board) > 0 and apply.repair_islands(board, []) == 0
          and len(list(board.GetTracks())) == before)

    # copper drawings are obstacles
    board, gnd, sig = make_board(graphic=True)
    live = []
    check("joined past a copper drawing", apply.repair_islands(board, live) == 1 and unconnected(board) == 0)
    via = [i for i in live if i.GetClass() == "PCB_VIA"][0]
    g = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.B_Cu][0]
    check("via clear of the drawing", not via.GetEffectiveShape(pcbnew.B_Cu).Collide(g.GetEffectiveShape(pcbnew.B_Cu), mm(0.19)))

    # a via keepout: no part of the via's pad inside it
    board, gnd, sig = make_board(keepout=True)
    live = []
    check("joined beside a via keepout", apply.repair_islands(board, live) == 1 and unconnected(board) == 0)
    v = [i for i in live if i.GetClass() == "PCB_VIA"][0]
    r, c = v.GetWidth(pcbnew.F_Cu) / 2 / MM, v.GetPosition()
    check("via pad outside the keepout", c.y / MM - r >= 10.6 - 0.001, f"{c.x / MM},{c.y / MM} r {r}")
    print("test_repair_islands: all passed")


if __name__ == "__main__":
    main()
