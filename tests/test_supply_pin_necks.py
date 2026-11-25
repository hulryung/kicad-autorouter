"""Fine-pitch supply pin exits use the neck width, retaining wide trunk routes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_supply_pin_necks: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline, add_pad
from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions, SCOPE_SELECTION
from autorouter.engine.compat import connected_items

b=pcbnew.BOARD();b.SetCopperLayerCount(4);outline(b,30,30)
ds=b.GetDesignSettings();ns=ds.m_NetSettings
ns.GetDefaultNetclass().SetClearance(150000);ns.GetDefaultNetclass().SetTrackWidth(200000)
power=pcbnew.NETCLASS('PWR');power.SetTrackWidth(500000);power.SetClearance(150000)
power.SetViaDiameter(600000);power.SetViaDrill(300000)
ns.SetNetclass('PWR',power);ns.SetNetclassPatternAssignment('+AVDD','PWR');ds.m_TrackMinWidth=150000
v=pcbnew.NETINFO_ITEM(b,'+AVDD');b.Add(v)
g=pcbnew.NETINFO_ITEM(b,'GND');b.Add(g)
f=pcbnew.FOOTPRINT(b);f.SetReference('U1');f.SetPosition(pcbnew.VECTOR2I(14000000,14000000));b.Add(f)
for side in range(4):
    for i in range(12):
        n=side*12+i+1;t=11.25+i*.5
        x,y=((10,t),(t,18),(18,16.75-i*.5),(16.75-i*.5,10))[side]
        w,h=(1.5,.3) if side%2==0 else (.3,1.5)
        add_pad(f,n,x,y,v if n in (4,8,11,15,17,21) else g,w=w,h=h)
b.BuildConnectivity()
o=NativeOptions(scope=SCOPE_SELECTION,scope_nets=['+AVDD'],pitch_mm=.1,
                fanout_vias=False,stitch_vias=False,diff_pairs=False,tune_lengths=False,time_limit_s=10)
result=pipeline.run_job(b,o,escalate=False)
b.BuildConnectivity()
pads=[p for p in f.Pads() if p.GetNetCode()==v.GetNetCode()]
seen={i.m_Uuid.AsString() for i in connected_items(b.GetConnectivity(),pads[0])}|{pads[0].m_Uuid.AsString()}
assert all(p.m_Uuid.AsString() in seen for p in pads),result.failed
tracks=[t for t in b.GetTracks() if t.GetNetCode()==v.GetNetCode() and t.GetClass()=='PCB_TRACK']
assert any(t.GetWidth()==500000 for t in tracks),'trunk width was reduced'
assert any(t.GetWidth()<500000 for t in tracks),'fine-pitch necks were not emitted'
from test_fanout_stub_clearance import violations
assert not violations(b,v.GetNetCode()), 'supply neck violates adjacent pad clearance'
print('test_supply_pin_necks: six supply pins connected with necks and wide trunks')
