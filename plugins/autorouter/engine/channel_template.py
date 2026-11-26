"""Replicate channel copper into a validated project copy; never edit the input."""
import json
import math
import os
import tempfile
from collections import Counter
from pathlib import Path


def _plan(board, references):
    if not isinstance(references, dict) or not references or any(
            not isinstance(a, str) or not isinstance(b, str) for a,b in references.items()):
        raise ValueError('reference_map must map source references to destination references')
    if len(set(references.values())) != len(references) or set(references) & set(references.values()):
        raise ValueError('source and destination references must be distinct and one-to-one')
    footprints = {}
    for fp in board.GetFootprints():
        footprints.setdefault(fp.GetReference(), []).append(fp)
    if any(len(footprints.get(ref, [])) != 1 for ref in list(references)+list(references.values())):
        raise ValueError('every mapped reference must identify exactly one footprint')
    pairs = [(footprints[a][0], footprints[b][0]) for a,b in references.items()]
    source, target = pairs[0]
    origin, destination = source.GetPosition(), target.GetPosition()
    angle = target.GetOrientationDegrees()-source.GetOrientationDegrees()
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    def transformed(pos):
        x,y = pos.x-origin.x,pos.y-origin.y
        return destination.x+cosine*x+sine*y, destination.y-sine*x+cosine*y
    net_map = {}
    source_pads = set()
    for a,b in pairs:
        if a.GetLayer()!=b.GetLayer():
            raise ValueError('mirrored/flipped channel placement is not supported')
        ap,bp = list(a.Pads()),list(b.Pads())
        an,bn = {p.GetNumber():p for p in ap},{p.GetNumber():p for p in bp}
        if len(an)!=len(ap) or len(bn)!=len(bp) or set(an)!=set(bn):
            raise ValueError('mapped footprints need identical unique pad numbers')
        for number,p in an.items():
            q=bn[number];x,y=transformed(p.GetPosition());pos=q.GetPosition()
            orientation=(q.GetOrientationDegrees()-p.GetOrientationDegrees()-angle+180)%360-180
            if (math.hypot(x-pos.x,y-pos.y)>1000 or abs(orientation)>1e-6
                    or p.GetSize()!=q.GetSize() or p.GetShape()!=q.GetShape()
                    or p.GetAttribute()!=q.GetAttribute()
                    or tuple(p.GetLayerSet().CuStack())!=tuple(q.GetLayerSet().CuStack())):
                raise ValueError('mapped pads do not share one rigid translation/rotation and pad geometry')
            code,other=p.GetNetCode(),q.GetNetCode()
            if bool(code)!=bool(other) or (code in net_map and net_map[code]!=other):
                raise ValueError('pad mapping gives inconsistent net assignments')
            net_map[code]=other
            source_pads.add(p.m_Uuid.AsString())
    # Shared supply/ground buses are not whole-channel templates. Copying all
    # of their copper would duplicate board-wide routes outside the channel.
    outside={p.GetNetCode() for p in board.GetPads() if p.m_Uuid.AsString() not in source_pads}
    excluded=sorted(board.FindNet(a).GetNetname() for a in net_map if a>0 and a in outside)
    net_map={a:b for a,b in net_map.items() if a>0 and a not in outside}
    if not net_map:
        raise ValueError('no source nets are confined to the mapped channel')
    if len(set(net_map.values()))!=len(net_map):
        raise ValueError('distinct source nets cannot merge into one destination net')
    return net_map,origin,destination,angle,excluded


def _signature(item):
    from .ai.board_copy import track_signature
    # This includes net, copper geometry, layers and widths, but not the UUID.
    signature = track_signature(item)
    return signature[:8] + signature[9:]


def replicate_channel(board, reference_map, output_directory):
    """Return a report and publish an accepted copy in a NEW output directory.

    Pad numbers infer net mapping; all mapped pads must fit one rigid transform.
    Only nets entirely contained in the source references are copied. Shared
    buses are excluded. Source copper and target copper are both preserved.
    DRC errors and per-net open counts must not increase. Requires kicad-cli.
    """
    import pcbnew
    from .ai import board_copy, context
    from .compat import zone_filler
    output=Path(output_directory).resolve()
    if output.exists():
        raise ValueError('output_directory must not already exist')
    net_map,origin,destination,angle,excluded=_plan(board,reference_map)
    source=[t for t in board.GetTracks() if t.GetNetCode() in net_map]
    if not source:
        raise ValueError('source channel has no routed copper')
    if any(t.GetParentGroup() is not None for t in source):
        raise ValueError('grouped/generated source copper must be handled as a group; not supported')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.channel-template-',dir=str(output.parent)) as folder:
        work=Path(folder)
        path=board_copy.snapshot(board,work/'input'/'board.kicad_pcb')
        candidate=board_copy.load_copy(path,board)
        protected=board_copy.protected_state(candidate)
        before=context.drc_summary(candidate,work/'before')
        if not before.get('available') or not before.get('unconnected_by_net_complete'):
            raise RuntimeError('baseline DRC/connectivity unavailable: '+str(before.get('error','')))
        existing={_signature(t) for t in candidate.GetTracks()}
        added=[];duplicates=0
        for item in source:
            duplicate=item.Duplicate()
            duplicate.Rotate(origin,pcbnew.EDA_ANGLE(angle,pcbnew.DEGREES_T))
            duplicate.Move(destination-origin)
            duplicate.SetLocked(False)
            duplicate.SetNet(candidate.FindNet(board.FindNet(net_map[item.GetNetCode()]).GetNetname()))
            signature=_signature(duplicate)
            if signature in existing:
                duplicates+=1
                continue
            candidate.Add(duplicate)
            added.append(duplicate);existing.add(signature)
        if board_copy.protected_state(candidate)!=protected:
            raise RuntimeError('replication changed protected board state')
        zone_filler(candidate).Fill(candidate.Zones())
        candidate.BuildConnectivity()
        after=context.drc_summary(candidate,work/'after')
        if not after.get('available') or not after.get('unconnected_by_net_complete'):
            raise RuntimeError('candidate DRC/connectivity unavailable: '+str(after.get('error','')))
        if Counter(after['violation_identities'])-Counter(before['violation_identities']):
            raise ValueError('replicated channel introduces new DRC errors')
        if any(count>before['unconnected_by_net'].get(net,0) for net,count in after['unconnected_by_net'].items()):
            raise ValueError('replicated channel regresses per-net connectivity')
        report={'reference_map':reference_map,'net_map':{board.FindNet(a).GetNetname():board.FindNet(b).GetNetname() for a,b in net_map.items()},
                'added':len(added),'duplicates_skipped':duplicates,'excluded_shared_nets':excluded,
                'before_opens':before['unconnected_by_net'],'after_opens':after['unconnected_by_net'],
                'board':str(output/'board.kicad_pcb')}
        accepted=work/'accepted'
        board_copy.snapshot(candidate,accepted/'board.kicad_pcb')
        (accepted/'replication.json').write_text(json.dumps(report,indent=2)+'\n')
        # The result is published only after validation; failures leave no
        # partially accepted output project and never touch the source board.
        os.rename(accepted,output)
    return report
