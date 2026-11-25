"""The second pass's via-in-small-pad last resort can be turned off, for
boards built without via filling (BATTEM2 PIO: 26 vias landed in 0603 pads).

  python3 tests/test_via_in_pad_last_resort.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew  # noqa: F401  (pipeline imports it)
except ImportError:
    print("test_via_in_pad_last_resort: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions


def test_last_resort_switch():
    board = pcbnew.LoadBoard(str(Path(__file__).resolve().parent / "fixtures" / "obd2-adapter.kicad_pcb"))
    o = NativeOptions()
    assert not o.via_in_small_pads and not o.via_in_pad_last_resort
    assert not pipeline.fine_options(o, ["A"], board).via_in_small_pads
    o.via_in_pad_last_resort = True
    assert pipeline.fine_options(o, ["A"], board).via_in_small_pads
    assert not o.via_in_small_pads  # retry opt-in must not relax the first pass
    o.via_in_pad_last_resort = False
    assert not pipeline.fine_options(o, ["A"], board).via_in_small_pads
    o.via_in_small_pads = True
    assert pipeline.fine_options(o, ["A"], board).via_in_small_pads
    print("via-in-pad last resort switch: ok")


if __name__ == "__main__":
    test_last_resort_switch()
    print("test_via_in_pad_last_resort: ok")
