"""The progress parser against Freerouting 2.5 log lines."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from autorouter.engine.progress import (ProgressParser, STAGE_DONE, STAGE_FANOUT, STAGE_LOAD,
                                        STAGE_OPTIMIZE, STAGE_ROUTE, STAGE_SAVE)

LOG = """\
2026-10-11 21:02:16.472 INFO   Freerouting v2.5.0 (build-date: 2026-10-05)
2026-10-11 21:02:18.143 INFO   [599D50\\99E12A] Loading board file 'test.dsn'...
2026-10-11 21:02:18.210 INFO   [599D50\\99E12A] Fanout stage started on board '4829' with 2 of 2 SMD pins needing fanout (0 already connected, 0 netless).
2026-10-11 21:02:18.284 INFO   [599D50\\99E12A] Fanout pass #1 on board '0e6f' completed in 0.07 seconds with 2 SMD pins fanouted, 0 not routed, 0 insert errors, +2 extra vias.
2026-10-11 21:02:18.289 INFO   [599D50\\99E12A] Auto-routing stage started on board '0e6f' with baseline score 0.00 for 5 unrouted items.
2026-10-11 21:02:18.344 INFO   [599D50\\99E12A] Auto-routing pass #1 on board '0e6f' was completed in 0.05 seconds with score 600.00 (2 unrouted and 1 violations).
2026-10-11 21:02:18.346 INFO   [599D50\\99E12A] Auto-routing pass #2 on board 'a0e7' was completed in 0.00 seconds with score 1000.00 (0 unrouted and 0 violations).
2026-10-11 21:02:18.349 INFO   [599D50\\99E12A] Auto-routing stage completed: started with 5 unrouted nets, completed in 0.16 seconds, final score: 1000.00 (0 unrouted and 0 violations), using 0.00 total CPU seconds.
2026-10-11 21:02:18.361 INFO   [599D50\\99E12A] Optimization stage started on board 'a0e7'. Baseline router score: 1000.00, optimizer score: 897.06, incomplete connections: 0, clearance violations: 0.
2026-10-11 21:02:18.400 INFO   [599D50\\99E12A] Optimizer pass #1 on board 'a3b2' was completed in 0.04 seconds with the score of 926.47 (0 unrouted and 0 violations).
2026-10-11 21:02:18.402 INFO   [599D50\\99E12A] Optimizer pass #1: optimizer score 897.06 -> 926.47 (IMPROVED, 3.2787%), router score: 1000.00, incomplete connections: 0, clearance violations: 0.
2026-10-11 21:02:18.443 INFO   [599D50\\99E12A] Job '599D50\\99E12A' finished with state: COMPLETED (elapsed: 0.28 seconds, finished at UTC: 2026-10-11T12:02:18.442746Z).
2026-10-11 21:02:18.483 INFO   Saving output file '/tmp/e2e/test.ses' (SES)...
2026-10-11 21:02:18.486 INFO   Successfully saved output file '/tmp/e2e/test.ses' (2309 bytes).
"""


def main():
    p = ProgressParser()
    lines = LOG.splitlines()
    assert p.feed(lines[1]).stage == STAGE_LOAD
    assert p.feed(lines[2]).stage == STAGE_FANOUT
    s = p.feed(lines[3])
    assert s.stage == STAGE_FANOUT and s.pass_number == 1
    s = p.feed(lines[4])
    assert s.stage == STAGE_ROUTE and s.total_items == 5 and s.unrouted == 5
    assert s.fraction(10) == 0.0
    s = p.feed(lines[5])
    assert (s.pass_number, s.unrouted, s.violations, s.score) == (1, 2, 1, 600.0)
    assert abs(s.fraction(10) - 0.6) < 1e-9, s.fraction(10)
    s = p.feed(lines[6])
    assert (s.unrouted, s.violations) == (0, 0)
    assert s.fraction(10) == 0.99                      # never 100% before the file is saved
    s = p.feed(lines[7])
    assert s.score == 1000.0
    assert p.feed(lines[8]).stage == STAGE_OPTIMIZE
    s = p.feed(lines[9])
    assert s.pass_number == 1 and s.fraction(10) is None
    s = p.feed(lines[10])
    assert (s.unrouted, s.violations) == (0, 0)
    s = p.feed(lines[11])
    assert s.job_state == "COMPLETED" and not s.finished_ok
    assert p.feed(lines[12]).stage == STAGE_SAVE
    s = p.feed(lines[13])
    assert s.stage == STAGE_DONE and s.output_saved == "/tmp/e2e/test.ses" and s.finished_ok
    assert s.fraction(10) == 1.0
    assert "0 unrouted" in s.summary()
    assert s.errors == []

    # Errors and uncaught exceptions are collected; a cancelled job isn't ok.
    p = ProgressParser()
    p.feed("2026-10-11 21:02:18.443 ERROR  [1] Board file could not be read")
    p.feed('Exception in thread "Thread-1" java.lang.RuntimeException: boom')
    p.feed("\tat app.freerouting.Foo.bar(Foo.java:1)")
    s = p.feed("2026-10-11 21:02:18.443 INFO   [1] Job '1' finished with state: CANCELLED (elapsed: 1 s).")
    assert len(s.errors) == 2, s.errors
    assert s.job_state == "CANCELLED" and not s.finished_ok
    assert "CANCELLED" in s.summary()
    print("test_progress: ok")


if __name__ == "__main__":
    main()
