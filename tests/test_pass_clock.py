"""A pass's time limit counts its board read (a whole-board read at 0.1 mm
takes minutes: a "15-minute" PIO pass ran 18), and a script without a
progress bar gets a status line every minute of the search (#21).

  <KiCad python3> tests/test_pass_clock.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_pass_clock: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import astar, pipeline
from autorouter.engine.native.problem import NativeOptions

HERE = Path(__file__).resolve().parent


def test_deadline_set_before_read():
    board = pcbnew.LoadBoard(str(HERE / "fixtures" / "obd2-adapter.kicad_pcb"))
    seen = []

    def extract(b, o, st):
        seen.append((o.time_limit_s, o.pass_deadline, astar.clock()))
        return None                                  # no problem: the job stops here

    o = NativeOptions()
    o.time_limit_s = 300.0
    o.fanout_first = False
    pipeline.run_job(board, o, escalate=False, apply_paths=False, extract_fn=extract)
    limit, deadline, at_read = seen[0]
    assert deadline and abs(deadline - (at_read + limit)) < 5.0, seen
    assert o.pass_deadline == 0.0                     # the caller's options untouched
    print("pass deadline set before the board read: ok")


def test_progress_lines():
    lines, ticks = [], []
    tell = pipeline._progress_lines(lambda *a: ticks.append(a), lines.append, every=0.0)
    tell(3, 10, 2, 1, "SIG")
    assert ticks == [(3, 10, 2, 1, "SIG")]
    assert lines and "3/10" in lines[0] and "1 open" in lines[0], lines
    assert pipeline._progress_lines(None, None) is None
    quiet = []
    tell = pipeline._progress_lines(None, quiet.append, every=3600.0)
    tell(1, 2, 1, 0, "A")
    assert not quiet                                  # under a minute: no line
    print("progress status lines: ok")


if __name__ == "__main__":
    test_progress_lines()
    test_deadline_set_before_read()
    print("test_pass_clock: ok")
