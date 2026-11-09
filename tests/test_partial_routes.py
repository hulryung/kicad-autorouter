"""Routing a board whose tracks are partly there (some segments of each net
deleted, the way a designer leaves a route half done): the new copper starts
and ends on the old, instead of running pad to pad again on top of it and
leaving the old pieces open. Routes the OBD-II fixture, deletes every third
segment of each signal net, routes again. Needs KiCad's Python; skips
otherwise.

  <KiCad python3> tests/test_partial_routes.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_partial_routes: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine import session
from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

HERE = Path(__file__).resolve().parent


def inside_length(a, b) -> float:
    """Length of a's centreline inside b's copper (nm, sampled every 20 um)."""
    s, e = a.GetStart(), a.GetEnd()
    length = math.hypot(e.x - s.x, e.y - s.y)
    if length == 0:
        return 0.0
    bs, be = b.GetStart(), b.GetEnd()
    r = b.GetWidth() / 2
    n = max(1, int(length / 20_000))
    inside = 0
    for k in range(n + 1):
        px, py = s.x + (e.x - s.x) * k / n, s.y + (e.y - s.y) * k / n
        dx, dy = be.x - bs.x, be.y - bs.y
        l2 = dx * dx + dy * dy
        u = max(0.0, min(1.0, ((px - bs.x) * dx + (py - bs.y) * dy) / l2)) if l2 else 0.0
        if math.hypot(bs.x + u * dx - px, bs.y + u * dy - py) < r - 1000:
            inside += 1
    return length * inside / (n + 1)


def check(name, cond, detail=""):
    if not cond:
        print(f"FAIL {name} {detail}")
        sys.exit(1)
    print(f"ok   {name}")


def main():
    board = pcbnew.LoadBoard(str(HERE / "fixtures" / "obd2-adapter.kicad_pcb"))
    options = NativeOptions(pitch_mm=0.1)
    job = pipeline.run_job(board, options, escalate=True, apply_paths=True)
    board.BuildConnectivity()
    check("first run routes the fixture", board.GetConnectivity().GetUnconnectedCount(True) == 0, str(job.failed))

    by_net = {}
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK" and t.GetNetCode() > 0:
            by_net.setdefault(t.GetNetCode(), []).append(t)
    removed = 0
    for tracks in by_net.values():
        tracks.sort(key=lambda t: (t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y))
        for t in tracks[1::3]:
            board.Remove(t)
            t.thisown = 0
            session._REMOVED.append(t)
            removed += 1
    board.BuildConnectivity()
    open_before = board.GetConnectivity().GetUnconnectedCount(True)
    check("deleting segments opens connections", removed > 0 and open_before > 0, f"{removed} removed")
    old = {str(t.m_Uuid.AsString()) for t in board.GetTracks()}

    pipeline.run_job(board, NativeOptions(pitch_mm=0.1), escalate=True, apply_paths=True)
    board.BuildConnectivity()
    open_after = board.GetConnectivity().GetUnconnectedCount(True)
    check("the second run finishes every route", open_after == 0, f"{open_before} -> {open_after}")

    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    kept = {}
    for t in tracks:
        if str(t.m_Uuid.AsString()) in old:
            kept.setdefault((t.GetNetCode(), t.GetLayer()), []).append(t)
    over = 0.0
    for t in tracks:
        if str(t.m_Uuid.AsString()) in old:
            continue
        for b in kept.get((t.GetNetCode(), t.GetLayer()), []):
            if b.GetBoundingBox().Intersects(t.GetBoundingBox()):
                d = inside_length(t, b)
                if d > 300_000:                       # more than a join's overlap
                    over += d
    check("no new copper drawn over the old", over < 1_000_000, f"{over / 1e6:.2f} mm")
    print("test_partial_routes: all passed")


if __name__ == "__main__":
    main()
