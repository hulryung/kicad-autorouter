"""Pair skew is tuned without a user length group, with scope/lock safeguards."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_auto_pair_skew: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_track
from autorouter.engine.native import pipeline,tuning
from autorouter.engine.native.problem import NativeOptions,SCOPE_SELECTION
from autorouter.engine.ai.board_copy import track_signature,uid


def make_board(path):
    board=pcbnew.BOARD();outline(board,45,40);board.SetFileName(str(path))
    for name,y,end in [('DATA+',10,25),('DATA-',20,31)]:
        net=pcbnew.NETINFO_ITEM(board,name);board.Add(net)
        add_track(board,net,pcbnew.F_Cu,(5,y),(end,y),w=0.2)
    return board


def main():
    with tempfile.TemporaryDirectory() as work:
        board=make_board(Path(work)/'pair.kicad_pcb')
        opt=NativeOptions(kicad_tuning_patterns=False)
        assert not opt.length_groups
        assert len(pipeline.automatic_pair_groups(board,opt))==1
        report=pipeline.tune_lengths(board,opt)
        assert report and report.groups[0]['ok'],report.lines() if report else None
        lengths=list(tuning.track_lengths(board).values())
        assert max(lengths)-min(lengths)<=1000000,lengths
        print('automatic pair skew:',[round(x/1e6,3) for x in lengths])
        for kw in ({'diff_pairs':False},{'tune_lengths':False},{'pair_skew_tolerance_mm':0},
                   {'scope':SCOPE_SELECTION,'scope_nets':['UNRELATED']},
                   {'scope':SCOPE_SELECTION,'scope_nets':['DATA+']},{'skip_nets':['DATA*']}):
            untouched=make_board(Path(work)/'skip.kicad_pcb')
            before={uid(t):track_signature(t) for t in untouched.GetTracks()}
            assert pipeline.tune_lengths(untouched,NativeOptions(**kw)) is None
            assert {uid(t):track_signature(t) for t in untouched.GetTracks()}==before
        fragmented=make_board(Path(work)/'fragmented.kicad_pcb')
        add_track(fragmented,fragmented.FindNet('DATA+'),pcbnew.F_Cu,(35,10),(36,10),w=0.2)
        assert not pipeline.automatic_pair_groups(fragmented,NativeOptions())
        waived=make_board(Path(work)/'waived.kicad_pcb')
        assert pipeline.tune_lengths(waived,NativeOptions(pair_uncoupled_max_mm=30),
            pair_exceptions=[{'nets':['DATA+','DATA-'],'exempt_skew':True}]) is None
        opt.length_groups=[{'name':'Explicit','nets':['DATA+','DATA-'],'pair':True,'tolerance':250000}]
        assert pipeline.automatic_pair_groups(board,opt)==[]
        board=make_board(Path(work)/'locked.kicad_pcb')
        for t in board.GetTracks():t.SetLocked(True)
        before={uid(t):track_signature(t) for t in board.GetTracks()}
        report=pipeline.tune_lengths(board,NativeOptions(kicad_tuning_patterns=False))
        assert report and not report.groups[0]['ok']
        assert {uid(t):track_signature(t) for t in board.GetTracks()}==before
    print('test_auto_pair_skew: all passed')


if __name__=='__main__':main()
