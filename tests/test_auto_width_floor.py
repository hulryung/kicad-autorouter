"""track_mode auto never goes below the net class width (issue #11).

A CHASSIS class (0.5 mm, 1.0 mm clearance) whose net already carried mostly
0.2 mm tracks was routed at 0.2 mm in auto mode: the board's usual width for
the net beat the class. The class width is a floor now; a wider existing
width still wins, and the neck-down at a fine-pitch pin is kept (a 0.5 mm
PWR rail at a 0.5 mm pitch pin necks to the Default class's 0.2 mm).

Builds the board in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_auto_width_floor.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_auto_width_floor: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000
FINE = (12.0, 14.0)          # U1.1, a 0.5 mm pitch pin on +5V


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def add_pad(fp, number, x, y, net, w=0.8, h=0.8):
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


def add_track(board, net, a, b, w):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pt(*a))
    t.SetEnd(pt(*b))
    t.SetWidth(int(w * MM))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(net)
    board.Add(t)
    return t


def make_board(existing_w=0.2):
    board = pcbnew.BOARD()
    ds = board.GetDesignSettings()
    ns = ds.m_NetSettings
    ns.GetDefaultNetclass().SetTrackWidth(int(0.2 * MM))
    ns.GetDefaultNetclass().SetClearance(int(0.15 * MM))
    ch = pcbnew.NETCLASS("CHASSIS")
    ch.SetTrackWidth(int(0.5 * MM))
    ch.SetClearance(int(1.0 * MM))
    ch.SetViaDiameter(int(0.8 * MM))
    ch.SetViaDrill(int(0.4 * MM))
    ns.SetNetclass("CHASSIS", ch)
    ns.SetNetclassPatternAssignment("CHASSIS", "CHASSIS")
    pwr = pcbnew.NETCLASS("PWR")
    pwr.SetTrackWidth(int(0.5 * MM))
    pwr.SetClearance(int(0.15 * MM))
    pwr.SetViaDiameter(int(0.7 * MM))
    pwr.SetViaDrill(int(0.3 * MM))
    ns.SetNetclass("PWR", pwr)
    ns.SetNetclassPatternAssignment("+5V", "PWR")
    ds.m_TrackMinWidth = int(0.1 * MM)
    net = pcbnew.NETINFO_ITEM(board, "CHASSIS")
    board.Add(net)
    vcc = pcbnew.NETINFO_ITEM(board, "+5V")
    board.Add(vcc)
    for (x1, y1), (x2, y2) in (((0, 0), (40, 0)), ((40, 0), (40, 20)), ((40, 20), (0, 20)), ((0, 20), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)
    # two mounting-hole pads already tied together by thin stubs (three
    # segments, so the net's usual width on the board is existing_w)
    j = pcbnew.FOOTPRINT(board)
    j.SetReference("J1")
    board.Add(j)
    add_pad(j, 1, 3.0, 3.0, net, 1.5, 1.5)
    add_pad(j, 2, 3.0, 9.0, net, 1.5, 1.5)
    for a, b in (((3.0, 3.0), (3.0, 5.0)), ((3.0, 5.0), (3.0, 7.0)), ((3.0, 7.0), (3.0, 9.0))):
        add_track(board, net, a, b, existing_w)
    # the open far end, along the board edge
    k = pcbnew.FOOTPRINT(board)
    k.SetReference("J2")
    board.Add(k)
    add_pad(k, 1, 37.0, 3.0, net, 1.5, 1.5)
    # a fine-pitch part: +5V on U1.1, unconnected neighbours at 0.5 mm, its
    # far end a decoupling pad
    u = pcbnew.FOOTPRINT(board)
    u.SetReference("U1")
    u.SetPosition(pt(12.0, 15.0))
    board.Add(u)
    for i in range(5):
        add_pad(u, i + 1, FINE[0] + 0.5 * i, FINE[1], vcc if i == 0 else None, 0.3, 1.2)
    c = pcbnew.FOOTPRINT(board)
    c.SetReference("C1")
    board.Add(c)
    add_pad(c, 1, 30.0, 15.0, vcc, 1.0, 1.0)
    board.BuildConnectivity()
    return board, net


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def test_rules():
    board, net = make_board()
    hints = extract.BoardHints(board)
    auto = extract.net_rules(board, net, NativeOptions(track_mode="auto"), hints).width
    netclass = extract.net_rules(board, net, NativeOptions(track_mode="netclass"), hints).width
    check("auto keeps the 0.5 mm class over 0.2 mm board tracks", auto == int(0.5 * MM), str(auto))
    check("netclass is 0.5 mm", netclass == int(0.5 * MM), str(netclass))
    board, net = make_board(existing_w=0.8)
    hints = extract.BoardHints(board)
    auto = extract.net_rules(board, net, NativeOptions(track_mode="auto"), hints).width
    check("a wider existing width still wins in auto", auto == int(0.8 * MM), str(auto))


def near(t, xy, mm):
    d = mm * MM
    return any(abs(p.x - xy[0] * MM) <= d and abs(p.y - xy[1] * MM) <= d for p in (t.GetStart(), t.GetEnd()))


def test_route():
    board, net = make_board()
    before = {str(t.m_Uuid.AsString()) for t in board.GetTracks()}
    opts = NativeOptions(pitch_mm=0.1, track_mode="auto", via_mode="netclass", stitch_vias=False,
                         fanout_vias=False, time_limit_s=60)
    job = pipeline.run_job(board, opts)
    new = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"
           and str(t.m_Uuid.AsString()) not in before]
    check("the CHASSIS and +5V connections are routed", new and not job.failed, f"failed {job.failed}")
    body = [t for t in new if not near(t, FINE, 2.0)]
    thin = [(t.GetWidth(), t.GetStart(), t.GetEnd()) for t in body if t.GetWidth() < 0.5 * MM]
    check("away from the fine-pitch pin every new track is at least 0.5 mm", body and not thin, str(thin))
    pin = [t for t in new if near(t, FINE, 0.05)]
    check("the stub at the fine-pitch pin is still necked",
          pin and all(t.GetWidth() < 0.5 * MM for t in pin),
          str([(t.GetWidth(), t.GetStart(), t.GetEnd()) for t in pin]))


def main():
    test_rules()
    test_route()
    print("test_auto_width_floor: all passed")


if __name__ == "__main__":
    main()
