"""Route an isolated sense component to one named join pad on a validated copy."""
import copy
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path


def _pad(board, name):
    if not isinstance(name, str) or '.' not in name:
        raise ValueError('pads must be named REF.NUMBER')
    ref, number = name.rsplit('.', 1)
    matches = [p for f in board.GetFootprints() if f.GetReference()==ref
               for p in f.Pads() if p.GetNumber()==number]
    if len(matches)!=1:
        raise ValueError('pad must resolve uniquely: '+name)
    return matches[0]


def _polygon(item, layer, clearance=0, inside=False):
    import pcbnew
    poly=pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(poly,layer,clearance,1000,
                                 pcbnew.ERROR_INSIDE if inside else pcbnew.ERROR_OUTSIDE)
    return poly


def _subtract(a,b):
    try:
        a.BooleanSubtract(b)
    except TypeError:
        import pcbnew
        a.BooleanSubtract(b,pcbnew.SHAPE_POLY_SET.PM_FAST)


def _restrict(problem, board, source, target, sense, force, separation):
    import pcbnew
    from .native.problem import Cluster,Connection
    from .native.grid import BLOCKED
    from .native import watchdog
    from .native.extract import _box
    code=source.GetNetCode();net=problem.nets[code];grid=problem.grid
    if net.layer_widths:
        raise ValueError('Kelvin routing currently requires one explicit track width across layers')
    keys={net.rules.key,(net.rules.via_diameter,net.rules.clearance)}
    gap=net.rules.clearance if separation is None else separation
    watchdog.stage("Kelvin force-copper isolation")
    for layer in problem.layers:
        watchdog.check()
        lid=layer.kicad_id
        allowed=_polygon(target,lid,inside=True) if target.IsOnLayer(lid) else None
        for item in force:
            watchdog.tick()
            if not item.IsOnLayer(lid):
                continue
            shape=_polygon(item,lid)
            if allowed is not None:_subtract(shape,allowed)
            if not shape.OutlineCount():continue
            x0,y0,x1,y1=_box(shape)
            for key in keys:
                if key not in problem.owners:continue
                owners=problem.owners[key];radius=key[0]//2+gap+problem.safety
                cells=grid.cells_where(layer.index,(x0-radius,y0-radius,x1+radius,y1+radius),
                    lambda x,y:shape.Collide(pcbnew.VECTOR2I(x,y),radius))
                grid.mark(owners,cells,BLOCKED)
    net.clusters=[];net.endpoints={};net.partner=0;net.route=True
    for pad in (source,target):
        pos=pad.GetPosition();cx,cy=grid.cell_x(pos.x),grid.cell_y(pos.y)
        if not grid.inside(cx,cy):
            raise ValueError('Kelvin pad is outside the routing window')
        cells=set()
        for layer in problem.layers:
            if pad.IsOnLayer(layer.kicad_id):
                idx=grid.index(layer.index,cx,cy)
                if problem.owners[net.rules.key][idx] in (0,code):
                    cells.add(idx);net.endpoints[idx]=(pos.x,pos.y,pos.x,pos.y,net.rules.width)
        if pad is source:
            for item in sense:
                for layer in problem.layers:
                    if not item.IsOnLayer(layer.kicad_id):continue
                    shape=item.GetEffectiveShape(layer.kicad_id)
                    cells.update(idx for idx in grid.cells_where(layer.index,_box(shape),
                        lambda x,y:shape.Collide(pcbnew.VECTOR2I(x,y),0))
                        if problem.owners[net.rules.key][idx] in (0,code))
        if not cells:
            raise ValueError('Kelvin pad has no legal centre cell at this width/pitch')
        net.clusters.append(Cluster(cells=cells,copper=set(cells),anchors=[(cx,cy)],refs={pad.GetParentFootprint().GetReference()}))
    a,b=net.clusters[0].anchors[0],net.clusters[1].anchors[0]
    problem.connections=[Connection(code,0,1,max(abs(a[0]-b[0]),abs(a[1]-b[1])),width=net.rules.width)]
    problem.stats['connections']=1
    problem.stats['kelvin_separation_nm']=gap
    return problem


def _check_join_geometry(board,target,force,added,gap):
    for lid in board.GetEnabledLayers().CuStack():
        allowed=_polygon(target,lid,inside=True) if target.IsOnLayer(lid) else None
        for item in force:
            if not item.IsOnLayer(lid):continue
            forbidden=_polygon(item,lid)
            if allowed is not None:_subtract(forbidden,allowed)
            if forbidden.OutlineCount() and any(t.IsOnLayer(lid) and forbidden.Collide(t.GetEffectiveShape(lid),gap) for t in added):
                raise ValueError('sense copper joins force copper outside the designated pad')


def route_kelvin(board, source_pad, join_pad, output_directory, *, options=None, separation_mm=None, status=None):
    """Publish a new copy only if sense copper touches force copper at join_pad.

    The source's connected sense component must be separate from the join pad.
    Both pads must share a net without copper zones. Existing copper and pad
    net assignments are preserved.
    Separation defaults to the net clearance; an explicit value is in mm.
    """
    import pcbnew
    from .ai import board_copy,context
    from .compat import connected_items
    from .native import extract,pipeline
    from .native.problem import NativeOptions,SCOPE_SELECTION
    tell=status or (lambda message: None)
    output=Path(output_directory).resolve()
    if output.exists():raise ValueError('output_directory must not already exist')
    if source_pad==join_pad:raise ValueError('source and join pads must differ')
    source,target=_pad(board,source_pad),_pad(board,join_pad)
    code=source.GetNetCode()
    if code<=0 or target.GetNetCode()!=code:raise ValueError('source and join pads must share an assigned net')
    if any(z.GetNetCode()==code and not z.GetIsRuleArea() for z in board.Zones()):
        raise ValueError('Kelvin routing of a net with copper zones is not supported')
    board.BuildConnectivity()
    sense_ids={source.m_Uuid.AsString()}|{i.m_Uuid.AsString() for i in connected_items(board.GetConnectivity(),source)}
    if target.m_Uuid.AsString() in sense_ids:
        raise ValueError('source is already connected to the join pad; existing Kelvin topology is not rewritten')
    separation=None
    if separation_mm is not None:
        if not isinstance(separation_mm,(int,float)) or not math.isfinite(separation_mm) or separation_mm<0:
            raise ValueError('separation_mm must be finite and nonnegative')
        separation=int(round(separation_mm*1000000))
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.kelvin-',dir=str(output.parent)) as folder:
        work=Path(folder);path=board_copy.snapshot(board,work/'input'/'board.kicad_pcb')
        candidate=board_copy.load_copy(path,board)
        source,target=_pad(candidate,source_pad),_pad(candidate,join_pad);code=source.GetNetCode()
        original={board_copy.uid(t):board_copy.track_signature(t) for t in candidate.GetTracks()}
        items=list(candidate.GetTracks())+list(candidate.GetPads())
        sense=[i for i in items if board_copy.uid(i) in sense_ids]
        force=[i for i in items if i.GetNetCode()==code and board_copy.uid(i) not in sense_ids and board_copy.uid(i)!=board_copy.uid(target)]
        protected=board_copy.protected_state(candidate)
        tell('Checking baseline DRC and connectivity...')
        before=context.drc_summary(candidate,work/'before')
        if not before.get('available') or not before.get('unconnected_by_net_complete'):
            raise RuntimeError('baseline DRC/connectivity unavailable')
        opts=copy.deepcopy(options) if options is not None else NativeOptions()
        opts.scope=SCOPE_SELECTION;opts.scope_nets=[source.GetNetname()];opts.scope_refs=[]
        opts.shove=False;opts.fanout_vias=False;opts.stitch_vias=False;opts.pours_only=False
        opts.diff_pairs=False;opts.pour_goals=False;opts.tune_lengths=False;opts.power_patterns=[]
        opts.direct_links=False;opts.neck_down=False
        def read(b,o,status):
            return _restrict(extract.extract(b,o,status=status),b,source,target,sense,force,separation)
        result=pipeline.run_job(candidate,opts,extract_fn=read,escalate=False,status=status)
        after_tracks={board_copy.uid(t):t for t in candidate.GetTracks()}
        if protected!=board_copy.protected_state(candidate) or any(
                uid not in after_tracks or board_copy.track_signature(after_tracks[uid])!=signature for uid,signature in original.items()):
            raise ValueError('Kelvin candidate changed existing copper or protected board state')
        added=[t for uid,t in after_tracks.items() if uid not in original]
        if not added or any(t.GetNetCode()!=code for t in added):raise ValueError('no isolated Kelvin route found')
        tell("Checking that sense copper joins only at the designated pad...")
        # Check actual new copper against all original force copper, subtracting
        # only an inner approximation of the designated pad on each layer.
        gap=getattr(result,"stats",{}).get("kelvin_separation_nm",separation or 0)
        _check_join_geometry(candidate,target,force,added,gap)
        candidate.BuildConnectivity()
        if board_copy.uid(target) not in {board_copy.uid(i) for i in connected_items(candidate.GetConnectivity(),source)}:
            raise ValueError('Kelvin route did not reach the designated pad')
        tell('Checking candidate DRC and connectivity...')
        after=context.drc_summary(candidate,work/'after')
        if not after.get('available') or not after.get('unconnected_by_net_complete'):
            raise RuntimeError('candidate DRC/connectivity unavailable')
        if Counter(after['violation_identities'])-Counter(before['violation_identities']):
            raise ValueError('Kelvin candidate introduces new DRC errors')
        if any(v>before['unconnected_by_net'].get(n,0) for n,v in after['unconnected_by_net'].items()):
            raise ValueError('Kelvin candidate regresses per-net connectivity')
        report={'source_pad':source_pad,'join_pad':join_pad,'net':source.GetNetname(),'added':len(added),
                'before_opens':before['unconnected_by_net'],'after_opens':after['unconnected_by_net'],
                'timed_out':result.timed_out,'separation_mm':gap/1000000,'board':str(output/'board.kicad_pcb')}
        accepted=work/'accepted';board_copy.snapshot(candidate,accepted/'board.kicad_pcb')
        (accepted/'kelvin.json').write_text(json.dumps(report,indent=2)+'\n');os.rename(accepted,output)
    return report
