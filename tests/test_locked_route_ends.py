"""A scoped repair extends a designer's locked inner-layer lane at its free end."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_locked_route_ends: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline, add_pad, add_track, add_via
from autorouter.engine.ai.board_copy import track_signature, uid
from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions, SCOPE_SELECTION


def main():
    for shove in (False, True):
        board = pcbnew.BOARD();board.SetCopperLayerCount(4);outline(board, 60, 20)
        net=pcbnew.NETINFO_ITEM(board,'COIL');board.Add(net)
        fp=pcbnew.FOOTPRINT(board);fp.SetReference('J1');board.Add(fp)
        add_pad(fp,1,5,10,net);add_pad(fp,2,50,10,net)
        add_track(board,net,pcbnew.F_Cu,(5,10),(8,10))
        add_via(board,net,8,10)
        add_track(board,net,pcbnew.In2_Cu,(8,10),(40,10))
        for t in board.GetTracks(): t.SetLocked(True)
        original={uid(t):track_signature(t) for t in board.GetTracks()}
        board.BuildConnectivity()
        options=NativeOptions(scope=SCOPE_SELECTION,scope_nets=['COIL'],shove=shove,
                              fanout_vias=False,stitch_vias=False,pitch_mm=0.1,time_limit_s=10)
        problem=extract.extract(board,options)
        li=next(l.index for l in problem.layers if l.kicad_id==pcbnew.In2_Cu)
        g=problem.grid;end=g.index(li,g.cell_x(40000000),g.cell_y(10000000))
        assert any(end in c.cells for c in problem.nets[net.GetNetCode()].clusters), shove
        job=pipeline.run_job(board,options,escalate=False)
        board.BuildConnectivity()
        assert board.GetConnectivity().GetUnconnectedCount(True)==0,(shove,job.failed)
        added=sum(t.GetLength() for t in board.GetTracks() if uid(t) not in original and t.GetClass()=='PCB_TRACK')/1e6
        assert added<15,(shove,added)
        assert {uid(t): track_signature(t) for t in board.GetTracks() if uid(t) in original} == original
        print('locked lane extended:',shove,round(added,3),'mm new copper')


if __name__=='__main__': main()
