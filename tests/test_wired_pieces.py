"""Track the board already has, as the router reads it:
  - a half-done route that leaves its pad the long way round and stops short
    of the other pad is continued from its open end (the search steers by
    the piece's ends, not only by its pads);
  - a route that joins the end of a track meets it at the end point, with no
    tail of copper left past the join.
Builds the boards in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_wired_pieces.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_wired_pieces: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def new_board(net_names):
    board = pcbnew.BOARD()
    nets = {}
    for name in net_names:
        nets[name] = pcbnew.NETINFO_ITEM(board, name)
        board.Add(nets[name])
    for (x1, y1), (x2, y2) in (((0, 0), (40, 0)), ((40, 0), (40, 30)), ((40, 30), (0, 30)), ((0, 30), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)
    return board, nets


def add_pad(fp, number, x, y, net, w=0.8, h=0.8):
    p = pcbnew.PAD(fp)
    p.SetNumber(str(number))
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(w * MM), int(h * MM)))
    p.SetPosition(pt(x, y))
    p.SetNet(net)
    fp.Add(p)


def add_track(board, net, a, b, width=0.25):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pt(*a))
    t.SetEnd(pt(*b))
    t.SetWidth(int(width * MM))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNetCode(net.GetNetCode())
    board.Add(t)


def length(t):
    return math.hypot(t.GetEnd().x - t.GetStart().x, t.GetEnd().y - t.GetStart().y)


def options():
    return NativeOptions(pitch_mm=0.1, fanout_vias=False, stitch_vias=False)


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    # 1. the long way round, 3.6 mm short of the first pad
    board, nets = new_board(["SIG"])
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("U1")
    board.Add(fp)
    add_pad(fp, 1, 5, 10, nets["SIG"])
    add_pad(fp, 2, 35, 10, nets["SIG"])
    way = ((35, 10), (35, 20), (8, 20), (5, 17), (5, 13.6))
    for a, b in zip(way, way[1:]):
        add_track(board, nets["SIG"], a, b)
    board.BuildConnectivity()
    before = {str(t.m_Uuid.AsString()) for t in board.GetTracks()}
    pipeline.run_job(board, options(), apply_paths=True)
    board.BuildConnectivity()
    new = sum(length(t) for t in board.GetTracks() if str(t.m_Uuid.AsString()) not in before) / MM
    check("the open end is joined", board.GetConnectivity().GetUnconnectedCount(True) == 0)
    check("...by the few millimetres that were missing", new < 6.0, f"{new:.1f} mm of new track")

    # 2. a route to the end of a track: no tail past the join. The old track
    # stops between grid points, 30 um off the cell the route ends on
    board, nets = new_board(["SIG"])
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("U1")
    board.Add(fp)
    add_pad(fp, 1, 5, 10, nets["SIG"])
    add_pad(fp, 2, 30, 10, nets["SIG"])
    add_track(board, nets["SIG"], (30, 10), (30, 16), 0.2)
    add_track(board, nets["SIG"], (30, 16), (20.03, 16), 0.2)
    board.BuildConnectivity()
    pipeline.run_job(board, options(), apply_paths=True)
    board.BuildConnectivity()
    check("the route reaches the old track", board.GetConnectivity().GetUnconnectedCount(True) == 0)
    ends = {}
    for t in board.GetTracks():
        for p in (t.GetStart(), t.GetEnd()):
            ends[(p.x, p.y)] = ends.get((p.x, p.y), 0) + 1
    pads = {(p.GetPosition().x, p.GetPosition().y) for p in board.GetPads()}
    loose = [(x / MM, y / MM) for (x, y), n in ends.items() if n == 1 and (x, y) not in pads
             and not any(p.HitTest(pcbnew.VECTOR2I(x, y)) for p in board.GetPads())]
    check("every track end meets another", not loose, str(loose))
    print("test_wired_pieces: all passed")


if __name__ == "__main__":
    main()
