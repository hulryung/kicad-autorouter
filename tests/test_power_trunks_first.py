"""An opt-in distributed power rail gets its tree before fine-pitch signals."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_power_trunks_first: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline, add_pad
from autorouter.engine.native import extract, router, apply
from autorouter.engine.native.problem import NativeOptions


def run(first):
    b = pcbnew.BOARD(); outline(b, 50, 30)
    rail = pcbnew.NETINFO_ITEM(b, '+12V'); b.Add(rail)
    sig = pcbnew.NETINFO_ITEM(b, 'DATA'); b.Add(sig)
    fp = pcbnew.FOOTPRINT(b); fp.SetReference('J1'); b.Add(fp)
    for i, x in enumerate((5, 15, 25, 35, 45)):
        add_pad(fp, i + 1, x, 10, rail)
    add_pad(fp, 6, 20, 20, sig); add_pad(fp, 7, 22, 20, sig)
    b.BuildConnectivity()
    p = extract.extract(b, NativeOptions(power_trunks_first=first, fanout_vias=False,
                         stitch_vias=False, diff_pairs=False, pitch_mm=0.2))
    for c in p.connections:
        if c.net == sig.GetNetCode(): c.priority = 0
    emitted = []
    result = router.run(p, on_path=lambda path: emitted.append(path.net))
    assert not result.failed, result.failed_detail
    for path in result.paths: apply.add_path(b, p.layers, path)
    b.BuildConnectivity()
    assert b.GetConnectivity().GetUnconnectedCount(True) == 0
    assert emitted[0] == (rail.GetNetCode() if first else sig.GetNetCode()), emitted
    if first:
        assert emitted.index(sig.GetNetCode()) >= 4, emitted

run(False)
run(True)
print('test_power_trunks_first: route order and complete rail connectivity passed')
