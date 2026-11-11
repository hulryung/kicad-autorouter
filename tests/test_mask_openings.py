"""Openings drawn on a solder mask layer are kept clear:
  - a window with no pad under it is closed to every track on that side: a
    route between two pads on either side of it goes round, and DRC finds no
    "solder mask aperture bridges items with different nets";
  - a window over one net's pad stays open to that net and shut to others.
Builds the boards in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_mask_openings.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_mask_openings: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.grid import BLOCKED
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


def add_pad(board, ref, x, y, net):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    board.Add(fp)
    p = pcbnew.PAD(fp)
    p.SetNumber("1")
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(0.8 * MM), int(0.8 * MM)))
    p.SetPosition(pt(x, y))
    p.SetNet(net)
    fp.Add(p)


def add_window(board, x0, y0, x1, y1):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_RECT)
    s.SetStart(pt(x0, y0))
    s.SetEnd(pt(x1, y1))
    s.SetFilled(True)
    s.SetLayer(pcbnew.F_Mask)
    s.SetWidth(0)
    board.Add(s)


def options():
    return NativeOptions(pitch_mm=0.1, fanout_vias=False, stitch_vias=False,
                         routable_layers=[True, False])


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def inside(t, box):
    """Does a track have a point inside the mm box (sampled)?"""
    a, b = t.GetStart(), t.GetEnd()
    for k in range(51):
        x = (a.x + (b.x - a.x) * k / 50) / MM
        y = (a.y + (b.y - a.y) * k / 50) / MM
        if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
            return True
    return False


def main():
    # 1. a bare window between two pads of SIG, with an OTHER pad's track to pass as well
    board, nets = new_board(["SIG", "OTHER"])
    add_pad(board, "P1", 10, 15, nets["SIG"])
    add_pad(board, "P2", 30, 15, nets["SIG"])
    add_pad(board, "P3", 20, 5, nets["OTHER"])
    add_pad(board, "P4", 20, 25, nets["OTHER"])
    window = (18, 12, 22, 18)
    add_window(board, *window)
    job = pipeline.run_job(board, options())
    board.BuildConnectivity()
    check("both nets routed round the window", board.GetConnectivity().GetUnconnectedCount(True) == 0,
          str(job.failed))
    tracks = [t for t in board.GetTracks() if t.Type() != pcbnew.PCB_VIA_T]
    check("no track inside the window", not any(inside(t, window) for t in tracks))

    # 2. a window over one pad: its own net may use it, the other may not
    board, nets = new_board(["SIG", "OTHER"])
    add_pad(board, "P1", 10, 15, nets["SIG"])
    add_pad(board, "P2", 30, 15, nets["SIG"])
    add_pad(board, "P3", 20, 5, nets["OTHER"])
    add_pad(board, "P4", 20, 25, nets["OTHER"])
    add_window(board, 9, 13, 13, 17)                     # over P1 (SIG)
    problem = extract.extract(board, options())
    sig, other = nets["SIG"].GetNetCode(), nets["OTHER"].GetNetCode()
    grid = problem.grid
    cell = grid.index(0, grid.cell_x(12 * MM), grid.cell_y(16 * MM))     # in the window, off the pad
    own = problem.owners[problem.nets[other].rules.key]
    check("a window over a pad belongs to the pad's net", own[cell] == sig, str(own[cell]))
    check("...and is not shut to that net", own[cell] != BLOCKED)
    job = pipeline.run_job(board, options())
    board.BuildConnectivity()
    check("the pad under the window is still routed", board.GetConnectivity().GetUnconnectedCount(True) == 0,
          str(job.failed))
    print("test_mask_openings: all passed")


if __name__ == "__main__":
    main()
