"""Scoped raster windows preserve obstacles, fixed boundary crossings and anchors."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_scoped_grid: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad,add_track
from autorouter.engine.native import extract,pipeline,router
from autorouter.engine.native.problem import NativeOptions,SCOPE_SELECTION
from autorouter.engine.ai.board_copy import uid,track_signature


def fixture():
    b=pcbnew.BOARD();b.SetCopperLayerCount(4);outline(b,200,110)
    nets=[]
    for name,points in [('LOCAL',[(20,20),(25,25)]),('CROSS',[(5,23),(100,23)]),('FAR',[(150,80),(160,80)])]:
        n=pcbnew.NETINFO_ITEM(b,name);b.Add(n);nets.append(n)
        f=pcbnew.FOOTPRINT(b);f.SetReference(name);b.Add(f)
        for k,(x,y) in enumerate(points):add_pad(f,k+1,x,y,n)
    add_track(b,nets[1],pcbnew.F_Cu,(5,23),(100,23))
    add_track(b,nets[2],pcbnew.F_Cu,(150,80),(160,80))
    b.BuildConnectivity()
    return b,nets


def options(enabled=True):
    return NativeOptions(scope=SCOPE_SELECTION,scope_nets=['LOCAL'],shove=True,scoped_grid=enabled,
                         fanout_vias=False,stitch_vias=False,pitch_mm=0.2,time_limit_s=10)

b,nets=fixture();whole=extract.extract(b,options(False));local=extract.extract(b,options())
router.Router(whole);router.Router(local)  # compare the complete ownership maps, including movable copper
g,h=local.grid,whole.grid
assert local.stats['scoped_grid'] and g.size<h.size/20,(g.size,h.size)
assert (g.ox-h.ox)%g.pitch==0 and (g.oy-h.oy)%g.pitch==0
assert not local.existing,'boundary-crossing and far foreign tracks must stay fixed'
ref_options=options();ref_options.scope_nets=[];ref_options.scope_refs=['LOCAL']
ref_problem=extract.extract(b,ref_options)
assert ref_problem.stats['grid_window_nm']==local.stats['grid_window_nm']
for key,owner in local.owners.items():
    full=whole.owners[key]
    for li in range(g.layers):
        for x in range(5,g.w-5,7):
            for y in range(5,g.h-5,7):
                a=g.index(li,x,y);z=h.index(li,h.cell_x(g.nm_x(x)),h.cell_y(g.nm_y(y)))
                assert owner[a]==full[z],(key,li,x,y,owner[a],full[z])
before={uid(t):track_signature(t) for t in b.GetTracks()}
job=pipeline.run_job(b,options(),escalate=False)
b.BuildConnectivity()
assert not job.failed,job.failed
assert b.GetConnectivity().GetUnconnectedCount(True)==0
assert {uid(t):track_signature(t) for t in b.GetTracks() if uid(t) in before}==before
# Deliberately partial selected-net copper stays inside the window too.
b,nets=fixture();add_track(b,nets[0],pcbnew.In2_Cu,(20,20),(150,20))
p=extract.extract(b,options())
assert p.grid.inside(p.grid.cell_x(150000000),p.grid.cell_y(20000000))
# Zero margin retains the historical unrestricted scoped-shove mode.
o=options();o.shove_margin_mm=0
assert not extract.extract(b,o).stats['scoped_grid']
# A longer detour outside the local window remains available in full-grid mode.
for enabled in (True,False):
    b,_=fixture();o=options(enabled);o.shove=False;o.allow_vias=False
    o.rip_up=False;o.retry_failed=False;o.time_limit_s=30
    job=pipeline.run_job(b,o,escalate=False)
    if enabled:
        assert job.failed==['LOCAL'],job.failed
        assert job.failed_detail[0]['verdict'] in ('no_path_in_scope_window','enclosed_in_scope_window'),job.failed_detail
        assert job.failed_detail[0]['grid_window_nm']
    else:
        assert not job.failed,job.failed
print('test_scoped_grid: passed',g.size,'of',h.size,'cells')
