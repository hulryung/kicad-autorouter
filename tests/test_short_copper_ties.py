"""Short opens to fixed tracks/vias, including a plane pad without a via site."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_short_copper_ties: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad,add_track,add_via
from test_plane_foreign_pour import zone
from autorouter.engine.compat import zone_filler
from autorouter.engine.native import pipeline
from autorouter.engine.native.problem import NativeOptions
from autorouter.engine.ai.board_copy import uid,track_signature


def main():
    for plane,blocked in ((False,False),(True,False),(True,True)):
        b=pcbnew.BOARD();b.SetCopperLayerCount(4);outline(b,40,30)
        net=pcbnew.NETINFO_ITEM(b,'+12V');b.Add(net)
        fp=pcbnew.FOOTPRINT(b);fp.SetReference('J1');b.Add(fp)
        add_pad(fp,1,5,10,net);add_pad(fp,2,20,10,net)
        add_track(b,net,pcbnew.F_Cu,(5,10),(18,10))
        if plane:
            add_via(b,net,18,10)
            zone(b,net,pcbnew.In2_Cu,((1,1),(39,1),(39,29),(1,29)),0)
            keep=pcbnew.ZONE(b);keep.SetIsRuleArea(True);keep.SetLayer(pcbnew.F_Cu)
            keep.SetDoNotAllowVias(True);keep.SetDoNotAllowTracks(False)
            keep.SetDoNotAllowPads(False)
            poly=keep.Outline();poly.NewOutline()
            for x,y in ((17,7),(23,7),(23,13),(17,13)):poly.Append(x*1000000,y*1000000)
            b.Add(keep)
            zone_filler(b).Fill(b.Zones())
        if blocked:
            wall=pcbnew.NETINFO_ITEM(b,'WALL');b.Add(wall)
            add_track(b,wall,pcbnew.F_Cu,(19,0),(19,30),w=0.3)
        for t in b.GetTracks():t.SetLocked(True)
        before={uid(t):track_signature(t) for t in b.GetTracks()}
        b.BuildConnectivity()
        result=pipeline.run_job(b,NativeOptions(pitch_mm=0.1,time_limit_s=10,stitch_vias=False),escalate=False)
        b.BuildConnectivity()
        assert not b.GetConnectivity().GetUnconnectedCount(True),(plane,result.failed)
        added=[t for t in b.GetTracks() if uid(t) not in before]
        length=sum(t.GetLength() for t in added if t.GetClass()=='PCB_TRACK')/1e6
        if blocked:
            assert any(t.GetClass()=='PCB_VIA' for t in added), 'blocked tie needs another plane via'
            assert all(min(t.GetStart().x,t.GetEnd().x)>19000000 for t in added if t.GetClass()=='PCB_TRACK' and t.GetLayer()==pcbnew.F_Cu)
        else:
            assert length<3,(plane,length)
            assert all(t.GetClass()!='PCB_VIA' for t in added),(plane,'unnecessary via')
        assert {uid(t):track_signature(t) for t in b.GetTracks() if uid(t) in before}==before
        print('short tie',plane,'blocked',blocked,round(length,3),'mm')

if __name__=='__main__':main()
