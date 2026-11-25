"""Rip-up cannot leave the ends of a fixed bridge hanging after rerouting its net."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
from test_native_grid import make_problem,add_net,RULES,PITCH
from autorouter.engine.native import router
from autorouter.engine.native.problem import ExistingPath,RoutedPath,SCOPE_SELECTION

p=make_problem(layers=1,w=400,h=300)
add_net(p,1,(100,20),(100,250));add_net(p,2,(0,50),(399,50))
p.connections=p.connections[:1];p.options.scope=SCOPE_SELECTION;p.options.scope_nets=['N1']
p.options.allow_vias=False;p.options.flat_first=False;p.options.direct_links=False
p.options.rip_rounds=1;p.options.conn_time_s=10;p.options.max_expansions=400000
# A locked/fixed bridge joins two movable portions but carries no pad itself.
p.grid.mark(p.owners[RULES.key],p.grid.segment_cells(0,150*PITCH,150*PITCH,250*PITCH,150*PITCH,400000),2)
segments=[(0,0,50*PITCH,0,150*PITCH),(0,0,150*PITCH,150*PITCH,150*PITCH),
          (0,250*PITCH,150*PITCH,399*PITCH,150*PITCH),(0,399*PITCH,150*PITCH,399*PITCH,50*PITCH)]
path=RoutedPath(2,RULES.width,segments,[],RULES.via_diameter,RULES.via_drill,pid=1)
cells=[]
for s in segments:cells+=p.grid.segment_cells(*s,PITCH//2)
ex=ExistingPath(path,[(0,1)],0,cells,[])
ex.fixed_contacts={(0,150*PITCH,150*PITCH),(0,250*PITCH,150*PITCH)}
p.existing=[ex]
r=router.Router(p);result=r.run()
copper=set().union(*(set(x.cells) for x in r.paths.values() if x.conn.net==2))
assert {p.grid.index(li,x//PITCH,y//PITCH) for li,x,y in ex.fixed_contacts}<=copper,'rerouting preserved pad connectivity but abandoned the fixed bridge ends'
assert r._find(2,0)==r._find(2,1)
assert result.failed_detail[0]['candidate_rejected']=='fixed_copper_contacts_would_be_lost'
print('test_fixed_remnant_contacts: passed',result.routed,'routed,',r.undone,'rolled back')
