"""Temporary keepouts select layers/net groups without mutating the board."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
from autorouter.engine.native.constraints import ViaBlocks
base=bytearray(3);mask=bytearray((0,1,0));view=ViaBlocks(base,[mask])
assert not view[0] and view[1]
base[0]=1;assert view[0]
base[0]=0;assert not view[0]
try:
    import pcbnew
except ImportError:
    print('test_routing_keepouts: live union passed; native part skipped')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad
from autorouter.engine.native import extract,router,apply
from autorouter.engine.native.problem import NativeOptions
from autorouter.engine.native.grid import BLOCKED

b=pcbnew.BOARD();outline(b,40,20)
nets={}
f=pcbnew.FOOTPRINT(b);f.SetReference('J1');b.Add(f)
for i,name in enumerate(('N1','N2','WALL'),1):
    n=pcbnew.NETINFO_ITEM(b,name);b.Add(n);nets[name]=n
for i,y in enumerate((5,15)):
    add_pad(f,2*i+1,5,y,nets['N'+str(i+1)])
    add_pad(f,2*i+2,35,y,nets['N'+str(i+1)])
t=pcbnew.PCB_TRACK(b);t.SetNet(nets['WALL']);t.SetLayer(pcbnew.F_Cu);t.SetWidth(400000)
t.SetStart(pcbnew.VECTOR2I(20000000,0));t.SetEnd(pcbnew.VECTOR2I(20000000,20000000));b.Add(t)
b.BuildConnectivity();zones=len(list(b.Zones()));before=len(list(b.GetTracks()))
rect=[0,0,22000000,20000000]
o=NativeOptions(fanout_vias=False,stitch_vias=False,diff_pairs=False,pitch_mm=.2,
                via_keepouts=[{'rect_nm':rect,'layers':['B.Cu'],'nets':['N1']}])
p=extract.extract(b,o)
a,c=nets['N1'].GetNetCode(),nets['N2'].GetNetCode()
assert a in p.net_via_blocks and c not in p.net_via_blocks
r=router.run(p)
assert {v.net for v in r.failed}=={a},r.failed_detail
assert any(path.net==c and path.vias for path in r.paths)
assert not any(path.net==a and path.vias for path in r.paths)
assert len(list(b.GetTracks()))==before and len(list(b.Zones()))==zones
# The same selection can address routing groups instead of net names.
o.via_keepouts=[{'rect_nm':rect,'groups':['signal']}]
p=extract.extract(b,o);assert a in p.net_via_blocks and c in p.net_via_blocks
# Copper exclusions on B.Cu leave the corresponding F.Cu cells free.
o.via_keepouts=[];o.layer_keepouts=[{'rect_nm':[8000000,8000000,12000000,12000000],'layers':['B.Cu']}]
p=extract.extract(b,o);g=p.grid;own=p.owners[p.nets[a].rules.key]
li={layer.name:layer.index for layer in p.layers};x,y=g.cell_x(10000000),g.cell_y(10000000)
assert own[g.index(li['B.Cu'],x,y)]==BLOCKED
assert own[g.index(li['F.Cu'],x,y)]!=BLOCKED
# A typo must fail, not silently disable a fabrication constraint.
o.layer_keepouts=[{'rect_nm':rect,'layers':['TYPO']}]
try:extract.extract(b,o)
except ValueError:pass
else:raise AssertionError('unknown copper layer was silently ignored')
print('test_routing_keepouts: net/group via restrictions, layer isolation and validation passed')

# Direct fanout and stitching must obey the same rule as ordinary A* vias.
from test_fanout_stub_clearance import make_board
b,nets=make_board()
o=NativeOptions(pours_only=True,stitch_vias=False,via_keepouts=[
    {'rect_nm':[0,0,30000000,20000000],'layers':['B.Cu'],'nets':['+3V3']}])
p=extract.extract(b,o);engine=router.Router(p);code=nets['+3V3'].GetNetCode()
assert engine._stitch_via(code,p.grid.cell_x(20000000),p.grid.cell_y(10000000)) is None
assert not engine.run().paths, 'plane fanout bypassed the net-specific via restriction'
print('test_routing_keepouts: fanout and stitching respect net-specific restrictions')
