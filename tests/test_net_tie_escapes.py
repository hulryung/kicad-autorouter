"""Declared net-tie bridge graphics must not wall their own pad exits."""

import sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
try:
    import pcbnew
except ImportError:
    print("test_net_tie_escapes: skipped (needs KiCad Python)")
    raise SystemExit(0)
from test_shove_scope import outline, add_pad, add_track
from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions, SCOPE_SELECTION
from autorouter.engine.ai.context import drc_summary


def fixture(declared=True):
    b = pcbnew.BOARD()
    outline(b, 30, 20)
    a = pcbnew.NETINFO_ITEM(b, "FORCE")
    b.Add(a)
    n = pcbnew.NETINFO_ITEM(b, "SENSE")
    b.Add(n)
    f = pcbnew.FOOTPRINT(b)
    f.SetReference("NT1")
    b.Add(f)
    add_pad(f, 1, 10, 10, a, w=0.5, h=0.5)
    add_pad(f, 2, 11, 10, n, w=0.5, h=0.5)
    if declared:
        f.AddNetTiePadGroup("1,2")
    bridge = pcbnew.PCB_SHAPE(f)
    bridge.SetShape(pcbnew.SHAPE_T_SEGMENT)
    bridge.SetStart(pcbnew.VECTOR2I(10000000, 10000000))
    bridge.SetEnd(pcbnew.VECTOR2I(11000000, 10000000))
    bridge.SetWidth(500000)
    bridge.SetLayer(pcbnew.F_Cu)
    f.Add(bridge)
    target = pcbnew.FOOTPRINT(b)
    target.SetReference("J1")
    b.Add(target)
    add_pad(target, 1, 20, 10, n)
    b.BuildConnectivity()
    return b


opts = NativeOptions(
    pitch_mm=0.1,
    scope=SCOPE_SELECTION,
    scope_nets=["SENSE"],
    allow_vias=False,
    routable_layers=[True, False],
    time_limit_s=10,
    fanout_vias=False,
    stitch_vias=False,
    diff_pairs=False,
    tune_lengths=False,
)
with tempfile.TemporaryDirectory() as folder:
    b = fixture()
    before = drc_summary(b, Path(folder) / "before")
    result = pipeline.run_job(b, opts, escalate=False)
    assert result.routed == 1, result.failed_detail
    after = drc_summary(b, Path(folder) / "after")
    assert (
        after["available"] and after["unconnected_by_net"].get("SENSE", 0) == 0
    ), after
    assert after["violations_by_type"] == before["violations_by_type"], (before, after)
    ordinary = pipeline.run_job(fixture(False), opts, escalate=False)
    assert (
        ordinary.routed == 0
    ), "ordinary copper graphic was treated as a declared net tie"
# Preserve a foreign wall that already blocked the pad before bridge handling.
wallboard = fixture()
foreign = pcbnew.NETINFO_ITEM(wallboard, "FOREIGN")
wallboard.Add(foreign)
add_track(wallboard, foreign, pcbnew.F_Cu, (11.6, 0), (11.6, 20), w=0.2)
wallboard.BuildConnectivity()
assert pipeline.run_job(wallboard, opts, escalate=False).routed == 0
# The middle of a long bridge stays an obstacle to an unrelated net.
cross = fixture()
fp = next(f for f in cross.GetFootprints() if f.GetReference() == "NT1")
next(p for p in fp.Pads() if p.GetNumber() == "2").SetPosition(
    pcbnew.VECTOR2I(13000000, 10000000)
)
next(g for g in fp.GraphicalItems() if g.IsOnLayer(pcbnew.F_Cu)).SetEnd(
    pcbnew.VECTOR2I(13000000, 10000000)
)
third = pcbnew.NETINFO_ITEM(cross, "THIRD")
cross.Add(third)
ends = pcbnew.FOOTPRINT(cross)
ends.SetReference("J2")
cross.Add(ends)
add_pad(ends, 1, 11.5, 6, third)
add_pad(ends, 2, 11.5, 14, third)
cross.BuildConnectivity()
opts.scope_nets = ["THIRD"]
with tempfile.TemporaryDirectory() as folder:
    before = drc_summary(cross, Path(folder) / "before")
    result = pipeline.run_job(cross, opts, escalate=False)
    assert result.routed == 1, result.failed_detail
    after = drc_summary(cross, Path(folder) / "after")
    assert after["violations_by_type"] == before["violations_by_type"], after
    assert after["unconnected_by_net"].get("THIRD", 0) == 0
print("test_net_tie_escapes: declared pad exit connected without new DRC errors")
