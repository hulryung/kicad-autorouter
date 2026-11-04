"""Putting routed paths on the board as tracks and vias (main thread only)."""
import math
from typing import Iterable, List

import pcbnew

from .problem import LayerInfo, RoutedPath


def _uncovered(tracks, x1: int, y1: int, x2: int, y2: int, width: int):
    """The parts of a segment not already covered by a collinear track among
    `tracks` (same net and layer, at least as wide), as (x1, y1, x2, y2)
    pieces. Two routes of one net share pad stubs and may ride along each
    other; writing the shared copper once keeps the artwork clean."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1:
        return []
    ux, uy = dx / length, dy / length
    covered = []
    for t in tracks:
        if t.GetWidth() < width:
            continue
        s, e = t.GetStart(), t.GetEnd()
        # collinear: both ends within a micrometre of this segment's line
        if abs((s.x - x1) * uy - (s.y - y1) * ux) > 1000 or abs((e.x - x1) * uy - (e.y - y1) * ux) > 1000:
            continue
        a = (s.x - x1) * ux + (s.y - y1) * uy
        b = (e.x - x1) * ux + (e.y - y1) * uy
        lo, hi = max(0.0, min(a, b)), min(length, max(a, b))
        if hi - lo > 1:
            covered.append((lo, hi))
    if not covered:
        return [(x1, y1, x2, y2)]
    covered.sort()
    pieces, at = [], 0.0
    for lo, hi in covered:
        if lo > at + 1:
            pieces.append((at, lo))
        at = max(at, hi)
    if length > at + 1:
        pieces.append((at, length))
    out = []
    for a, b in pieces:
        out.append((int(round(x1 + ux * a)), int(round(y1 + uy * a)), int(round(x1 + ux * b)), int(round(y1 + uy * b))))
    return [(ax, ay, bx, by) for ax, ay, bx, by in out if (ax, ay) != (bx, by)]


def _same_copper(board, net: int, layer_id: int):
    return [t for t in board.GetTracks()
            if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == net and t.GetLayer() == layer_id]


def trim_overlaps(board, items) -> int:
    """After a run: among the run's new segments, copper a same-net track on
    the same layer already covers (a pad stub two routes share, a run along
    another route of the net) is written once. Done at the end rather than
    as each path is added, so a path ripped up later in the run (shove, the
    second pass) cannot leave a segment that was trimmed against it hanging
    in the air. Returns how many segments were removed or shortened."""
    # SWIG hands out a fresh wrapper per access, so items are told apart by UUID
    uid = lambda item: str(item.m_Uuid.AsString())
    new = [t for t in items if t.GetClass() == "PCB_TRACK"]
    by_key = {}
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK":
            by_key.setdefault((t.GetNetCode(), t.GetLayer()), []).append(t)
    changed = 0
    for t in new:
        key = (t.GetNetCode(), t.GetLayer())
        me = uid(t)
        others = [o for o in by_key.get(key, []) if uid(o) != me]
        s, e = t.GetStart(), t.GetEnd()
        pieces = _uncovered(others, s.x, s.y, e.x, e.y, t.GetWidth())
        if pieces == [(s.x, s.y, e.x, e.y)]:
            continue
        changed += 1
        by_key[key] = others
        if not pieces:
            board.Remove(t)
            t.thisown = 0
            _REMOVED.append(t)
            continue
        t.SetStart(pcbnew.VECTOR2I(pieces[0][0], pieces[0][1]))
        t.SetEnd(pcbnew.VECTOR2I(pieces[0][2], pieces[0][3]))
        by_key[key].append(t)
        for x1, y1, x2, y2 in pieces[1:]:
            extra = pcbnew.PCB_TRACK(board)
            extra.SetStart(pcbnew.VECTOR2I(x1, y1))
            extra.SetEnd(pcbnew.VECTOR2I(x2, y2))
            extra.SetWidth(t.GetWidth())
            extra.SetLayer(t.GetLayer())
            extra.SetNetCode(t.GetNetCode())
            board.Add(extra)
            by_key[key].append(extra)
    return changed


_REMOVED: list = []        # items taken off the board; Python must not free them


def _collide(a, b, layer) -> bool:
    try:
        return bool(a.GetEffectiveShape(layer).Collide(b.GetEffectiveShape(layer), 0))
    except Exception:
        return True                                # unsure: count it as a contact (keeps the via)


def _via_layers(via, tracks, pads, zones, layers) -> int:
    """On how many copper layers the via touches other copper of its net
    (tracks, arcs and other vias, pads, zone fills)."""
    pos = via.GetPosition()
    box = via.GetBoundingBox()
    uid = str(via.m_Uuid.AsString())
    touched = 0
    for layer in layers:
        if not via.IsOnLayer(layer):
            continue
        hit = False
        for t in tracks:
            if t.GetClass() == "PCB_VIA":
                if str(t.m_Uuid.AsString()) != uid and t.IsOnLayer(layer) \
                        and t.GetBoundingBox().Intersects(box) and _collide(via, t, layer):
                    hit = True
            elif t.GetLayer() == layer and t.GetBoundingBox().Intersects(box) and _collide(via, t, layer):
                hit = True
            if hit:
                break
        if not hit:
            hit = any(p.IsOnLayer(layer) and p.GetBoundingBox().Intersects(box) and _collide(via, p, layer)
                      for p in pads)
        if not hit:
            for z in zones:
                if not z.IsOnLayer(layer):
                    continue
                try:
                    filled = z.GetFilledPolysList(layer)
                    if filled.OutlineCount() and filled.Collide(via.GetEffectiveShape(layer), 0):
                        hit = True
                        break
                except Exception:
                    hit = True
                    break
        if hit:
            touched += 1
            if touched >= 2:
                break
    return touched


def prune_dangling_vias(board, items) -> int:
    """After a job: a via of the job's own that touches copper of its net on
    one layer only (a plane-net pad via that missed the plane's fill, a
    fanout via a route made unnecessary) joins nothing; KiCad flags it as
    dangling. It comes off the board, and so does any of the job's tracks
    it leaves ending in the air (the stub that led to it). Removing such a
    via never breaks a connection. The pours are refilled first when a
    candidate turns up, so the check sees the copper DRC will see. Returns
    how many vias were removed."""
    vias = [t for t in items if t.GetClass() == "PCB_VIA"]
    if not vias:
        return 0
    copper = [l for l in board.GetEnabledLayers().CuStack()]
    nets = {v.GetNetCode() for v in vias}
    by_net_tracks, by_net_pads, by_net_zones = {}, {}, {}

    def collect():
        by_net_tracks.clear(), by_net_pads.clear(), by_net_zones.clear()
        for t in board.GetTracks():
            if t.GetNetCode() in nets:
                by_net_tracks.setdefault(t.GetNetCode(), []).append(t)
        for fp in board.GetFootprints():
            for p in fp.Pads():
                if p.GetNetCode() in nets:
                    by_net_pads.setdefault(p.GetNetCode(), []).append(p)
        for z in board.Zones():
            if not z.GetIsRuleArea() and z.GetNetCode() in nets:
                by_net_zones.setdefault(z.GetNetCode(), []).append(z)

    def lonely(cands):
        return [v for v in cands
                if _via_layers(v, by_net_tracks.get(v.GetNetCode(), []), by_net_pads.get(v.GetNetCode(), []),
                               by_net_zones.get(v.GetNetCode(), []), copper) < 2]

    collect()
    cands = lonely(vias)
    if not cands:
        return 0
    try:
        if any(not z.GetIsRuleArea() for z in board.Zones()):
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        return 0                                   # no fresh fills: leave the vias
    collect()
    cands = lonely(cands)
    own = {str(t.m_Uuid.AsString()) for t in items if t.GetClass() in ("PCB_TRACK", "PCB_ARC")}
    removed = 0
    for via in cands:
        net = via.GetNetCode()
        uid = str(via.m_Uuid.AsString())
        pos = via.GetPosition()
        r = via.GetBoundingBox().GetWidth() // 2
        # the job's tracks with an end inside the via: once it is gone, an end
        # touching nothing else (no other track, via or pad) is a stub
        stubs = []
        for t in by_net_tracks.get(net, []):
            if str(t.m_Uuid.AsString()) not in own or t.GetClass() != "PCB_TRACK":
                continue
            for end in (t.GetStart(), t.GetEnd()):
                if math.hypot(end.x - pos.x, end.y - pos.y) <= r:
                    stubs.append((t, end))
                    break
        board.Remove(via)
        via.thisown = 0
        _REMOVED.append(via)
        by_net_tracks[net] = [t for t in by_net_tracks.get(net, []) if str(t.m_Uuid.AsString()) != uid]
        removed += 1
        for t, end in stubs:
            tid = str(t.m_Uuid.AsString())
            layer = t.GetLayer()
            pt = pcbnew.VECTOR2I(end.x, end.y)
            reach = t.GetWidth() // 2
            joined = False
            for o in by_net_tracks.get(net, []):
                if str(o.m_Uuid.AsString()) == tid or not o.IsOnLayer(layer):
                    continue
                try:
                    joined = bool(o.HitTest(pt, reach))
                except Exception:
                    joined = True
                if joined:
                    break
            if not joined:
                for p in by_net_pads.get(net, []):
                    try:
                        joined = bool(p.IsOnLayer(layer) and p.HitTest(pt, reach))
                    except Exception:
                        joined = True
                    if joined:
                        break
            if not joined:
                for z in by_net_zones.get(net, []):
                    try:
                        if z.IsOnLayer(layer) and z.GetFilledPolysList(layer).Contains(pt):
                            joined = True
                            break
                    except Exception:
                        joined = True
                        break
            if not joined:
                board.Remove(t)
                t.thisown = 0
                _REMOVED.append(t)
                by_net_tracks[net] = [o for o in by_net_tracks.get(net, []) if str(o.m_Uuid.AsString()) != tid]
    return removed


def add_path(board, layers: List[LayerInfo], path: RoutedPath, trim: bool = False) -> List[object]:
    """Create the items for one path and add them to the board; returns them.
    With trim, segments are cut where the board already has the same copper
    (a run does that once at its end with trim_overlaps instead)."""
    added = []
    for seg in path.segments:
        layer, x1, y1, x2, y2 = seg[:5]
        if (x1, y1) == (x2, y2):
            continue
        width = seg[5] if len(seg) > 5 else path.width
        layer_id = layers[layer].kicad_id
        pieces = (_uncovered(_same_copper(board, path.net, layer_id), x1, y1, x2, y2, width) if trim
                  else [(x1, y1, x2, y2)])
        for px1, py1, px2, py2 in pieces:
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(px1, py1))
            track.SetEnd(pcbnew.VECTOR2I(px2, py2))
            track.SetWidth(width)
            track.SetLayer(layer_id)
            track.SetNetCode(path.net)
            board.Add(track)
            added.append(track)
    top, bottom = pcbnew.F_Cu, pcbnew.B_Cu
    for x, y in path.vias:
        if any(t.GetClass() == "PCB_VIA" and t.GetNetCode() == path.net and t.GetPosition().x == x and t.GetPosition().y == y
               for t in board.GetTracks()):
            continue                                      # two routes of the net sharing one via spot
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(x, y))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetWidth(path.via_diameter)
        via.SetDrill(path.via_drill)
        via.SetLayerPair(top, bottom)
        via.SetNetCode(path.net)
        board.Add(via)
        added.append(via)
    return added


def add_paths(board, layers: List[LayerInfo], paths: Iterable[RoutedPath]) -> List[object]:
    """All paths onto the board, untrimmed; returns the items added."""
    items: List[object] = []
    for path in paths:
        items += add_path(board, layers, path)
    return items
