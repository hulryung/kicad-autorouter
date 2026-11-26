"""Real DRC gates translated/rotated channel copies and rejects blocked copies."""
import sys,tempfile,json,subprocess,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_channel_template: skipped (needs KiCad Python)')
    raise SystemExit(0)
from test_shove_scope import outline,add_pad,add_track,add_via
from autorouter.engine.channel_template import replicate_channel, _plan
from autorouter.engine.ai.board_copy import protected_state,track_signature


def fixture(rotated=False,blocked=False):
    b=pcbnew.BOARD();outline(b,50,40)
    nets={}
    for name in ['SOURCE','TARGET','OBSTACLE']:
        n=pcbnew.NETINFO_ITEM(b,name);b.Add(n);nets[name]=n
    f=pcbnew.FOOTPRINT(b);f.SetReference('U1');f.SetPosition(pcbnew.VECTOR2I(10000000,10000000));b.Add(f)
    add_pad(f,1,10,10,nets['SOURCE']);add_pad(f,2,20,10,nets['SOURCE'])
    q=pcbnew.FOOTPRINT(b);q.SetReference('U2');q.SetPosition(pcbnew.VECTOR2I(35000000,25000000));b.Add(q)
    q.SetOrientationDegrees(90 if rotated else 0)
    add_pad(q,1,35,25,nets['TARGET']);add_pad(q,2,35 if rotated else 45,15 if rotated else 25,nets['TARGET'])
    for p in q.Pads():p.SetOrientationDegrees(90 if rotated else 0)
    add_track(b,nets['SOURCE'],pcbnew.F_Cu,(10,10),(12,10))
    add_track(b,nets['SOURCE'],pcbnew.F_Cu,(18,10),(20,10))
    for x in [12,18]:add_via(b,nets['SOURCE'],x,10)
    arc=pcbnew.PCB_ARC(b);arc.SetLayer(pcbnew.B_Cu);arc.SetNet(nets['SOURCE']);arc.SetWidth(300000)
    arc.SetStart(pcbnew.VECTOR2I(12000000,10000000));arc.SetMid(pcbnew.VECTOR2I(15000000,7000000));arc.SetEnd(pcbnew.VECTOR2I(18000000,10000000));b.Add(arc)
    if blocked:
        r=pcbnew.FOOTPRINT(b);r.SetReference('X1');b.Add(r);add_pad(r,1,40,25,nets['OBSTACLE'],w=8,h=8)
    b.BuildConnectivity()
    return b


with tempfile.TemporaryDirectory() as folder:
    root=Path(folder)
    for rotated in [False,True]:
        b=fixture(rotated);state=protected_state(b);copper=[track_signature(t) for t in b.GetTracks()]
        result=replicate_channel(b,{'U1':'U2'},root/str(rotated))
        assert result['added']==5 and result['after_opens'].get('TARGET',0)==0,result
        assert result['before_opens']['TARGET']==1
        assert protected_state(b)==state and [track_signature(t) for t in b.GetTracks()]==copper
        output=pcbnew.LoadBoard(result['board'])
        assert len({t.m_Uuid.AsString() for t in output.GetTracks()})==10
        again=replicate_channel(output,{'U1':'U2'},root/('again-'+str(rotated)))
        assert again['added']==0 and again['duplicates_skipped']==5,again
    # Exercise the shipped headless entry point against a saved project.
    from autorouter.engine.ai.board_copy import snapshot
    source=snapshot(fixture(),root/'cli-input'/'board.kicad_pcb')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    mapping=root/'mapping.json';mapping.write_text(json.dumps({'U1':'U2'}))
    command=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts'/'replicate-channel.py'),
             str(source),str(mapping),str(root/'cli-output')]
    result=subprocess.run(command,capture_output=True,text=True,timeout=120)
    assert result.returncode==0,(result.stdout,result.stderr)
    assert json.loads((root/'cli-output'/'replication.json').read_text())['added']==5
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
    b=fixture()
    shared=pcbnew.NETINFO_ITEM(b,'GND');b.Add(shared)
    fs={f.GetReference():f for f in b.GetFootprints()}
    add_pad(fs['U1'],3,10,12,shared);add_pad(fs['U2'],3,35,27,shared)
    mapping,_,_,_,excluded=_plan(b,{'U1':'U2'})
    assert excluded==['GND'] and len(mapping)==1
    for mapping in [{'U1':'U1'},{'U1':'missing'}]:
        try:
            _plan(b,mapping)
            raise AssertionError('invalid reference mapping accepted')
        except ValueError:
            pass
    group=pcbnew.PCB_GROUP(b);b.Add(group);group.AddItem(next(iter(b.GetTracks())))
    try:
        replicate_channel(b,{'U1':'U2'},root/'grouped')
        raise AssertionError('group semantics silently discarded')
    except ValueError as e:
        assert 'grouped' in str(e)
    assert not (root/'grouped').exists()
    b=fixture(blocked=True);state=protected_state(b);copper=[track_signature(t) for t in b.GetTracks()]
    try:
        replicate_channel(b,{'U1':'U2'},root/'blocked')
        raise AssertionError('a shorted template was accepted')
    except ValueError as e:
        assert 'new DRC errors' in str(e),str(e)
    assert not (root/'blocked').exists()
    assert protected_state(b)==state and [track_signature(t) for t in b.GetTracks()]==copper
    b=fixture();q=next(f for f in b.GetFootprints() if f.GetReference()=='U2')
    next(iter(q.Pads())).Move(pcbnew.VECTOR2I(1000000,0))
    try:
        replicate_channel(b,{'U1':'U2'},root/'misaligned')
        raise AssertionError('inconsistent pad transform accepted')
    except ValueError as e:
        assert 'rigid' in str(e)
    assert not (root/'misaligned').exists()
print('test_channel_template: translation, rotation, idempotence and atomic DRC rejection passed')
