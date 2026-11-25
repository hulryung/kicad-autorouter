"""A scoped repair must not exchange an open target for a broken existing net."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
from test_native_grid import make_problem,add_net,RULES,PITCH
from autorouter.engine.native import router
from autorouter.engine.native.problem import ExistingPath,RoutedPath,SCOPE_SELECTION

p=make_problem(layers=1,w=60,h=40)
add_net(p,1,(0,20),(59,20));add_net(p,2,(30,2),(30,37))
p.connections=p.connections[:1]
p.options.scope=SCOPE_SELECTION;p.options.scope_nets=['N1']
p.options.allow_vias=False;p.options.flat_first=False;p.options.direct_links=False
p.options.rip_rounds=1;p.options.conn_time_s=2
path=RoutedPath(2,RULES.width,[(0,30*PITCH,2*PITCH,30*PITCH,37*PITCH)],[],RULES.via_diameter,RULES.via_drill,pid=1)
p.existing=[ExistingPath(path,[(0,1)],0,[p.grid.index(0,30,y) for y in range(2,38)],[])]
r=router.Router(p);result=r.run()
assert r._find(2,0)==r._find(2,1),'repair broke the previously connected out-of-scope net'
assert r._find(1,0)!=r._find(1,1),'impossible crossing should remain open'
assert any(x.existing and x.path.segments==path.segments for x in r.paths.values())
assert result.failed and all(c.net==1 for c in result.failed)
print('test_scoped_shove_preserves_connections: passed')
