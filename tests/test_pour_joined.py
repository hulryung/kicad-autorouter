"""Pads a copper pour of their net already joins get no track: a small VCC
pour on F.Cu covers three VCC pads and the router must leave them to it,
whether the board was saved with the pour filled or not (KiCad keeps the
fills it last computed; a pour drawn but never filled, or filled before
tracks were moved, is common). A VCC pad outside the pour is joined to the
pour's edge, not to a pad inside it with a track across the fill: for a
pour net (the pour covers most of its pads) and for a routed net (it covers
only some). When another net's route then cuts the strip of pour that track
ended in off the rest, the join is routed again, pad to pad. Builds the board in memory. Needs KiCad's Python; skips
otherwise.

  <KiCad python3> tests/test_pour_joined.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_pour_joined: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000


def pt(x, y):
    return pcbnew.VECTOR2I(int(x * MM), int(y * MM))


def make_board(filled: bool, cut: bool = False):
    board = pcbnew.BOARD()
    vcc = pcbnew.NETINFO_ITEM(board, "VCC")
    board.Add(vcc)
    sig = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(sig)
    for (x1, y1), (x2, y2) in (((0, 0), (40, 0)), ((40, 0), (40, 30)), ((40, 30), (0, 30)), ((0, 30), (0, 0))):
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
    spots = [((4, 4), vcc), ((10, 6), vcc), ((6, 10), vcc), ((30, 20), sig), ((36, 26), sig)]
    if cut:
        # the signal's straight route runs just inside the pour's east edge
        spots = spots[:3] + [((12.4, 3), sig), ((12.4, 9), sig)]
    for n, ((x, y), net) in enumerate(spots, 1):
        p = pcbnew.PAD(fp)
        p.SetNumber(str(n))
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(p.SMDMask())
        p.SetSize(pcbnew.VECTOR2I(int(0.8 * MM), int(0.8 * MM)))
        p.SetPosition(pt(x, y))
        p.SetNet(net)
        fp.Add(p)
    z = pcbnew.ZONE(board)                         # a local pour, well under half the board
    z.SetLayer(pcbnew.F_Cu)
    z.SetNetCode(vcc.GetNetCode())
    z.SetMinThickness(int(0.2 * MM))
    z.SetLocalClearance(int((0.3 if cut else 0.2) * MM))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    o = z.Outline()
    o.NewOutline()
    for x, y in ((2, 2), (13, 2), (13, 13), (2, 13)):
        o.Append(int(x * MM), int(y * MM))
    board.Add(z)
    if filled:
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    return board, vcc.GetNetCode()


def add_pads(board, spots):
    fp = board.FindFootprintByReference("U1")
    for k, (x, y) in enumerate(spots):
        p = pcbnew.PAD(fp)
        p.SetNumber(str(10 + k))
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(p.SMDMask())
        p.SetSize(pcbnew.VECTOR2I(int(0.8 * MM), int(0.8 * MM)))
        p.SetPosition(pt(x, y))
        p.SetNet(board.FindNet("VCC"))
        fp.Add(p)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()


def over_pour(board, code):
    """Length (nm) of the net's tracks lying inside its own F.Cu fill."""
    import math
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    poly = [z for z in board.Zones() if z.GetNetCode() == code][0].GetFilledPolysList(pcbnew.F_Cu)
    inside = 0.0
    for t in board.GetTracks():
        if t.GetNetCode() != code or t.GetClass() != "PCB_TRACK":
            continue
        s, e = t.GetStart(), t.GetEnd()
        length = math.hypot(e.x - s.x, e.y - s.y)
        n = max(1, int(length / 50_000))
        hits = sum(1 for k in range(n + 1)
                   if poly.Contains(pcbnew.VECTOR2I(int(s.x + (e.x - s.x) * k / n), int(s.y + (e.y - s.y) * k / n))))
        inside += length * hits / (n + 1)
    return inside


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    for filled in (True, False):
        board, vcc = make_board(filled)
        job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1), apply_paths=True)
        vcc_tracks = [t for t in board.GetTracks() if t.GetNetCode() == vcc and t.GetClass() == "PCB_TRACK"]
        sig_tracks = [t for t in board.GetTracks() if t.GetNetCode() != vcc and t.GetClass() == "PCB_TRACK"]
        what = "filled" if filled else "unfilled"
        check(f"{what}: the signal is routed", sig_tracks and not job.failed, str(job.failed))
        check(f"{what}: no track over the pour", not vcc_tracks, f"{len(vcc_tracks)} VCC tracks")
    # a pad outside the pour: to the pour's edge. Pour net (3 of 4 pads
    # covered), then a routed net (3 of 7 covered)
    for label, spots in (("pour net", [(20, 6)]), ("routed net", [(20, 6), (24, 8), (20, 10), (26, 4)])):
        board, vcc = make_board(True)
        add_pads(board, spots)
        job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1), apply_paths=True)
        board.BuildConnectivity()
        check(f"{label}: the outside pads are joined", board.GetConnectivity().GetUnconnectedCount(True) == 0,
              str(job.failed))
        inside = over_pour(board, vcc)
        check(f"{label}: the track stops at the pour", inside < 500_000, f"{inside / 1e6:.2f} mm over the fill")
    # the signal, routed in the same run, cuts the pour's edge strip off: the
    # track that ended there must still reach the pads
    board, vcc = make_board(True, cut=True)
    add_pads(board, [(20, 6)])
    job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1), apply_paths=True)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    check("a join the refill took apart is routed again", board.GetConnectivity().GetUnconnectedCount(True) == 0
          and not job.failed, f"failed {job.failed} stats {job.stats.get('pour_joins_redone')}")
    print("test_pour_joined: all passed")


if __name__ == "__main__":
    main()
