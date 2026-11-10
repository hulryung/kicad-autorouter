"""Putting routed paths on the board as tracks and vias (main thread only)."""
import math
import os
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
            items.append(extra)                # still the run's own copper for the later clean-up
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
        if isinstance(items, list):
            items.append(t)                    # the run's copper, for the clean-up after this
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
    stubbed = set()                           # (net, layer, point): one stub per end point
    joins = set()                             # (net, layer, end, end) of every segment already there
    for u, net, layer, w, x1, y1, x2, y2 in snap:
        joins.add((net, layer, (x1, y1), (x2, y2)))
        joins.add((net, layer, (x2, y2), (x1, y1)))
    for u, net, layer, w, x1, y1, x2, y2 in snap:
        if u not in run:
            continue
        for px, py in ((x1, y1), (x2, y2)):
            if (px, py) in via_pts.get(net, ()) or (net, layer, px, py) in stubbed:
                continue
            via = next(((vx, vy) for vx, vy, r in vias.get(net, ()) if math.hypot(vx - px, vy - py) < r), None)
            if via is not None:
                stubbed.add((net, layer, px, py))
                if (net, layer, (px, py), via) in joins:
                    continue                  # a segment already runs from here to the centre
                add(net, layer, w, px, py, via[0], via[1])
                stats["via_stubs"] += 1
                continue
            if ends[(net, layer, px, py)] > 1 or (px, py) in pad_pts.get(net, ()) or in_pad(net, layer, px, py):
                continue
            # an end of the other track within its own copper of this point:
            # meet there, rather than on its centre line a few micrometres
            # short of that end (the split would leave the rest as a tail)
            near = [(math.hypot(ex - px, ey - py), ex, ey, hw) for hu, hw, hx1, hy1, hx2, hy2 in by_layer[(net, layer)]
                    if hu != u for ex, ey in ((hx1, hy1), (hx2, hy2))
                    if (hx1, hy1) != (hx2, hy2) and math.hypot(ex - px, ey - py) <= hw / 2]
            if near:
                d, ex, ey, hw = min(near)
                stubbed.add((net, layer, px, py))
                if d > 0 and (net, layer, (px, py), (ex, ey)) not in joins:
                    add(net, layer, min(w, hw), px, py, ex, ey)
                    stats["end_stubs"] += 1
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
                    stubbed.add((net, layer, px, py))
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
                    if isinstance(items, list):
                        items.remove(piece)
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
    # 5: tails. A route ends on a cell of the copper it joins, which can lie
    # past the point where the two centre lines meet: after the split there
    # a short piece of the run is left with one end free, all of it inside
    # the other track's copper. It joins nothing (KiCad flags it dangling).
    def inside(host, x, y):
        s, e = host.GetStart(), host.GetEnd()
        dx, dy = e.x - s.x, e.y - s.y
        length2 = dx * dx + dy * dy
        k = max(0.0, min(1.0, ((x - s.x) * dx + (y - s.y) * dy) / length2)) if length2 else 0.0
        return math.hypot(s.x + k * dx - x, s.y + k * dy - y) <= host.GetWidth() / 2 - WELD_TOL
    trimmed = True
    while trimmed:
        trimmed = False
        ends = Counter()
        groups = defaultdict(list)
        for t in tracks():
            groups[(t.GetNetCode(), t.GetLayer())].append(t)
            for p in (t.GetStart(), t.GetEnd()):
                ends[(t.GetNetCode(), t.GetLayer(), p.x, p.y)] += 1
        for (net, layer), lst in groups.items():
            for t in lst:
                if uid(t) not in run:
                    continue
                pts = [(t.GetStart().x, t.GetStart().y), (t.GetEnd().x, t.GetEnd().y)]
                free = [p for p in pts if ends[(net, layer, p[0], p[1])] == 1]
                if len(free) != 1:
                    continue
                fx, fy = free[0]
                if (fx, fy) in via_pts.get(net, ()) or (fx, fy) in pad_pts.get(net, ()) or in_pad(net, layer, fx, fy) \
                        or any(math.hypot(vx - fx, vy - fy) < r for vx, vy, r in vias.get(net, ())):
                    continue
                if any(uid(h) != uid(t)
                       and all(inside(h, x, y) for x, y in pts) for h in lst):
                    remove(t)
                    stats["tails"] += 1
                    trimmed = True
            if trimmed:
                break
    return dict(stats)


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
    it leaves ending in the air (the stub that led to it). A via whose copper
    is the only join between two pieces on its one layer (a pad stub ending
    at its centre, a route starting at its rim) stays: removing it, and the
    route as "its stub", would leave the pad open. The pours are refilled first when a
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

    def bridges(via) -> bool:
        """The via's copper is what joins two pieces of its net on one layer
        (a pad stub ending at its centre and a route starting at its rim):
        it does connect something, even on a single layer."""
        net = via.GetNetCode()
        items = []
        for t in by_net_tracks.get(net, []):
            if t.GetClass() == "PCB_TRACK" and _collide(via, t, t.GetLayer()):
                items.append((t, t.GetLayer()))
        for p in by_net_pads.get(net, []):
            for layer in copper:
                if p.IsOnLayer(layer) and _collide(via, p, layer):
                    items.append((p, layer))
                    break
        if len(items) < 2:
            return False
        root = list(range(len(items)))

        def find(i):
            while root[i] != i:
                root[i] = root[root[i]]
                i = root[i]
            return i
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (a, la), (b, lb) = items[i], items[j]
                if b.GetClass() == "PCB_TRACK" and la != lb:
                    continue
                layer = la if a.GetClass() == "PCB_TRACK" else lb
                if not (a.IsOnLayer(layer) and b.IsOnLayer(layer)):
                    continue
                if _collide(a, b, layer):
                    root[find(i)] = find(j)
        return len({find(i) for i in range(len(items))}) > 1

    for via in cands:
        net = via.GetNetCode()
        uid = str(via.m_Uuid.AsString())
        pos = via.GetPosition()
        r = via.GetBoundingBox().GetWidth() // 2
        if bridges(via):
            # the via's copper is a join on one layer only, which KiCad
            # still flags as dangling: a short track from its centre to
            # each track end inside its rim takes its place
            added = []
            for t in by_net_tracks.get(net, []):
                if t.GetClass() != "PCB_TRACK":
                    continue
                for end in (t.GetStart(), t.GetEnd()):
                    d = math.hypot(end.x - pos.x, end.y - pos.y)
                    if 1_000 < d <= r:
                        seg = pcbnew.PCB_TRACK(board)
                        seg.SetStart(pcbnew.VECTOR2I(pos.x, pos.y))
                        seg.SetEnd(pcbnew.VECTOR2I(end.x, end.y))
                        seg.SetLayer(t.GetLayer())
                        seg.SetWidth(t.GetWidth())
                        seg.SetNetCode(net)
                        added.append(seg)
            if not added:
                continue
            for seg in added:
                board.Add(seg)
                items.append(seg)
                by_net_tracks.setdefault(net, []).append(seg)
                own.add(str(seg.m_Uuid.AsString()))
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
        if os.environ.get("AUTOROUTER_DEBUG"):
            print(f"[prune] {via.GetNetname()} via at ({pos.x / 1e6:.2f}, {pos.y / 1e6:.2f})", flush=True)
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


def _is45(dx: float, dy: float) -> bool:
    a = math.degrees(math.atan2(dy, dx)) % 45
    return min(a, 45 - a) <= 0.25


_DIRS45 = [(math.cos(math.radians(45 * k)), math.sin(math.radians(45 * k))) for k in range(8)]


def _line_cross(p, d, q, e):
    """Where the line through p along d meets the line through q along e
    (None when parallel)."""
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-9:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return (p[0] + t * d[0], p[1] + t * d[1])


def snap_tiny_joins(board, items, tiny: int = 100_000, reach: int = 100_000) -> int:
    """After a run: a coupled run or a grid path that ends a few micrometres
    off the grid is joined to the next segment by a tiny piece at an odd
    angle (often turning back: an acute corner). Such a piece (with any
    other tiny pieces chained to it) is taken out, and the two 45-degree
    segments on either side are cut or stretched to meet where their lines
    cross. When one side ends at a via or pad (it cannot move), the other
    segment is cut where a short 45-degree join from that end meets it. Every
    end moves by at most `reach`, along the segment's own line. A piece lying
    wholly inside one pad of its net is simply removed. Only the run's own
    tracks are touched. Returns how many pieces were fixed."""
    uid = lambda item: str(item.m_Uuid.AsString())
    own = {uid(t) for t in items if t.GetClass() == "PCB_TRACK"}
    if not own:
        return 0
    nets = {t.GetNetCode() for t in items if t.GetClass() == "PCB_TRACK"}
    pads_by_net = {}
    for fp in board.GetFootprints():
        for p in fp.Pads():
            if p.GetNetCode() in nets:
                pads_by_net.setdefault(p.GetNetCode(), []).append(p)

    def seg_len(t):
        s, e = t.GetStart(), t.GetEnd()
        return math.hypot(e.x - s.x, e.y - s.y)

    def off45(t):
        s, e = t.GetStart(), t.GetEnd()
        return seg_len(t) >= 1000 and not _is45(e.x - s.x, e.y - s.y)

    fixed = 0
    tried = set()
    for _ in range(200):
        tracks, vias, ends = {}, {}, {}
        for t in board.GetTracks():
            if t.GetNetCode() not in nets:
                continue
            if t.GetClass() == "PCB_VIA":
                vias.setdefault(t.GetNetCode(), []).append(t)
            elif t.GetClass() == "PCB_TRACK":
                tracks[uid(t)] = t
                for p in (t.GetStart(), t.GetEnd()):
                    ends.setdefault((t.GetNetCode(), t.GetLayer(), p.x, p.y), []).append(uid(t))
        cand = [t for k, t in tracks.items() if k in own and k not in tried and seg_len(t) < tiny and off45(t)]
        if not cand:
            break
        seg = cand[0]
        tried.add(uid(seg))
        net, layer = seg.GetNetCode(), seg.GetLayer()

        def pinned(x, y):
            pt = pcbnew.VECTOR2I(int(x), int(y))
            # a via's centre (the join keeps leading to it; an end merely
            # inside the via's copper may move, the chain still reaches it)
            for v in vias.get(net, []):
                q = v.GetPosition()
                if math.hypot(q.x - x, q.y - y) <= 1000:
                    return True
            return any(p.IsOnLayer(layer) and p.HitTest(pt) for p in pads_by_net.get(net, []))

        def walk(start_uid, x, y):
            """From the chain end (x, y): the chain's tiny pieces met on the way
            and the movable neighbour (track uid) or None when the end is fixed."""
            chain, cur = [], start_uid
            while True:
                others = [k for k in ends.get((net, layer, x, y), []) if k != cur]
                if len(others) != 1 or pinned(x, y):
                    return chain, None, (x, y)
                k = others[0]
                if k in chain or k == uid(seg):
                    return chain, None, (x, y)
                t = tracks[k]
                if k not in own:
                    return chain, None, (x, y)
                s, e = t.GetStart(), t.GetEnd()
                far = (e.x, e.y) if (s.x, s.y) == (x, y) else (s.x, s.y)
                if seg_len(t) < tiny:
                    chain.append(k)
                    cur = k
                    x, y = far
                    continue
                if not _is45(far[0] - x, far[1] - y):
                    return chain, None, (x, y)
                return chain, k, (x, y)

        s, e = seg.GetStart(), seg.GetEnd()
        chain_a, ka, pa = walk(uid(seg), s.x, s.y)
        chain_b, kb, pb = walk(uid(seg), e.x, e.y)
        chain = [uid(seg)] + chain_a + chain_b

        def far_end(k, node):
            t = tracks[k]
            s, e = t.GetStart(), t.GetEnd()
            return (e.x, e.y) if (s.x, s.y) == node else (s.x, s.y)

        def unit(a, b):
            d = math.hypot(b[0] - a[0], b[1] - a[1])
            return ((b[0] - a[0]) / d, (b[1] - a[1]) / d)

        def keeps_way(far, node, x):
            # the stretched/cut segment still runs from far towards node, long enough
            return ((x[0] - far[0]) * (node[0] - far[0]) + (x[1] - far[1]) * (node[1] - far[1]) > 0
                    and math.hypot(x[0] - far[0], x[1] - far[1]) >= 10_000)

        def obtuse(x, p, q):
            u, w = unit(x, p), unit(x, q)
            return u[0] * w[0] + u[1] * w[1] <= 0.05

        def move_end(k, node, x):
            t = tracks[k]
            st = t.GetStart()
            pt = pcbnew.VECTOR2I(int(round(x[0])), int(round(x[1])))
            if (st.x, st.y) == node:
                t.SetStart(pt)
            else:
                t.SetEnd(pt)

        def drop(keys):
            for k in keys:
                t = tracks[k]
                board.Remove(t)
                t.thisown = 0
                _REMOVED.append(t)

        done = False
        if ka and kb and ka != kb:
            fa, fb = far_end(ka, pa), far_end(kb, pb)
            x = _line_cross(pa, unit(fa, pa), pb, unit(fb, pb))
            if (x is not None and math.hypot(x[0] - pa[0], x[1] - pa[1]) <= reach
                    and math.hypot(x[0] - pb[0], x[1] - pb[1]) <= reach
                    and keeps_way(fa, pa, x) and keeps_way(fb, pb, x) and obtuse(x, fa, fb)):
                move_end(ka, pa, x)
                move_end(kb, pb, x)
                drop(chain)
                done = True
        if not done:
            # one end stays put: a short 45-degree join from it to the other segment
            for k, node, fixed_pt in ((ka, pa, pb), (kb, pb, pa)):
                if not k:
                    continue
                f = far_end(k, node)
                best = None
                for d in _DIRS45:
                    x = _line_cross(fixed_pt, d, node, unit(f, node))
                    if x is None:
                        continue
                    t_along = (x[0] - fixed_pt[0]) * d[0] + (x[1] - fixed_pt[1]) * d[1]
                    if t_along < -1 or t_along > reach or math.hypot(x[0] - node[0], x[1] - node[1]) > reach:
                        continue
                    if not keeps_way(f, node, x):
                        continue
                    if t_along > 1000 and not obtuse(x, fixed_pt, f):
                        continue
                    score = math.hypot(x[0] - node[0], x[1] - node[1])
                    if best is None or score < best[0]:
                        best = (score, x, t_along)
                if best is None:
                    continue
                _, x, t_along = best
                move_end(k, node, x)
                keep = tracks[chain[0]]
                if t_along > 1000:
                    keep.SetStart(pcbnew.VECTOR2I(int(fixed_pt[0]), int(fixed_pt[1])))
                    keep.SetEnd(pcbnew.VECTOR2I(int(round(x[0])), int(round(x[1]))))
                    drop(chain[1:])
                else:
                    move_end(k, (int(round(x[0])), int(round(x[1]))), fixed_pt)
                    drop(chain)
                done = True
                break
        if not done and not ka and not kb:
            # nothing to stretch: a piece inside one pad of its net is not needed
            pts = [pa, pb]
            if any(p.IsOnLayer(layer) and all(p.HitTest(pcbnew.VECTOR2I(int(q[0]), int(q[1]))) for q in pts)
                   for p in pads_by_net.get(net, [])):
                drop(chain)
                done = True
        if done:
            fixed += 1
    return fixed


_CONTROL_WORDS = {"EN", "BUT", "LED", "OC", "NOC", "GOOD", "PG", "SENSE", "FAULT", "FLT", "ALERT", "INT", "RST", "NRST", "DET"}


def _is_rail(name: str, patterns) -> bool:
    """A supply net by name (the power patterns), not a control signal that
    only carries a power word (HUB_PWR_EN1, PWR_BUT, nPWR_LED)."""
    import fnmatch
    if not any(fnmatch.fnmatchcase(name.upper(), p.upper()) for p in patterns):
        return False
    words = {w for w in name.upper().replace("-", "_").replace("/", "_").split("_") if w}
    return not (words & _CONTROL_WORDS or any(w.rstrip("0123456789") in _CONTROL_WORDS for w in words))


def widen_power(board, items, patterns, cap: int = 500_000, neck: int = 750_000,
                fine_pitch: int = 800_000, fine_margin: int = 500_000) -> int:
    """After a run: the run's power-rail segments get as wide as the room
    around them allows, up to `cap` (pre-defined widths only). Routing keeps
    a rail at the width its narrowest pad fits so that it never takes room a
    signal needs; with every route in place, widening only where nothing of
    another net comes within clearance cannot open or short anything. A
    segment too close to something along its whole length keeps its width;
    one that is only tight at an end (a fine-pitch pad, a via between pins)
    keeps a narrow neck there. Returns how many segments were widened."""
    widths = sorted({int(w) for w in board.GetDesignSettings().m_TrackWidthList if 0 < int(w) <= cap}, reverse=True)
    if not widths:
        return 0
    rails = {c for c, n in board.GetNetsByNetcode().items() if c > 0 and _is_rail(n.GetNetname(), patterns)}
    segs = [t for t in items if t.GetClass() == "PCB_TRACK" and t.GetNetCode() in rails]
    if not segs:
        return 0
    hole_clear = int(board.GetDesignSettings().m_HoleClearance)
    edge_clear = int(board.GetDesignSettings().m_CopperEdgeClearance)
    others = [t for t in board.GetTracks()]
    pads = list(board.GetPads())
    edges = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
    for fp in board.GetFootprints():
        edges += [d for d in fp.GraphicalItems() if d.GetLayer() == pcbnew.Edge_Cuts]

    nc_clear = {}

    def gap(item, layer) -> int:
        # the larger of the item's resolved clearance and its net class's
        # (a run's grid rounds to the class value; DRC wants it in full)
        code = item.GetNetCode()
        if code not in nc_clear:
            try:
                nc_clear[code] = int(item.GetEffectiveNetClass().GetClearance())
            except Exception:
                nc_clear[code] = 0
        return max(int(item.GetOwnClearance(layer)), nc_clear[code])

    # fine-pitch parts (a pin within 0.8 mm of the next): their pad area and a
    # margin round it stay at routing width. A wide rail there reads as a blob
    # over the pin row or a ring hugging a QFN's toes, not as a trunk.
    fine_boxes = []
    for fp in board.GetFootprints():
        fpads = list(fp.Pads())
        pos = [(p.GetPosition().x, p.GetPosition().y) for p in fpads]
        if len(pos) < 4 or not any(min(math.hypot(x - u, y - v) for j, (u, v) in enumerate(pos) if j != i) < fine_pitch
                                   for i, (x, y) in enumerate(pos)):
            continue
        box = None
        for p in fpads:
            if box is None:
                box = p.GetBoundingBox()
            else:
                box.Merge(p.GetBoundingBox())
        box.Inflate(fine_margin)
        fine_boxes.append(box)

    # how far to look: the largest clearance anything on the board asks for
    # (a mounting hole's keep-out can be well over a millimetre)
    look = 1_000_000
    for p in pads:
        try:
            look = max(look, int(p.GetLocalClearance() or 0) + 500_000)
        except Exception:
            pass

    def clear(t, layer) -> bool:
        net = t.GetNetCode()
        box = t.GetBoundingBox()
        if any(box.Intersects(f) for f in fine_boxes):
            return False
        box.Inflate(look)
        shape = t.GetEffectiveShape(layer)
        own = gap(t, layer) + 5_000
        for o in others:
            if o.GetNetCode() == net or not o.IsOnLayer(layer) or not o.GetBoundingBox().Intersects(box):
                continue
            if o.GetClass() == "PCB_VIA" or o.GetLayer() == layer:
                if shape.Collide(o.GetEffectiveShape(layer), max(own, gap(o, layer) + 5_000)):
                    return False
        for p in pads:
            if not p.GetBoundingBox().Intersects(box):
                continue
            if p.GetNetCode() != net or p.GetNetCode() <= 0:
                if p.IsOnLayer(layer) and shape.Collide(p.GetEffectiveShape(layer), max(own, gap(p, layer) + 5_000)):
                    return False
                if p.HasHole():
                    # an unplated hole keeps the pad's own clearance (a mounting
                    # hole's keep-out), a plated one the board's hole clearance
                    hc = hole_clear
                    if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                        hc = max(hc, gap(p, layer))
                    if shape.Collide(p.GetEffectiveHoleShape(), hc + 5_000):
                        return False
        for d in edges:
            if d.GetBoundingBox().Intersects(box) and shape.Collide(d.GetEffectiveShape(), edge_clear + 5_000):
                return False
        return True

    def at(s, e, d):
        L = math.hypot(e.x - s.x, e.y - s.y)
        return pcbnew.VECTOR2I(int(round(s.x + (e.x - s.x) * d / L)), int(round(s.y + (e.y - s.y) * d / L)))

    widened = 0
    segs.sort(key=lambda t: -t.GetLength())
    for t in segs:
        w0 = t.GetWidth()
        layer = t.GetLayer()
        # copies: SWIG's GetStart/GetEnd hand back the track's own point
        s = pcbnew.VECTOR2I(t.GetStart().x, t.GetStart().y)
        e = pcbnew.VECTOR2I(t.GetEnd().x, t.GetEnd().y)
        length = t.GetLength()
        done = False
        for w in widths:
            if w <= w0:
                break
            # whole segment, then with a neck at the start, the end or both
            for a, b in ((0, 0), (neck, 0), (0, neck), (neck, neck),
                         (2 * neck, 0), (0, 2 * neck), (2 * neck, 2 * neck)):
                if a + b and length < a + b + neck:
                    continue
                ns, ne = (at(s, e, a) if a else s), (at(s, e, length - b) if b else e)
                t.SetStart(ns)
                t.SetEnd(ne)
                t.SetWidth(w)
                if clear(t, layer):
                    for p, q in ([(s, ns)] if a else []) + ([(ne, e)] if b else []):
                        extra = pcbnew.PCB_TRACK(board)
                        extra.SetStart(p)
                        extra.SetEnd(q)
                        extra.SetWidth(w0)
                        extra.SetLayer(layer)
                        extra.SetNetCode(t.GetNetCode())
                        board.Add(extra)
                        others.append(extra)
                    done = True
                    break
                t.SetStart(s)
                t.SetEnd(e)
                t.SetWidth(w0)
            if done:
                break
        if done:
            widened += 1
    return widened


def _pad_groups(board, conn, code):
    """The net's pads in groups that its copper (fills included) connects."""
    pads = [p for p in board.GetPads() if p.GetNetCode() == code]
    seen, groups = set(), []
    for p in pads:
        uid = str(p.m_Uuid.AsString())
        if uid in seen:
            continue
        group = [p]
        seen.add(uid)
        try:
            linked = list(conn.GetConnectedItems(p))
        except Exception:
            linked = []
        for item in linked:
            if item.GetClass() == "PAD" and str(item.m_Uuid.AsString()) not in seen:
                seen.add(str(item.m_Uuid.AsString()))
                group.append(item)
        groups.append(group)
    groups.sort(key=len, reverse=True)
    return groups


def repair_islands(board, items=None, reach: int = 1_500_000, step: int = 50_000, cancelled=None) -> int:
    """After every other clean-up: a piece of a pour that its pads alone hold
    (a signal routed round two GND pads of a resistor and a capacitor cuts
    them off the surface pour) gets a short stub from one of its pads to a
    via that lands in the net's connected fill on another layer. The
    router's grid check is a little coarser than DRC and can find no room
    where there is; this checks the copper itself, with the clearances DRC
    uses. The pours are refilled first; a via that does not join the island
    to the net after the refill comes off again. Only pads that sit in a
    piece of their net's fill are islands: pads no pour reaches are the
    router's to join, not this pass's. Returns how many islands were joined."""
    zones = [z for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0]
    if not zones:
        return 0
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    if conn.GetUnconnectedCount(True) == 0:
        return 0
    ds = board.GetDesignSettings()
    hole_clear = int(ds.m_HoleClearance)
    hole_hole = int(ds.m_HoleToHoleMin)
    edge_clear = int(ds.m_CopperEdgeClearance)
    copper = list(board.GetEnabledLayers().CuStack())
    keepouts = [z for z in board.Zones() if z.GetIsRuleArea()]
    edges = [d for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
    for fp in board.GetFootprints():
        edges += [d for d in fp.GraphicalItems() if d.GetLayer() == pcbnew.Edge_Cuts]
    nc_clear = {}

    def netclass(name):
        return ds.m_NetSettings.GetEffectiveNetClass(name)

    def gap(item, layer) -> int:
        code = item.GetNetCode()
        if code not in nc_clear:
            try:
                nc_clear[code] = int(netclass(item.GetNetname()).GetClearance())
            except Exception:
                nc_clear[code] = 0
        try:
            own = int(item.GetOwnClearance(layer))
        except Exception:
            own = 0
        return max(own, nc_clear[code])

    graphics = []                                 # copper drawings and text: obstacles like any copper
    for holder in [board.GetDrawings()] + [fp.GraphicalItems() for fp in board.GetFootprints()]:
        for d in holder:
            try:
                if d.IsOnCopperLayer():
                    graphics.append(d)
            except Exception:
                pass
    added_items, repairs = [], []
    joined = 0
    for code in sorted({z.GetNetCode() for z in zones}):
        groups = _pad_groups(board, conn, code)
        if len(groups) < 2:
            continue
        fills = [(lid, z.GetFilledPolysList(lid)) for z in zones if z.GetNetCode() == code
                 for lid in z.GetLayerSet().CuStack()]
        fills = [(lid, poly, i, pcbnew.SHAPE_POLY_SET(poly.Outline(i))) for lid, poly in fills
                 for i in range(poly.OutlineCount())]

        def pieces(group):
            """The pieces of fill a pad group is in: (layer, fill, outline index)."""
            pts, shapes = {}, {}
            for item in group + [i for i in conn.GetConnectedItems(group[0])]:
                kind = item.GetClass()
                if kind in ("PAD", "PCB_VIA"):
                    for lid in item.GetLayerSet().CuStack():
                        pts.setdefault(lid, []).append(item.GetPosition())
                        if kind == "PAD":
                            shapes.setdefault(lid, []).append(item)
                elif kind in ("PCB_TRACK", "PCB_ARC"):
                    pts.setdefault(item.GetLayer(), []).extend([item.GetStart(), item.GetEnd()])
            out = []
            for lid, poly, i, whole in fills:
                if any(poly.Contains(pt, i) for pt in pts.get(lid, [])):
                    out.append((lid, poly, i))
                    continue
                # a thermal relief leaves the pad's centre out of the fill:
                # the spokes still touch its copper
                box = poly.Outline(i).BBox()
                for pad in shapes.get(lid, []):
                    try:
                        if box.Intersects(pad.GetBoundingBox()) and whole.Collide(pad.GetEffectiveShape(lid), 0):
                            out.append((lid, poly, i))
                            break
                    except Exception:
                        pass
            return out

        held = [pieces(g) for g in groups]
        # the connected part of the net: the group in the most fill (a cut-off
        # piece can hold more pads than the plane's own few)
        area = [sum(abs(poly.Outline(i).Area()) for _, poly, i in h) for h in held]
        m = max(range(len(groups)), key=lambda k: (area[k], len(groups[k])))
        targets = held[m]
        if not targets:
            continue
        nc = netclass(groups[m][0].GetNetname())
        via_d, drill, width = int(nc.GetViaDiameter()), int(nc.GetViaDrill()), int(nc.GetTrackWidth())
        for k, group in enumerate(groups):
            if k == m or not held[k]:
                continue                              # no fill of its own: not an island
            if cancelled and cancelled():
                break
            if _join_island(board, group, code, targets, via_d, drill, width, reach, step, gap, hole_clear,
                            hole_hole, edge_clear, copper, keepouts, edges, graphics, added_items, repairs, groups[m][0]):
                joined += 1
    if not added_items:
        return 0
    # the refill decides: an island the new via does not join keeps nothing
    filler.Fill(board.Zones())
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    undone = False
    for group_pad, made, main_pad in repairs:
        uid = str(main_pad.m_Uuid.AsString())
        try:
            together = any(i.GetClass() == "PAD" and str(i.m_Uuid.AsString()) == uid
                           for i in conn.GetConnectedItems(group_pad))
        except Exception:
            together = False
        if together:
            continue
        joined -= 1
        undone = True
        for it in made:
            board.Remove(it)
            added_items.remove(it)
    if undone:
        filler.Fill(board.Zones())
        board.BuildConnectivity()
    if isinstance(items, list):
        items.extend(added_items)
    return joined


def _join_island(board, group, code, targets, via_d, drill, width, reach, step, gap, hole_clear, hole_hole,
                 edge_clear, copper, keepouts, edges, graphics, added_items, repairs, main_pad) -> bool:
    """One stub and via for an island's pads; True when placed."""
    margin = 5_000
    box = None
    for p in group:
        b = p.GetBoundingBox()
        if box is None:
            box = b
        else:
            box.Merge(b)
    box.Inflate(reach + 2_000_000)
    near = [t for t in board.GetTracks() if t.GetBoundingBox().Intersects(box)]
    near_pads = [p for p in board.GetPads() if p.GetBoundingBox().Intersects(box)]
    near_graphics = [d for d in graphics if d.GetBoundingBox().Intersects(box)]
    holes = [(t.GetPosition(), t.GetDrillValue() // 2) for t in near if t.GetClass() == "PCB_VIA"]
    holes += [(p.GetPosition(), max(p.GetDrillSize().x, p.GetDrillSize().y) // 2) for p in near_pads if p.HasHole()]

    def clear(item, layers, hole=False) -> bool:
        ibox = item.GetBoundingBox()
        ibox.Inflate(1_000_000)
        for layer in layers:
            shape = item.GetEffectiveShape(layer)
            own = gap(item, layer) + margin
            for o in near:
                if o.GetNetCode() == code or not o.IsOnLayer(layer) or not o.GetBoundingBox().Intersects(ibox):
                    continue
                if o.GetClass() == "PCB_VIA" or o.GetLayer() == layer:
                    if shape.Collide(o.GetEffectiveShape(layer), max(own, gap(o, layer) + margin)):
                        return False
            for p in near_pads:
                if not p.GetBoundingBox().Intersects(ibox):
                    continue
                if p.GetNetCode() != code or code <= 0:
                    if p.IsOnLayer(layer) and shape.Collide(p.GetEffectiveShape(layer), max(own, gap(p, layer) + margin)):
                        return False
                    if p.HasHole():
                        hc = hole_clear
                        if p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH:
                            hc = max(hc, gap(p, layer))
                        if shape.Collide(p.GetEffectiveHoleShape(), hc + margin):
                            return False
            for d in edges:
                if d.GetBoundingBox().Intersects(ibox) and shape.Collide(d.GetEffectiveShape(), edge_clear + margin):
                    return False
            for d in near_graphics:
                try:
                    if d.IsOnLayer(layer) and d.GetBoundingBox().Intersects(ibox) \
                            and getattr(d, "GetNetCode", lambda: 0)() != code \
                            and shape.Collide(d.GetEffectiveShape(layer), own):
                        return False
                except Exception:
                    return False
        if hole:
            hs = item.GetEffectiveHoleShape()
            for layer in copper:
                for o in near:
                    if o.GetNetCode() != code and o.IsOnLayer(layer) and o.GetBoundingBox().Intersects(ibox) \
                            and (o.GetClass() == "PCB_VIA" or o.GetLayer() == layer) \
                            and hs.Collide(o.GetEffectiveShape(layer), hole_clear + margin):
                        return False
            pos = item.GetPosition()
            for c, r in holes:
                if math.hypot(c.x - pos.x, c.y - pos.y) - r - drill // 2 < hole_hole + margin:
                    return False
        return True

    ring = [(0, 0)] + [(int(via_d / 2 * math.cos(k * math.pi / 4)), int(via_d / 2 * math.sin(k * math.pi / 4)))
                       for k in range(8)]
    no_via = [z for z in keepouts if z.GetDoNotAllowVias() or z.GetDoNotAllowTracks()]
    no_track = [z for z in keepouts if z.GetDoNotAllowTracks()]

    def keepout(x, y) -> bool:
        """The via's pad, not just its centre, in an area that bans it."""
        for z in no_via:
            outline = z.Outline()
            if any(outline.Contains(pcbnew.VECTOR2I(x + dx, y + dy)) for dx, dy in ring):
                return True
        return False

    def stub_banned(t, layer) -> bool:
        s_, e_ = t.GetStart(), t.GetEnd()
        n_ = max(1, int(math.hypot(e_.x - s_.x, e_.y - s_.y) / 100_000))
        for z in no_track:
            if not z.IsOnLayer(layer):
                continue
            outline = z.Outline()
            if any(outline.Contains(pcbnew.VECTOR2I(int(s_.x + (e_.x - s_.x) * k / n_), int(s_.y + (e_.y - s_.y) * k / n_)))
                   for k in range(n_ + 1)):
                return True
        return False

    def lands(x, y, layer) -> bool:
        r = via_d // 2
        for lid, poly, i in targets:
            if lid == layer:
                continue
            if all(poly.Contains(pcbnew.VECTOR2I(x + dx, y + dy), i)
                   for dx, dy in ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r))):
                return True
        return False

    def in_pad(x, y) -> bool:
        pt = pcbnew.VECTOR2I(x, y)
        return any(p.HitTest(pt) for p in near_pads)

    spots = []
    n = reach // step
    for p in group:
        if p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
            continue
        layer = p.GetLayer()
        if layer not in copper:
            continue
        c = p.GetPosition()
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                d2 = i * i + j * j
                if 0 < d2 <= n * n:
                    spots.append((d2, p, layer, c.x + i * step, c.y + j * step))
    spots.sort(key=lambda s: s[0])
    from .grid import jog

    def on_pad(x, y) -> bool:
        # the via's ring over pad copper: solder wicks down an open via
        r = via_d // 2
        for p in near_pads:
            bb = p.GetBoundingBox()
            if bb.GetLeft() - r < x < bb.GetRight() + r and bb.GetTop() - r < y < bb.GetBottom() + r:
                if any(p.HitTest(pcbnew.VECTOR2I(x + dx, y + dy)) for dx, dy in
                       ((0, 0), (r, 0), (-r, 0), (0, r), (0, -r), (r * 7 // 10, r * 7 // 10), (-r * 7 // 10, r * 7 // 10),
                        (r * 7 // 10, -r * 7 // 10), (-r * 7 // 10, -r * 7 // 10))):
                    return True
        return False

    # spots clear of every pad first; one with its ring on a pad of the
    # island only when nothing else fits
    # (the cheap tests first; what the first round put off waits its turn)
    if not any(lid != p.GetLayer() for lid, _, _ in targets for p in group):
        return False                              # no fill on another layer to land in
    def place(spot) -> bool:
        _, p, layer, x, y = spot
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(x, y))
        via.SetWidth(via_d)
        via.SetDrill(drill)
        via.SetNetCode(code)
        try:
            via.SetViaType(pcbnew.VIATYPE_THROUGH)
        except Exception:
            pass
        via.SetLayerPair(copper[0], copper[-1])
        if not clear(via, copper, hole=True):
            return False
        c = p.GetPosition()
        for at_end in (False, True):
            pts = jog(c.x, c.y, x, y, at_end=at_end)
            stubs = []
            for k in range(len(pts) - 1):
                t = pcbnew.PCB_TRACK(board)
                t.SetStart(pcbnew.VECTOR2I(*pts[k]))
                t.SetEnd(pcbnew.VECTOR2I(*pts[k + 1]))
                t.SetWidth(width)
                t.SetLayer(layer)
                t.SetNetCode(code)
                stubs.append(t)
            if all(clear(t, [layer]) and not stub_banned(t, layer) for t in stubs):
                made = stubs + [via]
                for it in made:
                    board.Add(it)
                    added_items.append(it)
                repairs.append((p, made, main_pad))
                return True
        return False

    put_off = []
    for spot in spots:
        _, p, layer, x, y = spot
        if not lands(x, y, layer) or keepout(x, y) or in_pad(x, y):
            continue
        if on_pad(x, y):
            put_off.append(spot)
        elif place(spot):
            return True
    return any(place(spot) for spot in put_off)


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
