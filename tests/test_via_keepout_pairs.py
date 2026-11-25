"""A paired search and its single-ended fallback cannot bypass a member's mask."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
from test_native_grid import make_problem
from autorouter.engine.native import router
from autorouter.engine.native.grid import BLOCKED
from autorouter.engine.native.problem import NetRules, NetInfo, Cluster, Connection

for blocked in (False,True):
    p=make_problem(layers=2,w=110,h=40,via_cost=20);g=p.grid
    rules=NetRules(150000,150000,450000,200000)
    p.owners={rules.key:g.new_owner_map(),(450000,150000):g.new_owner_map()}
    for own in p.owners.values():g.mark(own,[g.index(0,45,y) for y in range(40)],BLOCKED)
    for code,y in ((1,18),(2,21)):
        n=NetInfo(code,str(code),rules,partner=3-code,pair_gap=150000)
        for x in (5,100):
            cell=g.index(0,x,y)
            n.clusters.append(Cluster(cells={cell},anchors=[(x,y)],refs={str(x)}))
            px=g.nm_x(x-2 if x==5 else x+2)
            n.endpoints[cell]=(g.nm_x(x),g.nm_y(y),px,g.nm_y(y),150000)
            for own in p.owners.values():g.mark(own,[cell],code)
        p.nets[code]=n;p.connections.append(Connection(code,0,1,95,0,150000,pair=1))
    if blocked:p.net_via_blocks[1]=bytearray([1])*g.plane
    result=router.run(p)
    if blocked:
        assert any(c.net==1 for c in result.failed)
        assert not any(path.net==1 and path.vias for path in result.paths)
    else:
        assert result.pairs==1 and not result.failed
        assert all(len(path.vias)==2 for path in result.paths)
print('test_via_keepout_pairs: coupled and fallback via placement respect member restrictions')
