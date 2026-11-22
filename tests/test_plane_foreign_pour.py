"""A plane net's pads under a higher-priority pour of another net: a full-
board +3V3 pour on In2 with a +12V pour (priority 1) over the right half.
The +3V3 outline still covers the pads there, but its fill does not: a pad
via there lands in the +12V fill, an island of its own. Those pads (two
SMD caps, a through-hole pin) must instead be routed as strays to the
nearest piece of the +3V3 fill, and leave no +3V3 via in the +12V fill.
Builds a four-layer board in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_plane_foreign_pour.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_plane_foreign_pour: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000
W, H = 40, 30
CORRIDOR = ((22, 0), (W, 0), (W, H), (22, H))         # the +12V pour, over the +3V3 plane


def pt(x, y):
    return pcbnew.VECTOR2I(int(x * MM), int(y * MM))


def zone(board, net, layer, pts, prio):
    z = pcbnew.ZONE(board)
    z.SetLayer(layer)
    z.SetNetCode(net.GetNetCode())
    z.SetAssignedPriority(prio)
    z.SetMinThickness(int(0.2 * MM))
    z.SetLocalClearance(int(0.25 * MM))
    z.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    o = z.Outline()
    o.NewOutline()
    for x, y in pts:
        o.Append(int(x * MM), int(y * MM))
    board.Add(z)
    return z


def make_board():
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    v33 = pcbnew.NETINFO_ITEM(board, "+3V3")
    board.Add(v33)
    v12 = pcbnew.NETINFO_ITEM(board, "+12V")
    board.Add(v12)
    for (x1, y1), (x2, y2) in (((0, 0), (W, 0)), ((W, 0), (W, H)), ((W, H), (0, H)), ((0, H), (0, 0))):
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
    smd = [((4, 4), v33), ((8, 22), v33), ((14, 12), v33), ((16, 25), v33),   # under the +3V3 fill
           ((30, 13), v33), ((32, 16), v33),                                  # under the +12V fill
           ((26, 5), v12), ((35, 24), v12)]
    for n, ((x, y), net) in enumerate(smd, 1):
        p = pcbnew.PAD(fp)
        p.SetNumber(str(n))
        p.SetShape(pcbnew.PAD_SHAPE_RECT)
        p.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        p.SetLayerSet(p.SMDMask())
        p.SetSize(pcbnew.VECTOR2I(int(0.8 * MM), int(0.8 * MM)))
        p.SetPosition(pt(x, y))
        p.SetNet(net)
        fp.Add(p)
    p = pcbnew.PAD(fp)                            # a connector pin of +3V3 inside the +12V pour
    p.SetNumber("J")
    p.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    p.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
    p.SetLayerSet(p.PTHMask())
    p.SetSize(pcbnew.VECTOR2I(int(1.6 * MM), int(1.6 * MM)))
    p.SetDrillSize(pcbnew.VECTOR2I(int(0.9 * MM), int(0.9 * MM)))
    p.SetPosition(pt(36, 8))
    p.SetNet(v33)
    fp.Add(p)
    zone(board, v33, pcbnew.In2_Cu, ((0.5, 0.5), (W - 0.5, 0.5), (W - 0.5, H - 0.5), (0.5, H - 0.5)), 0)
    zone(board, v12, pcbnew.In2_Cu, CORRIDOR, 1)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    return board, v33.GetNetCode(), v12.GetNetCode()


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    board, v33, v12 = make_board()
    job = pipeline.run_job(board, NativeOptions(pitch_mm=0.1), apply_paths=True)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    check("every pad of both nets is joined", conn.GetUnconnectedCount(True) == 0,
          f"{conn.GetUnconnectedCount(True)} open, failed {job.failed}")
    x0, y0 = CORRIDOR[0]
    x1, y1 = CORRIDOR[2]
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == v33]

    def layers_at(p):
        return {t.GetLayer() for t in tracks if t.GetStart() == p or t.GetEnd() == p}

    # a +3V3 via in the +12V pour is only a layer change of a run (a track
    # off the pads' layer leaves it), never a pad's dead-end plane via
    dead = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetCode() == v33
            and x0 * MM < t.GetPosition().x < x1 * MM and y0 * MM < t.GetPosition().y < y1 * MM
            and not layers_at(t.GetPosition()) - {pcbnew.F_Cu}]
    check("no +3V3 plane via inside the +12V pour", not dead,
          str([(t.GetPosition().x / MM, t.GetPosition().y / MM) for t in dead]))
    plane = [z for z in board.Zones() if z.GetNetCode() == v33][0].GetFilledPolysList(pcbnew.In2_Cu)
    landed = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetCode() == v33
              and t.GetPosition().x < x0 * MM and plane.Contains(t.GetPosition())]
    check("the strays' run drops into the +3V3 pour", landed)
    in2 = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == v33
           and t.GetLayer() == pcbnew.In2_Cu]
    check("the strays' runs stay off the plane layer", not in2, f"{len(in2)} +3V3 tracks on In2")
    print("test_plane_foreign_pour: all passed")


if __name__ == "__main__":
    main()
