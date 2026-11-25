"""Raster reuse is bounded and survives geometry/rule changes without stale maps."""
import copy
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
from autorouter.engine.native.raster_cache import RasterCache
from autorouter.engine.native.grid import SpanCells
cache=RasterCache(3000)
piece=SpanCells();piece.add_span(1,100)
first=cache.get('a',lambda:piece)
assert list(first)==list(piece) and first.spans==tuple(piece.spans)
assert cache.get('a',lambda:1/0) is first
assert copy.deepcopy(cache) is cache
for i in range(20):cache.get(str(i),lambda:range(100))
assert cache.bytes<=cache.max_bytes
assert len(cache.entries)<20
assert RasterCache(0).get('a',lambda:piece) is piece
try:
    import pcbnew
except ImportError:
    print('test_raster_cache: portable bounds/reuse passed; native part skipped')
    raise SystemExit(0)
from test_fanout_stub_clearance import make_board
from autorouter.engine.native import extract
from autorouter.engine.native.problem import NativeOptions
b,nets=make_board()
o=NativeOptions(pitch_mm=.2,fanout_vias=False,stitch_vias=False,raster_cache_mb=8)

def check():
    warm=extract.extract(b,o)
    cold_options=copy.copy(o);cold_options.raster_cache_mb=0
    cold=extract.extract(b,cold_options)
    assert warm.grid.size==cold.grid.size and warm.owners==cold.owners
    assert warm.via_block==cold.via_block
    assert {c:n.endpoints for c,n in warm.nets.items()}=={c:n.endpoints for c,n in cold.nets.items()}
    assert warm.stats['raster_cache']['bytes']<=8*1024*1024
    assert not cold.stats['raster_cache']['bytes']
    return warm
check();second=check();assert second.stats['raster_cache']['hits']>0
p=next(p for p in b.GetPads() if p.GetNetCode()==nets['SCL'].GetNetCode())
p.SetPosition(p.GetPosition()+pcbnew.VECTOR2I(500000,200000));check()
p.SetOrientationDegrees(45);check()
p.SetSize(pcbnew.VECTOR2I(1200000,400000));check()
p.SetOrientationDegrees(0);p.SetShape(pcbnew.PAD_SHAPE_ROUNDRECT);p.SetRoundRectRadiusRatio(.25);check()
p.SetRoundRectRadiusRatio(.1);check()
p.SetNet(nets['SDA']);check()
b.GetDesignSettings().m_NetSettings.GetDefaultNetclass().SetClearance(250000);check()
t=pcbnew.PCB_TRACK(b);t.SetNet(nets['SDA']);t.SetLayer(pcbnew.F_Cu);t.SetWidth(200000)
t.SetStart(pcbnew.VECTOR2I(2000000,2000000));t.SetEnd(pcbnew.VECTOR2I(8000000,2000000));b.Add(t);check()
t.SetEnd(pcbnew.VECTOR2I(8000000,4000000));check()
b.Remove(t);check()
o.pitch_mm=.1;check()
for a,z in zip([(24,12),(26,12),(26,14),(24,14)],[(26,12),(26,14),(24,14),(24,12)]):
    edge=pcbnew.PCB_SHAPE(b);edge.SetShape(pcbnew.SHAPE_T_SEGMENT);edge.SetLayer(pcbnew.Edge_Cuts);edge.SetWidth(50000)
    edge.SetStart(pcbnew.VECTOR2I(int(a[0]*1e6),int(a[1]*1e6)));edge.SetEnd(pcbnew.VECTOR2I(int(z[0]*1e6),int(z[1]*1e6)));b.Add(edge)
check()
print('test_raster_cache: warm/cold maps match after pad, net, clearance, track and grid changes')

# A real multi-pass job shares its bounded cache but leaves caller options alone.
from autorouter.engine.native import pipeline
b,_=make_board();options=NativeOptions(pitch_mm=.2,stitch_vias=False)
seen=[]
def read(board,opts,status):
    problem=extract.extract(board,opts,status=status)
    seen.append((id(opts._raster_cache),problem.stats['raster_cache']['hits']))
    return problem
job=pipeline.run_job(b,options,extract_fn=read,escalate=False)
assert len(seen)>1 and len({key for key,hits in seen})==1
assert any(hits for key,hits in seen[1:])
assert job.stats['raster_cache']['hits']==sum(hits for key,hits in seen)
assert not hasattr(options,'_raster_cache'), 'job cache leaked into caller options'
print('test_raster_cache: actual pipeline passes reuse one bounded cache')
