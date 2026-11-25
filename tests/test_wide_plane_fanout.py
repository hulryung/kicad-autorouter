"""A plane pad above a foreign island reaches its own pour beyond the local ring."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_wide_plane_fanout: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad,add_via
from test_plane_foreign_pour import zone
from autorouter.engine.compat import zone_filler
from autorouter.engine.native import extract,router,apply
from autorouter.engine.native.problem import NativeOptions

def make_board(half_width=4):
    b=pcbnew.BOARD();b.SetCopperLayerCount(4);outline(b,40,30)
    a=pcbnew.NETINFO_ITEM(b,'+3V3');b.Add(a)
    c=pcbnew.NETINFO_ITEM(b,'+12V');b.Add(c)
    f=pcbnew.FOOTPRINT(b);f.SetReference('J1');b.Add(f)
    add_pad(f,1,5,15,a);add_pad(f,2,20,15,a);add_pad(f,3,20,10,c)
    add_via(b,a,5,15);add_via(b,c,20,10)
    zone(b,a,pcbnew.In2_Cu,((1,1),(39,1),(39,29),(1,29)),0)
    zone(b,c,pcbnew.In2_Cu,((20-half_width,8),(20+half_width,8),(20+half_width,22),(20-half_width,22)),1)
    zone_filler(b).Fill(b.Zones());b.BuildConnectivity()
    return b,a,c

b,a,c=make_board()
before={t.m_Uuid.AsString() for t in b.GetTracks()}
o=NativeOptions(pours_only=True,stitch_vias=False,pitch_mm=0.1,time_limit_s=10)
limited=extract.extract(b, NativeOptions(pours_only=True, stitch_vias=False, pitch_mm=0.1,
                                       plane_fanout_reach_mm=3.0, time_limit_s=10))
assert not router.run(limited).paths, "bounded search unexpectedly crossed the foreign island"
p=extract.extract(b,o)
assert any(code==a.GetNetCode() for code,cell in p.fanout),'pad was skipped although its plane is within 6 mm'
r=router.run(p)
for path in r.paths:apply.add_path(b,p.layers,path)
zone_filler(b).Fill(b.Zones());b.BuildConnectivity()
assert b.GetConnectivity().GetUnconnectedCount(True)==0
vias=[t for t in b.GetTracks() if t.GetClass()=='PCB_VIA' and t.m_Uuid.AsString() not in before and t.GetNetCode()==a.GetNetCode()]
assert vias
for v in vias:
    x,y=v.GetPosition().x/1e6,v.GetPosition().y/1e6
    assert not (16<x<24 and 8<y<22),(x,y,'via landed in foreign pour')
print('test_wide_plane_fanout: passed')

# Local plane goals can exist while every nearby via site is occupied.
b,a,c=make_board(1)
p=extract.extract(b,o)
assert any(p.fanout_goals.values()) and p.fanout_far_goals
for y in range(p.grid.h):
    for x in range(p.grid.w):
        if (p.grid.nm_x(x)-20_000_000)**2+(p.grid.nm_y(y)-15_000_000)**2 <= 3_300_000**2:
            p.via_block[y*p.grid.w+x]=1
far=p.fanout_far_goals
p.fanout_far_goals={}
assert not router.run(p).paths, 'local search should have no free via site'
p.fanout_far_goals=far
result=router.run(p)
assert result.paths, 'outer-ring fallback was not tried after local goals failed'
for path in result.paths:apply.add_path(b,p.layers,path)
zone_filler(b).Fill(b.Zones());b.BuildConnectivity()
assert b.GetConnectivity().GetUnconnectedCount(True)==0
print('test_wide_plane_fanout: blocked local sites use the outer fallback')
