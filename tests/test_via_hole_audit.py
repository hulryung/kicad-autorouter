"""Detect new same-net hole overlaps while exempting restored original pairs."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
try:
    import pcbnew
except ImportError:
    print("test_via_hole_audit: skipped (needs KiCad Python)")
    raise SystemExit(0)
from autorouter.engine.native.pipeline import _via_inventory, _new_via_hole_violations

b = pcbnew.BOARD()
b.GetDesignSettings().m_HoleToHoleMin = 250000
net = pcbnew.NETINFO_ITEM(b, "SAME")
b.Add(net)


def via(x):
    v = pcbnew.PCB_VIA(b)
    v.SetPosition(pcbnew.VECTOR2I(x, 1000000))
    v.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    v.SetWidth(800000)
    v.SetDrill(400000)
    v.SetNet(net)
    b.Add(v)
    return v


first = via(1000000)
second = via(1200000)
baseline = _via_inventory(b)
assert not _new_via_hole_violations(b, baseline)
b.Remove(second)
replacement = via(1200000)
assert not _new_via_hole_violations(b, baseline)
extra = via(1200000)
assert len(_new_via_hole_violations(b, baseline)) == 2
b.Remove(extra)
replacement.SetPosition(pcbnew.VECTOR2I(1300000, 1000000))
assert len(_new_via_hole_violations(b, baseline)) == 1
replacement.SetPosition(pcbnew.VECTOR2I(1650000, 1000000))
assert not _new_via_hole_violations(b, baseline)
print("test_via_hole_audit: new pairs audited, restored old geometry exempt")
