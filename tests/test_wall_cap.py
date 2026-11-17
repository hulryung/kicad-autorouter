"""The job's wall-clock cap: a benchmark's time limit counts search work only,
so board work (isolation rings, board reads) once took a "10-minute" job four
hours. The cap stops the routing as a timeout (not a cancel) and keeps what
was routed.

  <KiCad python3> tests/test_wall_cap.py
"""
import functools
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
print = functools.partial(print, flush=True)

try:
    import pcbnew
except ImportError:
    print("test_wall_cap: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions

HERE = Path(__file__).resolve().parent


def test_automatic_cap():
    o = NativeOptions()
    o.time_limit_s = 600.0
    assert pipeline.wall_cap(o) == 600.0 + 20 * 60.0          # limit + 20 min beats 3x for short limits
    o.time_limit_s = 3600.0
    assert pipeline.wall_cap(o) == 3 * 3600.0
    o.time_limit_s = 0
    assert pipeline.wall_cap(o) == 0                          # no time limit: no cap
    o.wall_limit_s = 90.0
    assert pipeline.wall_cap(o) == 90.0                       # an explicit cap
    o.wall_limit_s = 0
    o.time_limit_s = 600.0
    assert pipeline.wall_cap(o) == 0                          # 0 = none
    print("automatic cap: ok")


def test_cap_stops_routing():
    board = pcbnew.LoadBoard(str(HERE / "fixtures" / "obd2-adapter.kicad_pcb"))
    o = NativeOptions()
    o.pitch_mm = 0.1
    o.time_limit_s = 600.0
    o.wall_limit_s = 0.001                                    # over before the first search
    t0 = time.time()
    job = pipeline.run_job(board, o, escalate=True, apply_paths=False)
    took = time.time() - t0
    assert job.wall_capped and job.timed_out, (job.wall_capped, job.timed_out)
    assert not job.cancelled                                  # a timeout, not the user's cancel
    assert job.passes <= 1                                    # no finer second pass after the cap
    assert job.failed, "nothing can have been routed after the cap"
    print(f"cap stops routing: ok ({took:.0f}s, {len(job.failed)} nets left open)")


if __name__ == "__main__":
    test_automatic_cap()
    test_cap_stops_routing()
    print("test_wall_cap: ok")
