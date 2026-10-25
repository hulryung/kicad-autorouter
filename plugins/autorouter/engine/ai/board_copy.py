"""Isolated board copies and checked copper transfer for the PCB editor.

Do not call pcbnew.LoadBoard from the editor: it can replace the active project.
"""
import shutil
import json
from pathlib import Path

_HOLDERS = []


def snapshot(board, destination):
    import pcbnew
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(board.GetFileName()) if board.GetFileName() else None
    for suffix in ('.kicad_pro', '.kicad_dru', '.autorouter.json'):
        path = source.with_suffix(suffix) if source else None
        if path and path.exists():
            shutil.copy2(path, destination.with_suffix(suffix))
    _snapshot_rules(board, destination.with_suffix('.kicad_pro'))
    if not pcbnew.SaveBoard(str(destination), board):
        raise RuntimeError('Could not snapshot the open board')
    return destination


def _snapshot_rules(board, path):
    """Persist live routing constraints into the COPY, including unsaved edits."""
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault('meta', {'version': 1, 'filename': path.name})
    settings = board.GetDesignSettings()
    rules = data.setdefault('board', {}).setdefault('design_settings', {}).setdefault('rules', {})
    fields = {'min_clearance': 'm_MinClearance', 'min_track_width': 'm_TrackMinWidth',
              'min_via_diameter': 'm_ViasMinSize', 'min_through_hole_diameter': 'm_MinThroughDrill',
              'min_via_annular_width': 'm_ViasMinAnnularWidth', 'min_hole_clearance': 'm_HoleClearance',
              'min_hole_to_hole': 'm_HoleToHoleMin', 'min_copper_edge_clearance': 'm_CopperEdgeClearance'}
    for key, attr in fields.items():
        if hasattr(settings, attr):
            rules[key] = int(getattr(settings, attr)) / 1e6
    ns = settings.m_NetSettings
    net_settings = data.setdefault('net_settings', {})
    classes = {c['name']: c for c in net_settings.get('classes', [])}
    for name in ['Default'] + [str(n) for n in ns.GetNetclasses().keys()]:
        nc = ns.GetDefaultNetclass() if name == 'Default' else ns.GetNetClassByName(name)
        row = classes.setdefault(name, {'name': name})
        for key, attr in {'clearance': 'Clearance', 'track_width': 'TrackWidth',
                          'via_diameter': 'ViaDiameter', 'via_drill': 'ViaDrill',
                          'diff_pair_width': 'DiffPairWidth', 'diff_pair_gap': 'DiffPairGap',
                          'diff_pair_via_gap': 'DiffPairViaGap'}.items():
            has = getattr(nc, 'Has' + attr, None)
            get = getattr(nc, 'Get' + attr, None)
            if get and (has is None or has()):
                row[key] = get() / 1e6
            else:
                row.pop(key, None)
    net_settings['classes'] = list(classes.values())
    path.write_text(json.dumps(data, indent=2) + '\n')


def load_copy(path, template):
    import pcbnew
    board = pcbnew.PCB_IO_MGR.Load(pcbnew.PCB_IO_MGR.KICAD_SEXP, str(path))
    if board is None:
        raise RuntimeError('Could not load candidate board')
    board.SetFileName(str(path))
    # Reference only: this board neither owns nor replaces the editor's project.
    board.SetProject(template.GetProject(), True)
    board.SetDesignSettings(template.GetDesignSettings())
    return board


def uid(item):
    return str(item.m_Uuid.AsString())


def track_signature(item):
    result = [item.GetClass(), item.GetNetname(), item.GetWidth(), item.GetLayer(),
              item.GetStart().x, item.GetStart().y, item.GetEnd().x, item.GetEnd().y, item.IsLocked()]
    if item.GetClass() == 'PCB_VIA':
        result += [item.GetDrillValue(), item.TopLayer(), item.BottomLayer()]
    elif item.GetClass() == 'PCB_ARC':
        result += [item.GetMid().x, item.GetMid().y]
    return tuple(result)


def protected_state(board):
    """Routing may not change placement, pad assignments or locked copper."""
    footprints = {}
    for fp in board.GetFootprints():
        footprints[uid(fp)] = (fp.GetReference(), fp.GetPosition().x, fp.GetPosition().y,
                               round(fp.GetOrientationDegrees(), 6), fp.GetLayer(),
                               tuple(sorted((uid(p), p.GetNumber(), p.GetNetname()) for p in fp.Pads())))
    return {'footprints': footprints,
            'locked': {uid(t): track_signature(t) for t in board.GetTracks() if t.IsLocked()},
            'zones': sorted(uid(z) for z in board.Zones())}


def _groups(board):
    # Generators() exposes opaque pointers in KiCad's SWIG bindings. A tuning
    # pattern is accessible through its copper member's PCB_GROUP parent.
    import pcbnew
    groups = {uid(g): g for g in board.Groups()}
    for track in board.GetTracks():
        group = track.GetParentGroup()
        while group is not None:
            if not hasattr(group, "m_Uuid"):
                group = pcbnew.Cast_to_PCB_GROUP(pcbnew.Cast_to_BOARD_ITEM(group.AsEdaItem()))
            groups[uid(group)] = group
            group = group.GetParentGroup()
    return list(groups.values())


def apply_copper(board, path):
    """Transfer accepted tracks/vias/arcs and their groups, preserving the board.

    The caller is inside an action plugin, so KiCad captures these mutations
    in its usual undo record. Keep detached objects alive until KiCad owns them.
    """
    import pcbnew
    from .. import session
    holder = load_copy(path, board)
    if protected_state(board) != protected_state(holder):
        raise ValueError('Accepted result changed placement, pad nets, zones or locked copper')
    tracks = list(holder.GetTracks())
    groups = _groups(holder)
    members = {uid(g): [uid(m) for m in pcbnew.Cast_to_PCB_GROUP(g).GetItems()] for g in groups}
    target = {uid(t): t for t in tracks}
    target.update({uid(g): g for g in groups})
    # Validate all cross-references before touching the editor's board.
    for group_members in members.values():
        for member in group_members:
            if member not in target:
                item = board.ResolveItem(pcbnew.KIID(member))
                if item is None:
                    raise ValueError(f'Group references missing item {member}')
                target[member] = item
    nets = {}
    for track in tracks:
        name = track.GetNetname()
        net = board.FindNet(name) if name else None
        if name and (net is None or net.GetNetCode() <= 0):
            raise ValueError(f'Accepted result contains unknown net {name}')
        nets[uid(track)] = net
    old_groups = _groups(board)
    old_tracks = list(board.GetTracks())
    for g in groups:
        group = pcbnew.Cast_to_PCB_GROUP(g)
        for member in list(group.GetItems()):
            group.RemoveItem(member)
    for g in old_groups:
        board.Remove(g)
        g.thisown = 0
    for track in old_tracks:
        board.Remove(track)
        track.thisown = 0
    for track in tracks:
        holder.Remove(track)
        track.thisown = 0
        track.SetNet(nets[uid(track)])
        board.Add(track)
    for g in groups:
        holder.Remove(g)
        g.thisown = 0
        board.Add(g)
    for g in groups:
        group = pcbnew.Cast_to_PCB_GROUP(g)
        for member in members[uid(g)]:
            group.AddItem(target[member])
    _HOLDERS.append((holder, old_tracks, old_groups))
    board.BuildConnectivity()
    session.refill_zones(board)
    return len(tracks)
