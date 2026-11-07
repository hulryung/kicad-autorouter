"""Putting routed paths on the board as tracks and vias (main thread only)."""
import math
import os
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
