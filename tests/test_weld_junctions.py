"""weld_junctions: every junction of routed copper becomes a shared end point, so
KiCad's Select Connection (U) and the interactive router can walk it. Builds a
small board in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_weld_junctions.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_weld_junctions: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import apply

MM = 1_000_000


def mm(v):
    return int(round(v * MM))


def make_board():
    board = pcbnew.BOARD()
    net = pcbnew.NETINFO_ITEM(board, "N1")
    board.Add(net)
    other = pcbnew.NETINFO_ITEM(board, "N2")
    board.Add(other)
    return board, net.GetNetCode(), other.GetNetCode()


def track(board, net, x1, y1, x2, y2, width=0.25, layer=pcbnew.F_Cu):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(mm(x1), mm(y1)))
    t.SetEnd(pcbnew.VECTOR2I(mm(x2), mm(y2)))
    t.SetWidth(mm(width))
    t.SetLayer(layer)
    t.SetNetCode(net)
    board.Add(t)
    return t


def via(board, net, x, y, dia=0.6, drill=0.3):
    v = pcbnew.PCB_VIA(board)
    v.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    v.SetViaType(pcbnew.VIATYPE_THROUGH)
    v.SetWidth(mm(dia))
    v.SetDrill(mm(drill))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetNetCode(net)
    board.Add(v)
    return v


def segments(board, net):
    out = set()
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == net:
            a = (round(t.GetStart().x / MM, 4), round(t.GetStart().y / MM, 4))
            b = (round(t.GetEnd().x / MM, 4), round(t.GetEnd().y / MM, 4))
            out.add(tuple(sorted((a, b))))
    return out


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    board, n1, n2 = make_board()
    run = []
    # 1. a route ending inside a via, 0.1 mm off its centre
    via(board, n1, 10, 10)
    run.append(track(board, n1, 0, 10, 10.1, 10))
    # 2. a route ending exactly on the middle of an existing track (T on the centre line)
    existing = track(board, n1, 20, 0, 20, 10)
    run.append(track(board, n1, 15, 5, 20, 5))
    # 3. a route ending on the side of a wide track, 0.15 mm off its centre line
    run.append(track(board, n1, 30, 0, 30, 10, width=0.5))
    run.append(track(board, n1, 25, 5, 29.85, 5))
    # 4. two collinear pieces of one route joined end to end
    run.append(track(board, n1, 40, 0, 40, 5))
    run.append(track(board, n1, 40, 5, 40, 10))
    # 5. another net's copper crossing nothing of ours stays untouched
    track(board, n2, 50, 0, 50, 10)
    before_other = segments(board, n2)

    stats = apply.weld_junctions(board, run)
    print("stats", stats)
    seg = segments(board, n1)

    # the stub and the overhang inside the via fold into one track that ends on the via centre
    check("route now ends on the via centre", ((0.0, 10.0), (10.0, 10.0)) in seg
          and not any(10.1 in (a[0], b[0]) for a, b in seg), seg)
    check("existing track split at the T point",
          ((20.0, 0.0), (20.0, 5.0)) in seg and ((20.0, 5.0), (20.0, 10.0)) in seg, seg)
    check("side T: route extended to the centre line", ((25.0, 5.0), (30.0, 5.0)) in seg, seg)
    check("side T: wide track split at the projection",
          ((30.0, 0.0), (30.0, 5.0)) in seg and ((30.0, 5.0), (30.0, 10.0)) in seg, seg)
    check("collinear pieces merged", ((40.0, 0.0), (40.0, 10.0)) in seg and ((40.0, 0.0), (40.0, 5.0)) not in seg, seg)
    check("other nets untouched", segments(board, n2) == before_other)
    check("counts", stats.get("via_stubs") == 1 and stats.get("centre_stubs") == 1 and stats.get("t_splits", 0) >= 2
          and stats.get("merged", 0) >= 1, stats)

    # every junction is now a shared end point: no end of net N1 lies on another track's open segment
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == n1]
    ends = [(t.GetStart().x, t.GetStart().y) for t in tracks] + [(t.GetEnd().x, t.GetEnd().y) for t in tracks]
    tees = [p for p in ends for t in tracks
            if apply._on_centre_line(t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y, p[0], p[1]) is not None]
    check("no T-junctions left", not tees, tees)

    # idempotent: a second run changes nothing
    again = apply.weld_junctions(board, [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"])
    check("second run is a no-op", not any(again.values()), again)
    print("test_weld_junctions: all passed")


if __name__ == "__main__":
    main()
