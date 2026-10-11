"""One connection: A* from every cell of the source cluster to any cell of
the target cluster, across routable layers with vias.

Costs are integers: 10 per straight cell, 14 per diagonal, doubled for a
straight move against the layer's preferred direction,
plus a small turn penalty and the via cost. The heuristic is octile distance
to the nearest target anchor, so it stays admissible for in-layer moves.
"""
import heapq
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .grid import BLOCKED, Grid

STRAIGHT, DIAGONAL, AGAINST_STRAIGHT = 10, 14, 20
# (dx, dy, is_diagonal)
MOVES = ((1, 0, False), (-1, 0, False), (0, 1, False), (0, -1, False),
         (1, 1, True), (1, -1, True), (-1, 1, True), (-1, -1, True))


def route(grid: Grid, owners, via_block: bytearray, net: int,
          sources: Iterable[int], goals: Set[int], anchors: Sequence[Tuple[int, int]],
          horizontal: Sequence[bool], via_cost: int, allow_vias: bool, via_reach: int,
          max_expansions: int, turn_cost: int = 2,
          cancelled=None, routed=None, rippable=None, rip_penalty: int = 0) -> Optional[List[int]]:
    """The cell path (grid indices, source to goal) or None.

    With `routed` (cell -> path id of routed copper, 0 for none) and
    `rippable(pid)`, cells of other paths that may be ripped up are passable
    at rip_penalty per cell: the result then says which paths are in the way.
    """
    w, plane, nlayers = grid.w, grid.plane, grid.layers
    via_step = via_cost * STRAIGHT
    soft = routed is not None and rippable is not None
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

    def passable(idx: int) -> bool:
        o = owners[idx]
        if o == 0 or o == net or idx in goals:
            return True
        if soft:
            pid = routed[idx]
            return pid != 0 and rippable(pid)
        return False

    def extra(idx: int) -> int:
        """Rip-up penalty for stepping onto another path's cell."""
        if soft and owners[idx] not in (0, net) and routed[idx] != 0:
            return rip_penalty
        return 0

    # The via barrel reaches via_reach cells beyond the track half-width the
    # owner maps already account for: every cell of that disc must be free of
    # other nets on every layer.
    reach2 = via_reach * via_reach
    disc = [(dx, dy) for dx in range(-via_reach, via_reach + 1)
            for dy in range(-via_reach, via_reach + 1) if dx * dx + dy * dy <= reach2]

    def via_ok(cx: int, cy: int) -> bool:
        if via_block[cy * w + cx]:
            return False
        for dx, dy in disc:
            px, py = cx + dx, cy + dy
            if not (0 <= px < w and 0 <= py < grid.h):
                return False
            off = py * w + px
            for layer in range(nlayers):
                o = owners[layer * plane + off]
                if o != 0 and o != net:
                    if soft and routed[layer * plane + off] != 0 and rippable(routed[layer * plane + off]):
                        continue
                    return False
        return True

    g: Dict[int, int] = {}
    came: Dict[int, Tuple[int, int]] = {}      # idx -> (prev idx, move id)
    heap = []
    for s in sources:
        g[s] = 0
        came[s] = (-1, -1)
        heapq.heappush(heap, (h(s), 0, s, -1))
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
            return path
        closed.add(idx)
        expansions += 1
        if expansions > max_expansions:
            return None
        if cancelled is not None and expansions % 20000 == 0 and cancelled():
            return None

        layer, rest = divmod(idx, plane)
        cy, cx = divmod(rest, w)
        base = layer * plane
        horiz = horizontal[layer]
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
        if allow_vias and nlayers > 1 and via_ok(cx, cy):
            for other in range(nlayers):
                if other == layer:
                    continue
                nidx = other * plane + rest
                if nidx in closed or not passable(nidx):
                    continue
                ng = gc + via_step + extra(nidx)
                if ng < g.get(nidx, 1 << 60):
                    g[nidx] = ng
                    came[nidx] = (idx, 8)
                    heapq.heappush(heap, (ng + h(nidx), ng, nidx, 8))
    return None
