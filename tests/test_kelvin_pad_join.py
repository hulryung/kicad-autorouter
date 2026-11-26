"""A sense route must reach the named pad, not the nearer same-net force bus."""
import sys,tempfile,copy,json,subprocess,hashlib
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_kelvin_pad_join: skipped (needs KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad,add_track
from autorouter.engine.kelvin import route_kelvin, _check_join_geometry
from autorouter.engine.native.problem import NativeOptions
from autorouter.engine.ai.board_copy import protected_state,track_signature


def fixture(blocked=False):
    b=pcbnew.BOARD();outline(b,50,40)
    n=pcbnew.NETINFO_ITEM(b,'SENSE_FORCE');b.Add(n)
    f=pcbnew.FOOTPRINT(b);f.SetReference('J1');b.Add(f)
    add_pad(f,1,10,10,n);add_pad(f,2,40,10,n);add_pad(f,3,10,12,n)
    add_track(b,n,pcbnew.F_Cu,(40,10),(40,12))
    add_track(b,n,pcbnew.F_Cu,(40,12),(10,12))
    if blocked:
        add_track(b,n,pcbnew.F_Cu,(25,0),(25,40))
    b.BuildConnectivity()
    return b


with tempfile.TemporaryDirectory() as folder:
    root=Path(folder);b=fixture();state=protected_state(b);copper=[track_signature(t) for t in b.GetTracks()]
    options=NativeOptions(pitch_mm=.1,allow_vias=False,routable_layers=[True,False],time_limit_s=10)
    result=route_kelvin(b,'J1.1','J1.2',root/'accepted',options=options,separation_mm=.15)
    assert result['after_opens'].get('SENSE_FORCE',0)==0,result
    assert not result['timed_out'], 'successful short-budget job falsely reported a timeout'
    assert protected_state(b)==state and [track_signature(t) for t in b.GetTracks()]==copper
    output=pcbnew.LoadBoard(result['board'])
    added=[t for t in output.GetTracks() if t.m_Uuid.AsString() not in {x.m_Uuid.AsString() for x in b.GetTracks()}]
    assert sum(t.GetLength() for t in added)>29000000,'sense route took the nearby force-bus shortcut'
    from autorouter.engine.ai.board_copy import snapshot
    saved=snapshot(fixture(),root/'cli-input'/'board.kicad_pcb')
    digest=hashlib.sha256(saved.read_bytes()).hexdigest()
    command=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts'/'route-kelvin.py'),
             str(saved),'J1.1','J1.2',str(root/'cli-output'),'--separation','.15','--no-vias','--time-limit','10']
    run=subprocess.run(command,capture_output=True,text=True,timeout=120)
    assert run.returncode==0,(run.stdout,run.stderr)
    assert json.loads((root/'cli-output'/'kelvin.json').read_text())['after_opens'].get('SENSE_FORCE',0)==0
    assert hashlib.sha256(saved.read_bytes()).hexdigest()==digest
    spacing=fixture();target=next(p for p in spacing.GetPads() if p.GetNumber()=='2')
    force=list(spacing.GetTracks())
    near=add_track(spacing,spacing.FindNet('SENSE_FORCE'),pcbnew.F_Cu,(15,11.6),(20,11.6))
    _check_join_geometry(spacing,target,force,[near],0)
    try:
        _check_join_geometry(spacing,target,force,[near],150000)
        raise AssertionError('same-net separation violation was accepted')
    except ValueError:
        pass
    partial=fixture();net=partial.FindNet('SENSE_FORCE')
    add_track(partial,net,pcbnew.F_Cu,(10,10),(12,10))
    result=route_kelvin(partial,'J1.1','J1.2',root/'partial',options=options,separation_mm=.15)
    assert result['after_opens'].get('SENSE_FORCE',0)==0
    through=fixture();join=next(p for p in through.GetPads() if p.GetNumber()=='2')
    join.SetAttribute(pcbnew.PAD_ATTRIB_PTH);join.SetLayerSet(join.PTHMask())
    join.SetSize(pcbnew.VECTOR2I(1800000,1800000));join.SetDrillSize(pcbnew.VECTOR2I(600000,600000))
    result=route_kelvin(through,'J1.1','J1.2',root/'through-hole',options=options,separation_mm=.15)
    assert result['after_opens'].get('SENSE_FORCE',0)==0
    blocked=fixture(True)
    try:
        route_kelvin(blocked,'J1.1','J1.2',root/'blocked',options=options,separation_mm=.15)
        raise AssertionError('crossed a same-net force wall outside the join pad')
    except ValueError:
        pass
    assert not (root/'blocked').exists()
    multilayer=copy.deepcopy(options);multilayer.allow_vias=True;multilayer.routable_layers=[True,True]
    result=route_kelvin(fixture(True),'J1.1','J1.2',root/'multilayer',options=multilayer,separation_mm=.15)
    routed=pcbnew.LoadBoard(result['board'])
    assert any(t.GetClass()=='PCB_VIA' for t in routed.GetTracks())
    assert result['after_opens'].get('SENSE_FORCE',0)==0
    # Independent geometry acceptance must catch a same-net shortcut even
    # if a routing implementation accidentally bypasses the planning mask.
    def shortcut(candidate, opts, **kwargs):
        add_track(candidate,candidate.FindNet('SENSE_FORCE'),pcbnew.F_Cu,(10,10),(10,12))
        return SimpleNamespace(timed_out=False)
    with patch('autorouter.engine.native.pipeline.run_job',shortcut):
        try:
            route_kelvin(fixture(),'J1.1','J1.2',root/'shortcut',options=options)
            raise AssertionError('same-net shortcut passed geometric acceptance')
        except ValueError as e:
            assert 'outside the designated pad' in str(e),str(e)
    assert not (root/'shortcut').exists()
    try:
        route_kelvin(output,'J1.1','J1.2',root/'already',options=options)
        raise AssertionError('silently reused an existing sense route')
    except ValueError as e:
        assert 'already connected' in str(e),str(e)
print('test_kelvin_pad_join: isolated join, force-wall rejection and original preservation passed')

# A blocked connection may skip optional refinement without exhausting time.
from autorouter.engine.native import pipeline
for escalation in (False,True):
    b=pcbnew.BOARD();outline(b,30,20)
    net=pcbnew.NETINFO_ITEM(b,'OPEN');b.Add(net)
    wallnet=pcbnew.NETINFO_ITEM(b,'WALL');b.Add(wallnet)
    fp=pcbnew.FOOTPRINT(b);fp.SetReference('J1');b.Add(fp)
    add_pad(fp,1,5,10,net);add_pad(fp,2,25,10,net)
    add_track(b,wallnet,pcbnew.F_Cu,(15,0),(15,20))
    b.BuildConnectivity()
    opts=NativeOptions(pitch_mm=.2,allow_vias=False,routable_layers=[True,False],
        time_limit_s=30,fanout_vias=False,stitch_vias=False,shove=False,
        diff_pairs=False,tune_lengths=False,pour_goals=False)
    messages=[]
    result=pipeline.run_job(b,opts,escalate=escalation,status=messages.append)
    assert result.failed==['OPEN'],result.failed
    assert not result.timed_out,(result,messages)
    assert ('refinement_skipped' in result.stats)==escalation,result.stats
    assert not any('Time limit reached' in message for message in messages),messages
    opts.time_limit_s=1e-9
    expired=pipeline.run_job(b,opts,escalate=escalation)
    assert expired.timed_out and expired.failed,expired
print('short-budget success, optional refinement and actual timeout: passed')
