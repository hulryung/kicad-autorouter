"""Mixed-net pour territories stay reserved through routing and smoothing."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_protected_pours: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline, add_pad
from test_plane_foreign_pour import zone, pt
from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions


def fixture():
    board=pcbnew.BOARD();board.SetCopperLayerCount(4);outline(board,40,30)
    a=pcbnew.NETINFO_ITEM(board,'RAIL_A');board.Add(a)
    b=pcbnew.NETINFO_ITEM(board,'RAIL_B');board.Add(b)
    fp=pcbnew.FOOTPRINT(board);fp.SetReference('J1');board.Add(fp)
    add_pad(fp,1,5,15,a);add_pad(fp,2,35,15,a)
    add_pad(fp,3,20,10,b)
    for pad in fp.Pads():
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetLayerSet(pcbnew.LSET.AllCuMask())
        pad.SetDrillSize(pt(0.3,0.3))
    zone(board,a,pcbnew.In2_Cu,((1,1),(39,1),(39,29),(1,29)),0)
    z=zone(board,b,pcbnew.In2_Cu,((16,5),(24,5),(24,25),(16,25)),1)
    hole=z.Outline().NewHole(0)
    for x,y in ((18,18),(18,22),(22,22),(22,18)):
        z.Outline().Append(int(x*1e6),int(y*1e6),0,hole)
    board.BuildConnectivity()
    return board,a,b


def main():
    for enabled in (False,True):
        board,a,b=fixture()
        opt=NativeOptions(protect_pour_regions=enabled,plane_nets=False,
                          fanout_vias=False,stitch_vias=False,pour_goals=False,
                          routable_layers=[False,False,True,False],
                          pitch_mm=0.2,time_limit_s=10)
        problem=extract.extract(board,opt)
        g=problem.grid; li=next(l.index for l in problem.layers if l.kicad_id==pcbnew.In2_Cu)
        def owner(net,x,y):
            n=problem.nets[net.GetNetCode()]
            return problem.owners[n.rules.key][g.index(li,g.cell_x(int(x*1e6)),g.cell_y(int(y*1e6)))]
        if enabled:
            assert owner(a,20,15)==b.GetNetCode(),owner(a,20,15)
            assert owner(a,10,15)==a.GetNetCode()
            assert owner(a,20,20)==a.GetNetCode(), 'hole must expose underlying zone'
            assert owner(b,20,15)==b.GetNetCode(), 'own higher-priority territory stays usable'
        else:
            assert owner(a,20,15)==0
        job=pipeline.run_job(board,opt,escalate=False)
        assert not job.failed,job.failed
        assert any(t.GetClass()=='PCB_TRACK' and t.GetLayer()==pcbnew.In2_Cu for t in board.GetTracks())
        if enabled:
            for t in board.GetTracks():
                if t.GetClass()!='PCB_TRACK' or t.GetLayer()!=pcbnew.In2_Cu or t.GetNetCode()!=a.GetNetCode():
                    continue
                # Check the emitted geometry, after simplification, against the protected corridor.
                for k in range(101):
                    start,end=t.GetStart(),t.GetEnd()
                    x=(start.x+(end.x-start.x)*k/100)/1e6
                    y=(start.y+(end.y-start.y)*k/100)/1e6
                    assert not (16<x<24 and 5<y<25 and not (18<x<22 and 18<y<22)),(x,y)
    print('test_protected_pours: passed')

if __name__=='__main__': main()
