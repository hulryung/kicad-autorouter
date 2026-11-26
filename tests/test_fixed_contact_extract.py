"""Extract the two fixed bridge endpoints that a movable piece must preserve."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_fixed_contact_extract: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad,add_track
from autorouter.engine.native import extract
from autorouter.engine.native.problem import NativeOptions
b=pcbnew.BOARD();outline(b,40,30)
n=pcbnew.NETINFO_ITEM(b,'SIGNAL');b.Add(n)
f=pcbnew.FOOTPRINT(b);f.SetReference('J1');b.Add(f)
add_pad(f,1,5,5,n);add_pad(f,2,35,5,n)
for a,z in [((5,5),(5,15)),((5,15),(15,15)),((15,15),(25,15)),((25,15),(35,15)),((35,15),(35,5))]:
    add_track(b,n,pcbnew.F_Cu,a,z)
for t in b.GetTracks():
    if t.GetStart().x==15000000:t.SetLocked(True)
b.BuildConnectivity()
p=extract.extract(b,NativeOptions(shove=True,fanout_vias=False,stitch_vias=False))
paths=[x for x in p.existing if x.path.net==n.GetNetCode()]
assert len(paths)==1,len(paths)
assert paths[0].fixed_contacts=={(0,15000000,15000000),(0,25000000,15000000)},paths[0].fixed_contacts
assert all(not t.IsLocked() for x in paths for t in x.items)
print('test_fixed_contact_extract: passed')

# A fixed T junction already supports the bridge end at x=15. Moving a
# redundant unlocked branch cannot make that endpoint dangling.
junction=add_track(b,n,pcbnew.B_Cu,(15,15),(15,20))
junction.SetLocked(True)
b.BuildConnectivity()
p=extract.extract(b,NativeOptions(shove=True,fanout_vias=False,stitch_vias=False))
contacts=set().union(*(x.fixed_contacts for x in p.existing if x.path.net==n.GetNetCode()))
assert contacts=={(0,15000000,15000000),(0,25000000,15000000)},contacts
junction.SetLayer(pcbnew.F_Cu)
for t in b.GetTracks():
    if t.GetStart().x==15000000:t.SetLocked(True)
b.BuildConnectivity()
p=extract.extract(b,NativeOptions(shove=True,fanout_vias=False,stitch_vias=False))
contacts=set().union(*(x.fixed_contacts for x in p.existing if x.path.net==n.GetNetCode()))
assert contacts=={(0,25000000,15000000)},contacts
# A pad supports the remaining fixed endpoint even when its movable branch
# is rerouted. No artificial contact requirement should keep that branch fixed.
add_pad(f,3,25,15,n)
b.BuildConnectivity()
p=extract.extract(b,NativeOptions(shove=True,fanout_vias=False,stitch_vias=False))
contacts=set().union(*(x.fixed_contacts for x in p.existing if x.path.net==n.GetNetCode()))
assert not contacts,contacts
print('test_fixed_contact_extract: fixed junctions and pads remain valid supports')
