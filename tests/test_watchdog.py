"""The job watchdog: steps timed, a stalled step reported with where it is,
a hard stop past the wall cap and its grace.

  <KiCad python3> tests/test_watchdog.py   (the first two run on any Python)
"""
import functools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
print = functools.partial(print, flush=True)

from autorouter.engine.native import watchdog


def busy_wait(seconds):
    end = time.time() + seconds
    while time.time() < end:
        pass


def test_stall_and_slow_steps():
    told = []
    w = watchdog.start(told.append, stall_s=0.2)
    w.slow_s = 0.3
    try:
        watchdog.stage("quick step")
        watchdog.stage("stuck step")
        busy_wait(0.9)                     # 0.2 s -> report, 0.4 s -> again, 0.8 s -> again
        watchdog.stage("after")
    finally:
        watchdog.stop(w)
    assert watchdog.current() is None
    assert [n for n, _, _ in w.stalls] and all(n == "stuck step" for n, _, _ in w.stalls), w.stalls
    assert 2 <= len(w.stalls) <= 3, w.stalls                       # once, then at each doubling
    assert "busy_wait" in w.stalls[0][2], w.stalls[0][2]           # where it is stuck
    assert any(t.startswith("Slow step: 'stuck step'") for t in told), told
    assert [n for n, _ in w.slow()] == ["stuck step"], w.slow()
    print("stall report and slow steps: ok")


def test_hard_deadline():
    w = watchdog.start(None, hard_deadline=time.time() + 0.1)
    try:
        watchdog.stage("board read: tracks")
        watchdog.check()                   # before the deadline: nothing
        assert not watchdog.overdue()
        time.sleep(0.15)
        try:
            watchdog.check()
            raise AssertionError("no Overrun past the hard deadline")
        except watchdog.Overrun:
            pass
        assert w.overrun == "board read: tracks"
    finally:
        watchdog.stop(w)
    watchdog.check()                       # no watch: a no-op
    assert not watchdog.overdue()
    print("hard deadline: ok")


def test_pipeline_hard_stop():
    try:
        import pcbnew
    except ImportError:
        print("pipeline hard stop: skipped (needs KiCad's Python)")
        return
    from autorouter.engine.native import pipeline
    from autorouter.engine.native.problem import NativeOptions
    board = pcbnew.LoadBoard(str(Path(__file__).resolve().parent / "fixtures" / "obd2-adapter.kicad_pcb"))
    o = NativeOptions()
    o.pitch_mm = 0.1
    o.time_limit_s = 600.0
    o.wall_limit_s = 0.001
    saved = pipeline.POST_CAP_MIN_S, pipeline.POST_CAP_SHARE
    pipeline.POST_CAP_MIN_S, pipeline.POST_CAP_SHARE = 0.0, 0.0      # no grace: hard stop = the cap
    try:
        job = pipeline.run_job(board, o, escalate=True, apply_paths=False)
    finally:
        pipeline.POST_CAP_MIN_S, pipeline.POST_CAP_SHARE = saved
    assert job.wall_capped and job.timed_out and not job.cancelled
    assert job.stats.get("overrun_passes"), job.stats.get("overrun_passes")
    assert "slow_stages" in job.stats and "stalls" in job.stats
    assert watchdog.current() is None
    print(f"pipeline hard stop: ok (cut off: {job.stats['overrun_passes']})")


if __name__ == "__main__":
    test_stall_and_slow_steps()
    test_hard_deadline()
    test_pipeline_hard_stop()
    print("test_watchdog: ok")
