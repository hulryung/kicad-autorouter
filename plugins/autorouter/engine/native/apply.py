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


def _point_to_segment(x: int, y: int, x1: int, y1: int, x2: int, y2: int) -> float:
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(x - x1, y - y1)
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length2))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


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


def _via_radius(via) -> int:
    try:
        return via.GetWidth(pcbnew.F_Cu) // 2            # KiCad 9+: the width is per layer
    except TypeError:
        return via.GetWidth() // 2


def prune_redundant_vias(board, items, within_nm: int, log=None) -> int:
    """After a run: a via the run dropped comes off again when the net's
    copper around it (tracks, pads and the filled pours on their layers)
    stays in one piece without it and that piece holds another via of the
    net within `within_nm`. Two neighbouring pads of a power net each got a
    via before the signals were routed; once a track or the pour on their
    layer joins the pads, one via serves both, as a designer draws a
    decoupling cap and the pin it feeds. The stub a removed via leaves
    behind is taken off too, back to where it meets other copper. The
    zones are refilled first so the pours stand as they will on the
    finished board. Returns the number of vias removed."""
    if within_nm <= 0 or not items:
        return 0
    uid = lambda item: str(item.m_Uuid.AsString())
    live = {uid(t): t for t in items if t.GetClass() in ("PCB_TRACK", "PCB_VIA")}
    if not any(t.GetClass() == "PCB_VIA" for t in live.values()):
        return 0
    try:
        zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0]
        if zones:
            pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    except Exception:
        zones = []

    def _touch(item, x: int, y: int, slack: int) -> bool:
        """Is the point within the item's copper (plus slack)?"""
        cls = item.GetClass()
        if cls == "PCB_TRACK":
            s, e = item.GetStart(), item.GetEnd()
            return _point_to_segment(x, y, s.x, s.y, e.x, e.y) <= item.GetWidth() // 2 + slack
        if cls == "PCB_VIA":
            p = item.GetPosition()
            return math.hypot(p.x - x, p.y - y) <= _via_radius(item) + slack
        if cls == "PAD":
            try:
                return bool(item.HitTest(pcbnew.VECTOR2I(x, y)))
            except Exception:
                return False
        return False

    def _anchors(item):
        if item.GetClass() == "PCB_TRACK":
            s, e = item.GetStart(), item.GetEnd()
            return [(s.x, s.y), (e.x, e.y)]
        p = item.GetPosition()
        return [(p.x, p.y)]

    def _layers(item):
        cls = item.GetClass()
        if cls == "PCB_TRACK":
            return {item.GetLayer()}
        if cls == "PCB_VIA":
            return None                               # a through via: every layer
        try:
            return set(item.GetLayerSet().CuStack())
        except Exception:
            return None

    graphs = {}

    def _graph(net: int):
        """Nodes: the net's tracks, vias and pads (by UUID) and each filled
        pour layer of the net (by zone UUID and layer); edges where copper
        touches, from the geometry (KiCad's connectivity hands back whole
        clusters, not neighbours)."""
        if net in graphs:
            return graphs[net]
        copper = [(uid(t), t) for t in board.GetTracks() if t.GetNetCode() == net]
        copper += [(uid(p), p) for p in board.GetPads() if p.GetNetCode() == net]
        adj = {k: [] for k, _ in copper}
        for i, (ka, a) in enumerate(copper):
            la = _layers(a)
            for kb, b in copper[i + 1:]:
                lb = _layers(b)
                if la is not None and lb is not None and not (la & lb):
                    continue
                if a.GetClass() == "PAD" and b.GetClass() == "PAD":
                    continue
                if any(_touch(b, x, y, 1000) for x, y in _anchors(a)) or \
                        any(_touch(a, x, y, 1000) for x, y in _anchors(b)):
                    adj[ka].append((kb, b))
                    adj[kb].append((ka, a))
        for z in zones:
            if z.GetNetCode() != net:
                continue
            for layer in z.GetLayerSet().CuStack():
                try:
                    fill = z.GetFilledPolysList(layer)
                    if fill.OutlineCount() == 0:
                        continue
                except Exception:
                    continue
                zk = (uid(z), int(layer))
                adj[zk] = []
                for ka, a in copper:
                    la = _layers(a)
                    if la is not None and layer not in la:
                        continue
                    if a.GetClass() == "PCB_TRACK":
                        hit = any(fill.Contains(pcbnew.VECTOR2I(x, y)) for x, y in _anchors(a))
                    else:
                        # a pad or via with a thermal relief: its centre is in
                        # the gap, the spokes touch its edge
                        try:
                            hit = bool(fill.Collide(a.GetEffectiveShape(layer), 1000))
                        except Exception:
                            hit = any(fill.Contains(pcbnew.VECTOR2I(x, y)) for x, y in _anchors(a))
                    if hit:
                        adj[ka].append((zk, None))
                        adj[zk].append((ka, a))
        graphs[net] = adj
        return adj

    def _near(a, b) -> bool:
        pa, pb = a.GetPosition(), b.GetPosition()
        return abs(pa.x - pb.x) <= within_nm and abs(pa.y - pb.y) <= within_nm \
            and (pa.x - pb.x) ** 2 + (pa.y - pb.y) ** 2 <= within_nm * within_nm

    gone: set = set()
    removed = 0
    # by position: deterministic, and of a pair the one farther down the
    # board goes when either would do
    vias = sorted((v for v in live.values() if v.GetClass() == "PCB_VIA"),
                  key=lambda v: (v.GetNetCode(), v.GetPosition().y, v.GetPosition().x))
    for via in vias:
        vk = uid(via)
        adj = _graph(via.GetNetCode())
        held = [(k, o) for k, o in adj.get(vk, []) if o is not None and k not in gone]
        if not held:
            if log is not None:
                log.append((via.GetNetCode(), via.GetPosition().x, via.GetPosition().y, "lone"))
            continue                                      # a stitching via: placed on purpose
        # the piece of the net the via's copper falls into without the via
        seen = {vk, held[0][0]}
        queue = [held[0][0]]
        found = False
        while queue:
            k = queue.pop(0)
            for nk, o in adj.get(k, []):
                if nk in gone or nk in seen:
                    continue
                seen.add(nk)
                queue.append(nk)
                if o is not None and o.GetClass() == "PCB_VIA" and _near(o, via):
                    found = True
        split = any(k not in seen for k, _ in held)
        if log is not None:
            log.append((via.GetNetCode(), via.GetPosition().x, via.GetPosition().y,
                        "bridge" if split else ("removed" if found else "kept"), [o.GetClass() for _, o in held]))
        if split or not found:
            continue
        gone.add(vk)
        board.Remove(via)
        via.thisown = 0
        _REMOVED.append(via)
        removed += 1
        # the stub it leaves: the run's own tracks with an end in the air now
        while True:
            loose = None
            for k, _ in held:
                if k in gone or k not in live or live[k].GetClass() != "PCB_TRACK":
                    continue
                t = live[k]
                for x, y in _anchors(t):
                    if not any(o is not None and nk not in gone and _touch(o, x, y, 1000)
                               for nk, o in adj.get(k, [])):
                        loose = k
                        break
                if loose:
                    break
            if loose is None:
                break
            t = live[loose]
            gone.add(loose)
            board.Remove(t)
            t.thisown = 0
            _REMOVED.append(t)
            held = [(k, o) for k, o in adj.get(loose, []) if o is not None and k not in gone]
    if removed:
        try:
            board.BuildConnectivity()
        except Exception:
            pass
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
