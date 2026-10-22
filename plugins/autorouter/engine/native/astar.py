"""One connection: A* from every cell of the source cluster to any cell of
the target cluster, across routable layers with vias.

Costs are integers: 10 per straight cell along the layer's preferred
direction, 15 against it, 18 per diagonal,
plus a small turn penalty and the via cost. The heuristic is octile distance
to the nearest target anchor, so it stays admissible for in-layer moves.
"""
import heapq
import os
import time as _time
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .grid import BLOCKED, BODY_LANE, Grid

# Costs per move. Against the layer's preferred direction a straight move costs
# 1.5x; a diagonal is half along, half against, so it costs the mean of the
# two straight costs times sqrt(2): 14 * 1.25 = 17.5 -> 18. That keeps a
# straight run against the preferred direction (15 per cell) cheaper than a
# zig-zag of diagonals (18 per cell), while a true 45-degree move is still
# cheaper than an L (18 vs 25).
STRAIGHT, DIAGONAL, AGAINST_STRAIGHT = 10, 18, 15
LANE_PENALTY = 60                               # per cell inside another pad's outlet lane (6 straight cells)
BODY_PENALTY = 10                               # per cell under an IC body on its own layer: that run counts double
LAST = {"expansions": 0, "capped": False}       # what the last search did (debugging)
TOTALS = {"searches": 0, "expansions": 0, "capped": 0, "seconds": 0.0}   # over the process (profiling)
# Search work over the process: expansions plus a share per search, and whether
# a search is under way (its expansions so far are in LAST)
WORK = [0, False]
SEARCH_WORK = 50
# A search still going after PROBE_AT expansions checks whether its goal end
# is walled in (a pin whose outlet other copper took): a flood back from the
# goals of at most PROBE_CELLS cells. A walled-in goal then fails at once
# instead of after the whole board is searched up to the cap.
PROBE_AT = 20_000
PROBE_CELLS = 20_000
WORK_RATE = float(os.environ.get("KICAD_AUTOROUTER_WORK_RATE") or 0)


def clock() -> float:
    """Seconds for the time limits: the wall clock, or, with
    KICAD_AUTOROUTER_WORK_RATE set (expansions per second), the search work
    done so far at that rate. Benchmarks use it: the same input then gives the
    same result on a busy, sleeping or slower machine."""
    if WORK_RATE > 0:
        return (WORK[0] + (LAST["expansions"] if WORK[1] else 0)) / WORK_RATE
    return _time.time()

# (dx, dy, is_diagonal)
MOVES = ((1, 0, False), (-1, 0, False), (0, 1, False), (0, -1, False),
         (1, 1, True), (1, -1, True), (-1, 1, True), (-1, -1, True))


def route(*args, **kwargs) -> Optional[List[int]]:
    """_route, counting its work for clock()."""
    LAST["expansions"], WORK[1] = 0, True
    try:
        return _route(*args, **kwargs)
    finally:
        WORK[0] += LAST["expansions"] + SEARCH_WORK
        WORK[1] = False


def _route(grid: Grid, owners, via_block: bytearray, net: int,
          sources: Iterable[int], goals: Set[int], anchors: Sequence[Tuple[int, int]],
          horizontal: Sequence[bool], via_cost: int, allow_vias: bool, via_reach: int,
          max_expansions: int, turn_cost: int = 2,
          cancelled=None, routed=None, rippable=None, rip_penalty: int = 0,
          also_ok: int = 0, source_costs=None, routable=None,
          neck_owners=None, neck_region=None, via_guard=None, lane_guard=None,
          history=None, via_lead: int = 0, lead_from=None) -> Optional[List[int]]:
    """The cell path (grid indices, source to goal) or None.

    With `routed` (cell -> path id of routed copper, 0 for none) and
    `rippable(pid)`, cells of other paths that may be ripped up are passable
    at rip_penalty per cell: the result then says which paths are in the way.
    `history` (cell -> extra cost) is what earlier rip-ups learnt about
    contested cells: a route pays it so two nets stop fighting over one spot.
    `via_lead` keeps vias at least that many cells from the goals and from
    `lead_from` (a cell, or a pair of cells for the stretch between them; the
    first source when not given): a coupled pair needs a straight lead to
    flare out to its vias.
    """
    w, plane, nlayers = grid.w, grid.plane, grid.layers
    sources = list(sources)
    via_step = via_cost * STRAIGHT
    soft = routed is not None and rippable is not None
    routable = list(routable) if routable is not None else [True] * nlayers
    anchors = list(anchors) or [grid.unpack(next(iter(goals)))[1:]]

    def h(idx: int) -> int:
        rest = idx % plane
        cy, cx = divmod(rest, w)
        best = None
        for ax, ay in anchors:
            dx = ax - cx if ax >= cx else cx - ax
            dy = ay - cy if ay >= cy else cy - ay
            d = STRAIGHT * (dx if dx > dy else dy) + (DIAGONAL - STRAIGHT) * (dy if dx > dy else dx)
            if best is None or d < best:
                best = d
        return best

    necked = neck_owners is not None and neck_region

    def passable(idx: int) -> bool:
        # near a fine-pitch pin the track is narrower: judge those cells by
        # the neck-width owner map
        o = neck_owners[idx] if (necked and idx in neck_region) else owners[idx]
        if o == 0 or o == net or o == also_ok or idx in goals:
            return True
        if soft:
            pid = routed[idx]
            return pid != 0 and rippable(pid)
        return False

    def extra(idx: int) -> int:
        """Rip-up penalty for stepping onto another path's cell, and the
        price of running through another pad's outlet lane (allowed, but a
        detour of a few millimetres is preferred)."""
        cost = 0
        if lane_guard is not None:
            g = lane_guard[idx]
            if g == BODY_LANE:
                # the room inside an IC's pin ring on the part's layer: a
                # designer runs a track there only to reach a via of a pin
                # whose way out is under the body, not along the pin rows
                cost += BODY_PENALTY
            elif g and g != net and g != also_ok:
                cost += LANE_PENALTY
        if soft and owners[idx] not in (0, net) and routed[idx] != 0:
            cost += rip_penalty
        if history is not None:
            cost += history[idx]
        return cost

    # The via barrel reaches via_reach cells beyond the track half-width the
    # owner maps already account for: every cell of that disc must be free of
    # other nets on every layer.
    reach2 = via_reach * via_reach
    disc = [(dx, dy) for dx in range(-via_reach, via_reach + 1)
            for dy in range(-via_reach, via_reach + 1) if dx * dx + dy * dy <= reach2]

    # the disc as offsets from its centre on each layer; a disc that would
    # leave the board is refused up front (it reaches via_reach along the axes)
    disc_offsets = [layer * plane + dy * w + dx for layer in range(nlayers) for dx, dy in disc]
    gh = grid.h
    # owners do not change during one search: a spot's answer holds for every
    # layer the search reaches it on (the search expands a spot once per layer)
    via_memo: Dict[int, bool] = {}
    lead_cost = via_lead * STRAIGHT
    if via_lead:
        if lead_from is None:
            lead_from = list(sources)[:1]
        elif not isinstance(lead_from, (tuple, list)):
            lead_from = [lead_from]
        src = [grid.unpack(s)[1:] for s in lead_from]
        (sx0, sy0), (sx1, sy1) = src[0], src[-1]
        sdx, sdy = sx1 - sx0, sy1 - sy0
        slen2 = sdx * sdx + sdy * sdy

    def near_end(idx: int, cx: int, cy: int) -> bool:
        if not via_lead:
            return False
        if h(idx) < lead_cost:
            return True
        # distance to the source stretch
        t = 0.0 if slen2 == 0 else max(0.0, min(1.0, ((cx - sx0) * sdx + (cy - sy0) * sdy) / slen2))
        dx, dy = abs(sx0 + sdx * t - cx), abs(sy0 + sdy * t - cy)
        return STRAIGHT * max(dx, dy) + (DIAGONAL - STRAIGHT) * min(dx, dy) < lead_cost

    def via_ok(cx: int, cy: int) -> bool:
        rest = cy * w + cx
        known = via_memo.get(rest)
        if known is not None:
            return known
        ok = _via_ok(cx, cy, rest)
        via_memo[rest] = ok
        return ok

    def _via_ok(cx: int, cy: int, rest: int) -> bool:
        if via_block[rest]:
            return False
        if via_guard is not None:
            # the outlet of another net's pad escape: no via barrel there
            g = via_guard[rest]
            if g and g != net and g != also_ok:
                return False
        if cx < via_reach or cy < via_reach or cx >= w - via_reach or cy >= gh - via_reach:
            return False
        for off in disc_offsets:
            i = rest + off
            o = owners[i]
            if o != 0 and o != net and o != also_ok:
                if soft and routed[i] != 0 and rippable(routed[i]):
                    continue
                return False
        return True

    source_set = set(sources)

    def enclosed() -> Tuple[bool, int]:
        """(True, steps) when the cells the search could reach the goals
        from are a closed pocket: a flood back from the goals, by the
        search's own rules but looser (corners may be cut, vias go up to
        the lead), runs out without meeting a source or a cell the search
        has reached. (False, steps) when it meets one or grows past
        PROBE_CELLS."""
        seen = {c for c in goals if routable[c // plane]}
        todo = list(seen)
        steps = 0
        while todo:
            x = todo.pop()
            steps += 1
            if steps > PROBE_CELLS:
                return False, steps
            layer, rest = divmod(x, plane)
            cy, cx = divmod(rest, w)
            base = layer * plane
            near = []
            for dx, dy, _ in MOVES:
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < w and 0 <= ny < gh:
                    near.append(base + ny * w + nx)
            if allow_vias and nlayers > 1 and via_ok(cx, cy):
                near.extend(other * plane + rest for other in range(nlayers) if other != layer)
            for y in near:
                if y in seen:
                    continue
                if y in g or y in source_set:
                    return False, steps            # the search's side is in reach
                if routable[y // plane] and passable(y):
                    seen.add(y)
                    todo.append(y)
        return True, steps

    TOTALS["searches"] += 1
    _t0 = _time.time()
    g: Dict[int, int] = {}
    came: Dict[int, Tuple[int, int]] = {}      # idx -> (prev idx, move id)
    heap = []
    for s in sources:
        g0 = source_costs.get(s, 0) if source_costs else 0
        g[s] = g0
        came[s] = (-1, -1)
        heapq.heappush(heap, (g0 + h(s), g0, s, -1))
    closed: Set[int] = set()
    expansions = 0

    while heap:
        f, gc, idx, move = heapq.heappop(heap)
        if idx in closed:
            continue
        if idx in goals:
            path = [idx]
            prev = came[idx][0]
            while prev != -1:
                path.append(prev)
                prev = came[prev][0]
            path.reverse()
            TOTALS["expansions"] += expansions
            TOTALS["seconds"] += _time.time() - _t0
            return path
        closed.add(idx)
        expansions += 1
        if expansions == PROBE_AT:
            # a long search: is the goal end walled in? Then no amount of
            # searching from this side reaches it (it would wander the board
            # to the cap); the flood's own steps count as search work
            shut, steps = enclosed()
            expansions += steps
            if shut:
                LAST["expansions"], LAST["capped"] = expansions, False
                TOTALS["expansions"] += expansions
                TOTALS["enclosed"] = TOTALS.get("enclosed", 0) + 1
                TOTALS["seconds"] += _time.time() - _t0
                return None
        LAST["expansions"], LAST["capped"] = expansions, False
        if expansions > max_expansions:
            LAST["capped"] = True
            TOTALS["capped"] += 1
            TOTALS["expansions"] += expansions
            TOTALS["seconds"] += _time.time() - _t0
            return None
        if cancelled is not None and expansions % 20000 == 0 and cancelled():
            return None

        layer, rest = divmod(idx, plane)
        cy, cx = divmod(rest, w)
        base = layer * plane
        horiz = horizontal[layer]
        if not routable[layer]:
            continue                       # a pad exit on an excluded layer: nowhere to go
        for m, (dx, dy, diag) in enumerate(MOVES):
            nx, ny = cx + dx, cy + dy
            if nx < 0 or ny < 0 or nx >= w or ny >= grid.h:
                continue
            nidx = base + ny * w + nx
            if nidx in closed or not passable(nidx):
                continue
            if diag:
                # no corner cutting: both orthogonal neighbours must be free
                if not (passable(base + cy * w + nx) and passable(base + ny * w + cx)):
                    continue
                step = DIAGONAL
            else:
                along = (dy == 0) if horiz else (dx == 0)
                step = STRAIGHT if along else AGAINST_STRAIGHT
            if move != -1 and move != m:
                step += turn_cost
            ng = gc + step + extra(nidx)
            if ng < g.get(nidx, 1 << 60):
                g[nidx] = ng
                came[nidx] = (idx, m)
                heapq.heappush(heap, (ng + h(nidx), ng, nidx, m))
        if allow_vias and nlayers > 1:
            # the layers a via here could go on to, before the costly barrel check
            targets = [nidx for nidx in (other * plane + rest for other in range(nlayers)
                                         if other != layer and routable[other])
                       if nidx not in closed and passable(nidx) and gc + via_step < g.get(nidx, 1 << 60)]
            if not targets or near_end(idx, cx, cy) or not via_ok(cx, cy):
                continue
            for nidx in targets:
                ng = gc + via_step + extra(nidx)
                if ng < g.get(nidx, 1 << 60):
                    g[nidx] = ng
                    came[nidx] = (idx, 8)
                    heapq.heappush(heap, (ng + h(nidx), ng, nidx, 8))
    return None
