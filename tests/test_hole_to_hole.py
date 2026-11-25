"""No via the router drops comes within the board's hole-to-hole minimum of
another hole, of its own net included. A GND pad on F.Cu with the GND plane
on B.Cu gets a fanout via; a locked GND via the designer placed (with or
without a stub to the pad) sits next to it. The new via keeps its drill
0.25 mm (Board Setup's hole-to-hole) from that via's drill, or is not added
at all when the designer's via already ties the pad to the plane. Same in
shove mode, and with a signal to route through the area. Builds the board
in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_hole_to_hole.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_hole_to_hole: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.compat import zone_filler
from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000
H2H = 0.25


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def add_pad(fp, number, x, y, net, w=0.6, h=0.6):
    p = pcbnew.PAD(fp)
    p.SetNumber(str(number))
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(w * MM), int(h * MM)))
    p.SetPosition(pt(x, y))
    p.SetNet(net)
    fp.Add(p)
    return p


def make_board(via_at, stub: bool):
    board = pcbnew.BOARD()
    ds = board.GetDesignSettings()
    ds.m_HoleToHoleMin = int(H2H * MM)
    ns = ds.m_NetSettings
    ns.GetDefaultNetclass().SetTrackWidth(int(0.2 * MM))
    ns.GetDefaultNetclass().SetClearance(int(0.15 * MM))
    ns.GetDefaultNetclass().SetViaDiameter(int(0.5 * MM))
    ns.GetDefaultNetclass().SetViaDrill(int(0.25 * MM))
    gnd = pcbnew.NETINFO_ITEM(board, "GND")
    board.Add(gnd)
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    for (x1, y1), (x2, y2) in (((0, 0), (20, 0)), ((20, 0), (20, 15)), ((20, 15), (0, 15)), ((0, 15), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("U1")
    board.Add(fp)
    add_pad(fp, 1, 10.0, 7.0, gnd)
    add_pad(fp, 2, 3.0, 3.0, gnd)
    add_pad(fp, 3, 3.0, 7.5, sig)
    add_pad(fp, 4, 17.0, 7.5, sig)
    x, y = via_at
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pt(x, y))
    v.SetWidth(int(0.5 * MM))
    v.SetDrill(int(0.25 * MM))
    v.SetNet(gnd)
    v.SetLocked(True)
    board.Add(v)
    if stub:
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pt(10.0, 7.0))
        t.SetEnd(pt(x, y))
        t.SetWidth(int(0.2 * MM))
        t.SetLayer(pcbnew.F_Cu)
        t.SetNet(gnd)
        t.SetLocked(True)
        board.Add(t)
    z = pcbnew.ZONE(board)
    z.SetLayer(pcbnew.B_Cu)
    z.SetNetCode(gnd.GetNetCode())
    z.SetMinThickness(int(0.2 * MM))
    z.SetLocalClearance(int(0.2 * MM))
    o = z.Outline()
    o.NewOutline()
    for px, py in ((0.5, 0.5), (19.5, 0.5), (19.5, 14.5), (0.5, 14.5)):
        o.Append(int(px * MM), int(py * MM))
    board.Add(z)
    zone_filler(board).Fill(board.Zones())
    board.BuildConnectivity()
    return board, gnd.GetNetCode()


def hole_gaps(board):
    """(gap mm, a, b) for every pair of vias whose drills are closer than the minimum."""
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    bad = []
    for i, a in enumerate(vias):
        for b in vias[i + 1:]:
            pa, pb = a.GetPosition(), b.GetPosition()
            gap = math.hypot(pa.x - pb.x, pa.y - pb.y) - (a.GetDrillValue() + b.GetDrillValue()) / 2
            if gap < H2H * MM - 1_000:
                bad.append((round(gap / MM, 3), (pa.x / MM, pa.y / MM), (pb.x / MM, pb.y / MM)))
    return bad


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    spots = [(10.0 + r * math.cos(a), 7.0 + r * math.sin(a))
             for a in (k * math.pi / 4 for k in range(8)) for r in (0.75, 0.6)]
    for shove in (False, True):
        mode = "shove" if shove else "plain"
        worst = []
        opened = 0
        for spot in spots:
            board, gnd = make_board(spot, stub=False)
            job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1, shove=shove, stitch_vias=False))
            worst += hole_gaps(board)
            zone_filler(board).Fill(board.Zones())
            board.BuildConnectivity()
            vias = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
            print(f"     {mode} via at ({spot[0]:.2f}, {spot[1]:.2f}): {vias} vias, "
                  f"{board.GetConnectivity().GetUnconnectedCount(True)} open, failed {job.failed}")
            opened += board.GetConnectivity().GetUnconnectedCount(True)
        check(f"{mode}: every pad joined", not opened, str(opened))
        check(f"{mode}: fanout vias keep the hole-to-hole minimum from a locked same-net via", not worst, str(worst))
        board, gnd = make_board(spots[1], stub=True)
        before = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
        job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1, shove=shove, stitch_vias=False))
        near = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetCode() == gnd
                and math.hypot(t.GetPosition().x - 10 * MM, t.GetPosition().y - 7 * MM) < 2 * MM]
        check(f"{mode}: the designer's stubbed via is the pad's fanout (no second via)", len(near) == 1,
              str([(t.GetPosition().x / MM, t.GetPosition().y / MM) for t in near]))
        check(f"{mode}: no hole-to-hole violation", not hole_gaps(board), str(hole_gaps(board)))
    print("test_hole_to_hole: all passed")


if __name__ == "__main__":
    main()
