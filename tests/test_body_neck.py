"""An IC pin's track runs straight on past its pad before it turns: the
short neck a designer draws on every pin of a QFP when fanning it out,
instead of a bend at the pad's end. Where the lane past the pad is shut,
the plain short exit is still used.
Builds the board in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_body_neck.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_body_neck: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def add_pad(fp, number, x, y, net, w, h):
    p = pcbnew.PAD(fp)
    p.SetNumber(str(number))
    p.SetShape(pcbnew.PAD_SHAPE_RECT)
    p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    p.SetLayerSet(p.SMDMask())
    p.SetSize(pcbnew.VECTOR2I(int(w * MM), int(h * MM)))
    p.SetPosition(pt(x, y))
    if net is not None:
        p.SetNet(net)
    fp.Add(p)


def make_board(blocker):
    board = pcbnew.BOARD()
    nets = {}
    for name in ("SIG", "OTHER"):
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
    # a 16-pin, 0.5 mm pitch body: two rows of upright pads, 6 mm apart
    u = pcbnew.FOOTPRINT(board)
    u.SetReference("U1")
    u.SetPosition(pt(11.75, 15))
    board.Add(u)
    for i in range(8):
        add_pad(u, i + 1, 10 + 0.5 * i, 12, nets["SIG"] if i == 3 else None, 0.3, 1.5)
        add_pad(u, 16 - i, 10 + 0.5 * i, 18, None, 0.3, 1.5)
    far = pcbnew.FOOTPRINT(board)
    far.SetReference("P1")
    board.Add(far)
    add_pad(far, 1, 25, 5, nets["SIG"], 0.8, 0.8)
    if blocker:
        # another net's pad right outside the row, 0.75 mm past the pin's pad
        b = pcbnew.FOOTPRINT(board)
        b.SetReference("C1")
        board.Add(b)
        add_pad(b, 1, 11.5, 10.1, nets["OTHER"], 1.0, 0.8)
        add_pad(b, 2, 11.5, 8.5, nets["OTHER"], 1.0, 0.8)
    return board, nets


def neck(board, net):
    """How far the upright run from U1.4 (x = 11.5) goes past the pad's end (y = 11.25), in mm."""
    top = 11.25
    for t in board.GetTracks():
        if t.Type() == pcbnew.PCB_VIA_T or t.GetNetCode() != net.GetNetCode():
            continue
        a, b = t.GetStart(), t.GetEnd()
        if abs(a.x - b.x) < 1000 and abs(a.x / MM - 11.5) < 0.08 and max(a.y, b.y) / MM > 10.0:
            top = min(top, a.y / MM, b.y / MM)
    return 11.25 - top


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    opts = dict(pitch_mm=0.1, fanout_vias=False, stitch_vias=False, routable_layers=[True, False])
    board, nets = make_board(blocker=False)
    job = pipeline.run_job(board, NativeOptions(**opts))
    board.BuildConnectivity()
    check("the pin is routed", board.GetConnectivity().GetUnconnectedCount(True) == 0, str(job.failed))
    n = neck(board, nets["SIG"])
    check("its track runs straight past the pad before turning", n >= 0.55, f"{n:.2f} mm")

    board, nets = make_board(blocker=True)
    job = pipeline.run_job(board, NativeOptions(**opts))
    board.BuildConnectivity()
    check("with a part right outside the row the pin is still routed",
          board.GetConnectivity().GetUnconnectedCount(True) == 0, str(job.failed))
    print("test_body_neck: all passed")


if __name__ == "__main__":
    main()
