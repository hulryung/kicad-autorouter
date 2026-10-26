"""Putting routed paths on the board as tracks and vias (main thread only)."""
import math
from collections import Counter, defaultdict
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


WELD_TOL = 2000          # nm: a point this close to a centre line counts as on it


def _on_centre_line(x1, y1, x2, y2, px, py, tol=WELD_TOL):
    """Parameter u in (0, 1) where (px, py) lies on the open segment, or None."""
    if (px, py) in ((x1, y1), (x2, y2)):
        return None
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return None
    u = ((px - x1) * dx + (py - y1) * dy) / length2
    if u <= 1e-9 or u >= 1 - 1e-9:
        return None
    if math.hypot(x1 + u * dx - px, y1 + u * dy - py) > tol:
        return None
    return u


def weld_junctions(board, items) -> dict:
    """After a run (and after trim_overlaps): make every junction of the run's
    copper a shared end point, the way KiCad's own router leaves it. The
    router ends a route on any grid cell of the copper it joins, so a route
    can stop inside a via off its centre or on the side of another track,
    and trimming leaves ends in the middle of the copper that covers them.
    KiCad counts those as connected (copper overlaps), but Select Connection
    (U) and the interactive router walk end points and stop there. This:
      1. adds a stub from a run track's end inside a via to the via centre;
      2. adds a stub from a free run track end lying on another track's
         copper to that track's centre line;
      3. splits every track of the run's nets where another end of the same
         net and layer lies on its centre line (T-junctions);
      4. merges collinear run segments joined end to end (same width, no
         via or pad there) and drops zero-length ones.
    Connectivity is unchanged. Returns counts of what was done."""
    uid = lambda item: str(item.m_Uuid.AsString())
    run = {uid(t) for t in items if t.GetClass() == "PCB_TRACK"}
    nets = {t.GetNetCode() for t in items if t.GetNetCode() > 0}
    stats = Counter()
    if not run or not nets:
        return dict(stats)
    vias = defaultdict(list)                  # net -> [(x, y, radius)]
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA" and t.GetNetCode() in nets:
            p = t.GetPosition()
            vias[t.GetNetCode()].append((p.x, p.y, t.GetWidth(pcbnew.F_Cu) / 2))
    via_pts = {n: {(x, y) for x, y, _ in lst} for n, lst in vias.items()}
    pads = defaultdict(list)                  # net -> pads, for "end inside a pad"
    pad_pts = defaultdict(set)
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() in nets:
                pads[pad.GetNetCode()].append(pad)
                c = pad.GetPosition()
                pad_pts[pad.GetNetCode()].add((c.x, c.y))

    def tracks():
        return [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetCode() in nets]

    def add(net, layer, width, x1, y1, x2, y2):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(int(x1), int(y1)))
        t.SetEnd(pcbnew.VECTOR2I(int(x2), int(y2)))
        t.SetWidth(width)
        t.SetLayer(layer)
        t.SetNetCode(net)
        board.Add(t)
        run.add(uid(t))
        return t

    def remove(t):
        board.Remove(t)
        t.thisown = 0                         # Python must never free a board item
        _REMOVED.append(t)

    def in_pad(net, layer, x, y):
        p = pcbnew.VECTOR2I(int(x), int(y))
        return any(pad.IsOnLayer(layer) and pad.HitTest(p) for pad in pads[net])

    # 1 + 2: stubs, from a snapshot of the geometry (the board is only added to)
    snap = [(uid(t), t.GetNetCode(), t.GetLayer(), t.GetWidth(),
             t.GetStart().x, t.GetStart().y, t.GetEnd().x, t.GetEnd().y) for t in tracks()]
    ends = Counter()
    by_layer = defaultdict(list)
    for u, net, layer, w, x1, y1, x2, y2 in snap:
        ends[(net, layer, x1, y1)] += 1
        ends[(net, layer, x2, y2)] += 1
        by_layer[(net, layer)].append((u, w, x1, y1, x2, y2))
    for u, net, layer, w, x1, y1, x2, y2 in snap:
        if u not in run:
            continue
        for px, py in ((x1, y1), (x2, y2)):
            if (px, py) in via_pts.get(net, ()):
                continue
            via = next(((vx, vy) for vx, vy, r in vias.get(net, ()) if math.hypot(vx - px, vy - py) < r), None)
            if via is not None:
                add(net, layer, w, px, py, via[0], via[1])
                stats["via_stubs"] += 1
                continue
            if ends[(net, layer, px, py)] > 1 or (px, py) in pad_pts.get(net, ()) or in_pad(net, layer, px, py):
                continue
            for hu, hw, hx1, hy1, hx2, hy2 in by_layer[(net, layer)]:
                if hu == u:
                    continue
                dx, dy = hx2 - hx1, hy2 - hy1
                length2 = dx * dx + dy * dy
                if length2 == 0:
                    continue
                k = ((px - hx1) * dx + (py - hy1) * dy) / length2
                if k <= 1e-6 or k >= 1 - 1e-6:
                    continue
                cx, cy = int(round(hx1 + k * dx)), int(round(hy1 + k * dy))
                d = math.hypot(cx - px, cy - py)
                if WELD_TOL < d <= hw / 2:
                    add(net, layer, min(w, hw), px, py, cx, cy)
                    stats["centre_stubs"] += 1
                    break
    # 3: split hosts at the ends that lie on their centre lines
    groups = defaultdict(list)
    for t in tracks():
        groups[(t.GetNetCode(), t.GetLayer())].append(t)
    for (net, layer), lst in groups.items():
        points = set()
        for t in lst:
            points.add((t.GetStart().x, t.GetStart().y))
            points.add((t.GetEnd().x, t.GetEnd().y))
        for t in lst:
            s, e = t.GetStart(), t.GetEnd()
            cuts = sorted((u, p) for p in points for u in [_on_centre_line(s.x, s.y, e.x, e.y, p[0], p[1])]
                          if u is not None)
            if not cuts:
                continue
            chain = [(s.x, s.y)] + [p for _, p in cuts] + [(e.x, e.y)]
            was_run = uid(t) in run
            t.SetEnd(pcbnew.VECTOR2I(chain[1][0], chain[1][1]))
            for a, b in zip(chain[1:], chain[2:]):
                piece = add(net, layer, t.GetWidth(), a[0], a[1], b[0], b[1])
                if not was_run:
                    run.discard(uid(piece))       # a split of existing copper stays existing copper
            stats["t_splits"] += len(cuts)
    # 4: merge collinear run segments, drop zero-length ones
    def collinear(a, b):
        ax, ay = a.GetEnd().x - a.GetStart().x, a.GetEnd().y - a.GetStart().y
        bx, by = b.GetEnd().x - b.GetStart().x, b.GetEnd().y - b.GetStart().y
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        return la > 0 and lb > 0 and abs(ax * by - ay * bx) / (la * lb) < 1e-4
    merged = True
    while merged:
        merged = False
        at = defaultdict(list)
        for t in tracks():
            for p in (t.GetStart(), t.GetEnd()):
                at[(t.GetNetCode(), t.GetLayer(), p.x, p.y)].append(t)
        for (net, layer, x, y), lst in at.items():
            if len(lst) != 2 or (x, y) in via_pts.get(net, ()) or (x, y) in pad_pts.get(net, ()):
                continue
            a, b = lst
            if uid(a) == uid(b) or uid(a) not in run or uid(b) not in run:
                continue
            if a.GetWidth() != b.GetWidth() or not collinear(a, b) or in_pad(net, layer, x, y):
                continue
            fa = a.GetEnd() if (a.GetStart().x, a.GetStart().y) == (x, y) else a.GetStart()
            fb = b.GetEnd() if (b.GetStart().x, b.GetStart().y) == (x, y) else b.GetStart()
            a.SetStart(pcbnew.VECTOR2I(fa.x, fa.y))
            a.SetEnd(pcbnew.VECTOR2I(fb.x, fb.y))
            remove(b)
            stats["merged"] += 1
            merged = True
            break
    for t in tracks():
        if uid(t) in run and (t.GetStart().x, t.GetStart().y) == (t.GetEnd().x, t.GetEnd().y):
            remove(t)
            stats["zero_length"] += 1
    return dict(stats)


_REMOVED: list = []        # items taken off the board; Python must not free them


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
