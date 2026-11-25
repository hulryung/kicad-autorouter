"""Custom-rule status exposes smaller inherited and explicit local clearances."""
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_local_clearance_warning: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad
from autorouter.engine.native import dru,extract
from autorouter.engine.native.problem import NativeOptions

b=pcbnew.BOARD();outline(b,40,30)
hv=pcbnew.NETINFO_ITEM(b,'HV');b.Add(hv)
logic=pcbnew.NETINFO_ITEM(b,'LOGIC');b.Add(logic)
f=pcbnew.FOOTPRINT(b);f.SetReference('U12');b.Add(f)
for k in range(4):add_pad(f,k+1,5+k*3,10,hv)
f.SetLocalClearance(200000)
r=pcbnew.FOOTPRINT(b);r.SetReference('R1');b.Add(r);add_pad(r,1,30,10,logic)
text='(version 1) (rule "HV outer" (layer outer) (condition "A.NetName == \'HV\' && B.NetName != \'HV\'") (constraint clearance (min 0.6mm)))'
rules=dru.ClearanceRules.parse(text,{'Default':200000})
items={n.GetNetCode():dru.Item('Default',n.GetNetname()) for n in (hv,logic)}
def warnings(rule=rules):return extract.local_clearance_warnings(b,rule,items,list(items.values()))
lines=warnings();assert len(lines)==1,lines
assert 'U12: footprint local clearance 0.2 mm' in lines[0] and "'HV outer' 0.6 mm on 4 pad(s)" in lines[0],lines
pad=next(iter(f.Pads()));pad.SetLocalClearance(700000)
assert 'on 3 pad(s)' in warnings()[0]
pad.SetLocalClearance(0)
assert any('pad local clearance 0 mm' in x for x in warnings())
inner=dru.ClearanceRules.parse(text.replace('outer','inner'),{'Default':200000})
assert not warnings(inner),'inner-only rule does not apply to front SMD pads'
relaxed=dru.ClearanceRules.parse(text+' (rule "exception" (condition "A.Reference == \'U12\'") (constraint clearance (min 0.1mm)))',{'Default':200000})
pad.SetLocalClearance(None)
assert not warnings(relaxed),'later matching exception wins'
messages=[]
with patch.object(dru,'for_board',return_value=rules):
    extract.extract(b,NativeOptions(fanout_vias=False,stitch_vias=False),status=messages.append)
assert any(x.startswith('Custom rules warning: U12:') for x in messages),messages
assert f.GetLocalClearance()==200000 and pad.GetLocalClearance() is None
f.SetLocalClearance(None)
assert not warnings(),'unset overrides must not be reported as zero'
print('test_local_clearance_warning: passed')
