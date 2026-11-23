"""Locked tracks are never split, trimmed or moved by a job (issue #12).

Every existing track is locked, shove is off and the job routes what is
left. A new route that joins a locked track mid-way (a T-junction) used to
split it in two at the weld, and one joining near an end moved the end a
few micrometres: new UUIDs, and the lock lost on the pieces. Now the new
copper ends on the locked track (KiCad connects a track end lying on
another track) and the locked track keeps its UUID, end points and lock.
Also when the track is unlocked but named in NativeOptions.locked_uuids.

Builds the boards in memory. Needs KiCad's Python; skips otherwise.

  <KiCad python3> tests/test_locked_join.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_locked_join: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

MM = 1_000_000


def pt(x, y):
    return pcbnew.VECTOR2I(int(round(x * MM)), int(round(y * MM)))


def outline(board, w, h):
    for (x1, y1), (x2, y2) in (((0, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0))):
        s = pcbnew.PCB_SHAPE(board)
        s.SetShape(pcbnew.SHAPE_T_SEGMENT)
        s.SetStart(pt(x1, y1))
        s.SetEnd(pt(x2, y2))
        s.SetLayer(pcbnew.Edge_Cuts)
        s.SetWidth(int(0.1 * MM))
        board.Add(s)


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


def board_with(a, b, far_pad, pad, lock=True):
    """30 x 20 mm, 2 layers: SIG's 0.5 mm track a -> b on F.Cu from pad R1.1
    (pad R1.2 at b when far_pad), locked when `lock`, and pad R2.1 to join."""
    board = pcbnew.BOARD()
    ds = board.GetDesignSettings()
    ds.m_NetSettings.GetDefaultNetclass().SetTrackWidth(int(0.25 * MM))
    ds.m_NetSettings.GetDefaultNetclass().SetClearance(int(0.15 * MM))
    net = pcbnew.NETINFO_ITEM(board, "SIG")
    board.Add(net)
    outline(board, 30, 20)
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("R1")
    board.Add(fp)
    add_pad(fp, 1, *a, net)
    if far_pad:
        add_pad(fp, 2, *b, net)
    fp2 = pcbnew.FOOTPRINT(board)
    fp2.SetReference("R2")
    board.Add(fp2)
    add_pad(fp2, 1, *pad, net)
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pt(*a))
    t.SetEnd(pt(*b))
    t.SetWidth(int(0.5 * MM))
    t.SetLayer(pcbnew.F_Cu)
    t.SetNet(net)
    t.SetLocked(lock)
    board.Add(t)
    board.BuildConnectivity()
    return board, t


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def shape(t):
    return (t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y, t.GetWidth(), t.GetLayer())


def main():
    cases = (
        # label, track a, b, pad at b, pad to join, locked (else by UUID)
        ("mid-track join", (5, 10), (25, 10), True, (15, 3), True),
        ("join near a free end", (5, 10), (20, 10), False, (19.9, 4), True),
        ("join near a pad end", (5, 10), (25, 10), True, (24.4, 4), True),
        ("mid-track join, locked by UUID", (5, 10), (25, 10), True, (15, 3), False),
    )
    for label, a, b, far_pad, pad, lock in cases:
        board, t = board_with(a, b, far_pad, pad, lock)
        uid = str(t.m_Uuid.AsString())
        before = shape(t)
        opts = NativeOptions(pitch_mm=0.1, shove=False)
        if not lock:
            opts.locked_uuids = [uid]
        job = pipeline.run_job(board, opts)
        tracks = [x for x in board.GetTracks() if x.GetClass() == "PCB_TRACK"]
        mine = [x for x in tracks if str(x.m_Uuid.AsString()) == uid]
        everything = [shape(x) for x in tracks]
        check(f"{label}: routed", not job.failed, str(job.failed))
        check(f"{label}: the locked track is still there", len(mine) == 1, str(everything))
        check(f"{label}: its end points, width and layer are unchanged", shape(mine[0]) == before,
              f"{before} -> {shape(mine[0])}")
        check(f"{label}: its lock is kept", bool(mine[0].IsLocked()) == lock)
        # no piece of it (a split or a copy) along its line
        pieces = [x for x in tracks if str(x.m_Uuid.AsString()) != uid and x.GetWidth() == before[4]
                  and x.GetStart().y == before[1] and x.GetEnd().y == before[1]]
        check(f"{label}: no piece of it was added", not pieces, str([shape(x) for x in pieces]))
        board.BuildConnectivity()
        check(f"{label}: the board is fully connected",
              board.GetConnectivity().GetUnconnectedCount(True) == 0,
              f"{everything} {job.stats.get('weld_detail')}")
    print("test_locked_join: all passed")


if __name__ == "__main__":
    main()
