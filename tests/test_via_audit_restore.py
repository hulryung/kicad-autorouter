"""Via audit distinguishes rollback restoration from new/moved/duplicate copper."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_via_audit_restore: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import add_pad,add_via,pt
from autorouter.engine.native import pipeline

b=pcbnew.BOARD();n=pcbnew.NETINFO_ITEM(b,'SIGNAL');b.Add(n)
f=pcbnew.FOOTPRINT(b);f.SetReference('J1');b.Add(f);add_pad(f,1,10,10,n)
add_via(b,n,10,10)
original=next(iter(b.GetTracks()));before=pipeline._via_inventory(b)
b.Remove(original);add_via(b,n,10,10)
assert not pipeline._vias_in_small_pads(b,before,1500000),'identical restoration is not new copper'
add_via(b,n,10,10)
assert pipeline._vias_in_small_pads(b,before,1500000)==['J1.1'],'duplicate must not inherit exemption'
for t in list(b.GetTracks()):b.Remove(t)
add_via(b,n,11,10);before=pipeline._via_inventory(b)
v=next(iter(b.GetTracks()));v.SetPosition(pt(10,10))
assert pipeline._vias_in_small_pads(b,before,1500000)==['J1.1'],'same UUID moved into pad must be audited'
print('test_via_audit_restore: passed')
