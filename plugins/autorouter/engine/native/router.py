"""Routing every connection of a RoutingProblem, with rip-up and reroute.

Runs on a worker thread; it never touches pcbnew. Progress, each routed path
and each ripped-up path go out through callbacks (the dialog marshals them to
the UI thread and keeps the board in step).

Order of work:
  1. every connection, shortest first, with a plain A*;
  2. for each failed connection, a "soft" A* that may cross other routed
     paths at a penalty; the paths it crosses are ripped up, the connection
     is routed for real, and the ripped paths are rerouted (they may end up
     elsewhere, or fail and get their own turn in the next round);
  3. a last pass over whatever is still open with a bigger budget and cheaper
     vias.
Fixed copper (pads, pre-existing tracks, keepouts, escape stubs) is never
ripped up; only paths this run created are.
"""
import math
import time
from array import array
from typing import Callable, Dict, List, Optional, Set, Tuple

import os

from . import astar
from .grid import BLOCKED, Grid, jog
from .problem import Connection, NetInfo, RoutedPath, RoutingProblem

ProgressFn = Callable[[int, int, int, int, str], None]    # done, total, routed, failed, net name
PathFn = Callable[[RoutedPath], None]
RemoveFn = Callable[[int], None]                            # path id
_DEBUG = bool(os.environ.get("AUTOROUTER_DEBUG"))
RIP_PENALTY = 400                                           # per cell, in cost units (40 straight cells)
HISTORY_STEP = 100                                          # per rip-up, on each contested cell (10 straight cells)
MAX_BLOCKERS = 10
PROTECTED_GROUPS = ("pairs", "memory", "high_speed")          # routing groups lower groups may not rip up


class RouteResult:
    def __init__(self):
        self.routed = 0
        self.failed: List[Connection] = []
        self.paths: List[RoutedPath] = []
        self.ripped = 0
        self.fanout = 0
        self.pairs = 0
        self.stitched = 0
        self.shoved = 0                                  # existing board paths ripped up and rerouted
        self.failed_detail: List[dict] = []
        self.seconds = 0.0
        self.cancelled = False
        self.timed_out = False                           # the time limit cut the run short


class _Path:
    """A routed connection and everything needed to undo it."""
    def __init__(self, pid: int, conn: Connection, path: RoutedPath, cells: List[int], paired: bool = False):
        self.pid, self.conn, self.path, self.cells = pid, conn, path, cells
        self.claims: Dict[Tuple[int, int], List[int]] = {}      # owner-map key -> claimed cells
        self.via_cells: List[int] = []
        self.joins: List[Tuple[int, int]] = [(conn.a, conn.b)]  # cluster pairs this path connects
        self.existing = False                                   # copper that was on the board already
        self.paired = paired                                   # half of a coupled pair: never ripped up


def _segment_distance(a, b) -> float:
    """Shortest distance between two segments (x1, y1, x2, y2)."""
    def seg_point(x1, y1, x2, y2, px, py):
        dx, dy = x2 - x1, y2 - y1
        l2 = dx * dx + dy * dy
        t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
        return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))

    def cross(ox, oy, ax, ay, bx, by):
        return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)

    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    d1 = cross(ax1, ay1, ax2, ay2, bx1, by1)
    d2 = cross(ax1, ay1, ax2, ay2, bx2, by2)
    d3 = cross(bx1, by1, bx2, by2, ax1, ay1)
    d4 = cross(bx1, by1, bx2, by2, ax2, ay2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(seg_point(ax1, ay1, ax2, ay2, bx1, by1), seg_point(ax1, ay1, ax2, ay2, bx2, by2),
               seg_point(bx1, by1, bx2, by2, ax1, ay1), seg_point(bx1, by1, bx2, by2, ax2, ay2))


def _offset_polyline(points: List[Tuple[int, int]], d: float, side: int) -> List[Tuple[int, int]]:
    """A polyline shifted sideways by d (mitred corners), to one side."""
    n = len(points)
    if n < 2:
        return list(points)
    normals = []
    for i in range(n - 1):
        ux, uy = points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]
        length = math.hypot(ux, uy) or 1.0
        normals.append((-uy / length * side, ux / length * side))
    out = []
    for i in range(n):
        if i == 0:
            nx, ny = normals[0]
            scale = d
        elif i == n - 1:
            nx, ny = normals[-1]
            scale = d
        else:
            ax, ay = normals[i - 1]
            bx, by = normals[i]
            mx, my = ax + bx, ay + by
            m = math.hypot(mx, my)
            if m < 1e-9:                                  # a U-turn: fall back to the next normal
                nx, ny, scale = bx, by, d
            else:
                nx, ny = mx / m, my / m
                scale = d / max(0.3, nx * bx + ny * by)   # mitre, capped for very sharp corners
        out.append((int(round(points[i][0] + nx * scale)), int(round(points[i][1] + ny * scale))))
    return out


def _two_piece(grid: Grid, a: int, b: int, passable) -> Optional[List[int]]:
    """Cells of an octilinear two-piece path from cell a to cell b on one
    layer (a diagonal run and a straight run, either order), or None if any
    cell is blocked or a diagonal step would cut a corner."""
    plane, w = grid.plane, grid.w
    layer = a // plane
    ax, ay = a % w, (a % plane) // w
    bx, by = b % w, (b % plane) // w
    dx, dy = bx - ax, by - ay
    sx = (dx > 0) - (dx < 0)
    sy = (dy > 0) - (dy < 0)
    ndiag = min(abs(dx), abs(dy))
    nstraight = abs(dx) - ndiag if abs(dx) > abs(dy) else abs(dy) - ndiag
    diag = (sx, sy)
    straight = (sx, 0) if abs(dx) > abs(dy) else (0, sy)
    for order in ((diag, ndiag, straight, nstraight), (straight, nstraight, diag, ndiag)):
        d1, n1, d2, n2 = order
        x, y = ax, ay
        cells = []
        ok = True
        for d, n in ((d1, n1), (d2, n2)):
            for _ in range(n):
                nx, ny = x + d[0], y + d[1]
                idx = layer * plane + ny * w + nx
                if not passable(idx):
                    ok = False
                    break
                if d[0] and d[1] and not (passable(layer * plane + y * w + nx) and
                                          passable(layer * plane + ny * w + x)):
                    ok = False
                    break
                cells.append(idx)
                x, y = nx, ny
            if not ok:
                break
        if ok:
            return cells
    return None


def _smooth(grid: Grid, cells: List[int], passable) -> List[int]:
    """Straighten a cell path the way a designer would redraw it: from each
    point, jump as far ahead as a two-piece octilinear path (one straight run
    and one diagonal run) can reach through free cells, so wiggles and
    staircases collapse into at most two segments. Layer changes are kept
    where they are; only runs on a single layer are rewritten."""
    plane = grid.plane
    n = len(cells)
    if n < 3:
        return cells
    out = [cells[0]]
    i = 0
    while i < n - 1:
        layer = cells[i] // plane
        # the last index of this layer run
        end = i
        while end + 1 < n and cells[end + 1] // plane == layer:
            end += 1
        if end == i:                                   # a via step: keep it
            out.append(cells[i + 1])
            i += 1
            continue
        # farthest j reachable with a two-piece path (binary search is unsafe,
        # reachability is not monotone, so walk back from the end)
        j = end
        replaced = None
        while j > i + 1:
            replaced = _two_piece(grid, cells[i], cells[j], passable)
            if replaced is not None:
                break
            j -= 1
        if replaced is None:
            out.append(cells[i + 1])
            i += 1
        else:
            out.extend(replaced)
            i = j
    return out


def _simplify(grid: Grid, cells: List[int]):
    """Cell path -> straight segments per layer plus via positions."""
    points = [grid.unpack(i) for i in cells]
    segments, vias = [], []
    i, n = 0, len(points)
    while i < n - 1:
        layer, x0, y0 = points[i]
        j = i + 1
        if points[j][0] != layer:
            vias.append((grid.nm_x(x0), grid.nm_y(y0)))
            i = j
            continue
        dx, dy = points[j][1] - x0, points[j][2] - y0
        while j + 1 < n and points[j + 1][0] == layer and \
                (points[j + 1][1] - points[j][1], points[j + 1][2] - points[j][2]) == (dx, dy):
            j += 1
        _, x1, y1 = points[j]
        segments.append((layer, grid.nm_x(x0), grid.nm_y(y0), grid.nm_x(x1), grid.nm_y(y1)))
        i = j
    return segments, vias


def _with_pad_entries(grid: Grid, net: NetInfo, cells: List[int], segments):
    """Extend a cell path at both ends: grid cell -> exit point -> pad centre,
    when the end cell is a pad endpoint of this net."""
    out = list(segments)
    for cell, at_start in ((cells[0], True), (cells[-1], False)):
        entry = net.endpoints.get(cell)
        if entry is None:
            continue
        ex, ey, px, py, width = entry
        layer, cx, cy = grid.unpack(cell)
        gx, gy = grid.nm_x(cx), grid.nm_y(cy)
        extra = []
        if (gx, gy) != (ex, ey):
            extra.append((layer, gx, gy, ex, ey, width))
        if (ex, ey) != (px, py):
            extra.append((layer, ex, ey, px, py, width))
        if at_start:
            out = [(l, x2, y2, x1, y1, w) for l, x1, y1, x2, y2, w in reversed(extra)] + out
        else:
            out = out + extra
    return _merge_collinear(out)


def _fold_back(segments):
    """Collapse an excursion: consecutive segments A->B then B->C on one layer
    with C on the line AB behind B become A->C. A route that ran along a
    pad's stub lane to the exit cell past the pad and then took the stub back
    to the pad centre would otherwise double over itself and, once the
    shared copper is written once, leave the piece past the pad hanging."""
    out = []
    for seg in segments:
        out.append(seg)
        while len(out) >= 2:
            a, b = out[-2], out[-1]
            if a[0] != b[0] or (a[3], a[4]) != (b[1], b[2]):
                break
            ux, uy = a[3] - a[1], a[4] - a[2]
            vx, vy = b[3] - b[1], b[4] - b[2]
            if ux * vy - uy * vx != 0 or ux * vx + uy * vy >= 0:
                break                                  # not collinear, or carrying on
            width = max(a[5] if len(a) > 5 else 0, b[5] if len(b) > 5 else 0)
            folded = (a[0], a[1], a[2], b[3], b[4]) + ((width,) if len(a) > 5 or len(b) > 5 else ())
            out.pop(); out.pop()
            if (folded[1], folded[2]) != (folded[3], folded[4]):
                out.append(folded)
    return out


def _merge_collinear(segments):
    """Join consecutive segments that continue in the same direction on the
    same layer with the same width (a stub running straight into its route)."""
    segments = _fold_back(segments)
    merged = []
    for seg in segments:
        if merged:
            prev = merged[-1]
            if (prev[0] == seg[0] and (prev[3], prev[4]) == (seg[1], seg[2])
                    and tuple(prev[5:]) == tuple(seg[5:])):
                ax, ay = prev[3] - prev[1], prev[4] - prev[2]
                bx, by = seg[3] - seg[1], seg[4] - seg[2]
                if ax * by - ay * bx == 0 and ax * bx + ay * by > 0:
                    merged[-1] = tuple(prev[:3]) + (seg[3], seg[4]) + tuple(prev[5:])
                    continue
        merged.append(seg)
    return merged


class Router:
    def __init__(self, problem: RoutingProblem):
        self.p = problem
        self.grid = problem.grid
        self.opts = problem.options
        self.horizontal = [l.horizontal for l in problem.layers]
        self.routable = [l.routable for l in problem.layers]
        # fixed copper as read from the board; the live maps start as copies
        self.fixed = {key: array("i", own) for key, own in problem.owners.items()}
        self.fixed_via_block = bytearray(problem.via_block)
        # cell -> path id, per owner map, for cells whose owner is routed copper
        self.routed = {key: array("i", [0]) * self.grid.size for key in problem.owners}
        self.paths: Dict[int, _Path] = {}
        self.next_pid = 1
        self.rips: Dict[Tuple[int, int, int], int] = {}        # (net, a, b) -> times ripped
        # cell -> extra cost, grown on the cells where a rip-up took one net's
        # route through another's: the next searches of both nets steer clear,
        # so two connections that keep ripping each other up settle apart
        self.history = None
        # union-find over each net's initial clusters, joined by surviving paths
        self.parent: Dict[int, List[int]] = {code: list(range(len(n.clusters)))
                                             for code, n in problem.nets.items()}
        # shove mode: the board's own tracks enter as paths that rip-up may
        # take out of the way; their connections are rerouted like any other
        self.shoved = 0
        for ex in problem.existing:
            net = problem.nets.get(ex.path.net)
            if net is None or not net.clusters:
                continue
            c = max(ex.cluster, 0)
            path = _Path(ex.path.pid, Connection(ex.path.net, c, c, 0, rank=net.rank), ex.path, list(ex.cells))
            path.joins = list(ex.joins)
            path.existing = True
            self.paths[path.pid] = path
            self._claim(path)
            self.next_pid = max(self.next_pid, path.pid + 1)
        for code in {ex.path.net for ex in problem.existing}:
            if code in self.parent:
                self._rebuild_union(code)

    # ---- clusters -------------------------------------------------------

    def _find(self, code: int, i: int) -> int:
        parent = self.parent[code]
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def _rebuild_union(self, code: int) -> None:
        self.parent[code] = list(range(len(self.p.nets[code].clusters)))
        for path in self.paths.values():
            if path.conn.net == code:
                for a, b in path.joins:
                    self.parent[code][self._find(code, a)] = self._find(code, b)

    def _component(self, code: int, i: int) -> Tuple[Set[int], List[Tuple[int, int]]]:
        """All cells and anchors a route may start from / end on for the
        component holding initial cluster i."""
        net = self.p.nets[code]
        root = self._find(code, i)
        cells: Set[int] = set()
        anchors: List[Tuple[int, int]] = []
        for j, cl in enumerate(net.clusters):
            if self._find(code, j) == root:
                cells |= cl.cells
                anchors += cl.anchors
        for path in self.paths.values():
            if path.conn.net == code and self._find(code, path.conn.a) == root:
                cells.update(path.cells)
        return cells, anchors

    # ---- claims -----------------------------------------------------------

    def _claim(self, path: _Path) -> None:
        """Make a path's copper an obstacle (and remember what it claimed)."""
        grid, p, net = self.grid, self.p, self.p.nets[path.conn.net]
        rp = path.path
        for key, owners in p.owners.items():
            width, clearance = key
            cells: List[int] = []
            for seg in rp.segments:
                layer, x1, y1, x2, y2 = seg[:5]
                seg_w = seg[5] if len(seg) > 5 else rp.width
                r = seg_w // 2 + max(clearance, net.rules.clearance) + width // 2 + p.safety
                cells += grid.segment_cells(layer, x1, y1, x2, y2, r)
            r_via = rp.via_diameter // 2 + max(clearance, net.rules.clearance) + width // 2 + p.safety
            for x, y in rp.vias:
                for layer in range(grid.layers):
                    cells += grid.disc_cells(layer, x, y, r_via)
            path.claims[key] = cells
            routed = self.routed[key]
            for idx in cells:
                cur = owners[idx]
                if cur == 0:
                    owners[idx] = net.code
                    routed[idx] = path.pid
                elif cur != net.code and cur != BLOCKED:
                    # overlaps another net's zone: blocked for everyone. It stays
                    # rip-up-able only if that other zone was routed copper too
                    # (routed[idx] already names a path); fixed copper underneath
                    # means ripping this path would not free the cell.
                    owners[idx] = BLOCKED
        r_hole = rp.via_drill // 2 + p.hole_to_hole + p.max_via_drill // 2
        for x, y in rp.vias:
            path.via_cells += grid.disc_cells(0, x, y, r_hole)
        for idx in path.via_cells:
            p.via_block[idx] = 1

    def _rebuild_maps(self) -> None:
        """Fixed copper plus every surviving path, from scratch."""
        for key, owners in self.p.owners.items():
            owners[:] = self.fixed[key]
            self.routed[key][:] = array("i", [0]) * self.grid.size
        self.p.via_block[:] = self.fixed_via_block
        for pid in sorted(self.paths):
            path = self.paths[pid]
            path.claims = {}
            path.via_cells = []
            self._claim(path)

    # ---- routing one connection ------------------------------------------

    def _key(self, conn: Connection) -> Tuple[int, int]:
        net = self.p.nets[conn.net]
        return (conn.width or net.rules.width, net.rules.clearance)

    def _search(self, conn: Connection, via_cost: int, cap: int, soft: bool, cancelled):
        net = self.p.nets[conn.net]
        owners = self.p.owners[self._key(conn)]
        a_cells, a_anchors = self._component(conn.net, conn.a)
        b_cells, b_anchors = self._component(conn.net, conn.b)
        if not a_cells or not b_cells:
            return None
        if len(a_cells) > len(b_cells):
            a_cells, b_cells, b_anchors = b_cells, a_cells, a_anchors
        # pad cells inside another net's clearance zone would put the track end
        # too close to that net; only use them when nothing else is left
        goals = {c for c in b_cells if owners[c] in (0, net.code)} or b_cells
        sources = {c for c in a_cells if owners[c] in (0, net.code)} or a_cells
        width = conn.width or net.rules.width
        # cells to check around a via centre: the barrel beyond the track half
        # width, plus one cell because the check samples cell centres
        via_reach = max(0, int(math.ceil((net.rules.via_diameter - width) / 2 / self.grid.pitch))) + 1
        kwargs = {}
        if soft:
            kwargs = {"routed": self.routed[self._key(conn)],
                      "rippable": lambda pid: self._rippable(pid, conn),
                      "rip_penalty": RIP_PENALTY}
        region = self._neck_region(conn)
        if region:
            kwargs["neck_owners"] = self.p.owners.get((conn.neck, net.rules.clearance), owners)
            kwargs["neck_region"] = region
        return astar.route(self.grid, owners, self.p.via_block, net.code, sources, goals, b_anchors,
                           self.horizontal, via_cost, self.opts.allow_vias, via_reach, cap,
                           self.opts.turn_cost, cancelled, routable=self.routable, via_guard=self.p.via_guard, lane_guard=self.p.lane_guard,
                           history=self.history, **kwargs)

    def _neck_region(self, conn: Connection) -> Set[int]:
        """Cells within neck_length of the connection's fine-pitch exits, on
        every layer: the track stays at the neck width there."""
        if not conn.neck:
            return set()
        grid = self.grid
        net = self.p.nets[conn.net]
        r = int(self.opts.neck_length_mm * 1_000_000 / grid.pitch)
        region: Set[int] = set()
        for cluster in (conn.a, conn.b):
            if not net.clusters[cluster].tight:
                continue
            for cell in net.clusters[cluster].cells:
                if cell not in net.endpoints:
                    continue
                _, cx, cy = grid.unpack(cell)
                for layer in range(grid.layers):
                    base = layer * grid.plane
                    for dy in range(-r, r + 1):
                        for dx in range(-r, r + 1):
                            if dx * dx + dy * dy <= r * r and grid.inside(cx + dx, cy + dy):
                                region.add(base + (cy + dy) * grid.w + cx + dx)
        return region

    def _blockers(self, conn: Connection, cells: List[int]) -> Set[int]:
        """Paths of other nets that a soft route runs through: along its cells
        and, where it changes layer, under the via barrel on every layer."""
        routed = self.routed[self._key(conn)]
        grid = self.grid
        net = self.p.nets[conn.net]
        width = conn.width or net.rules.width
        reach = max(0, int(math.ceil((net.rules.via_diameter - width) / 2 / grid.pitch))) + 1
        spots = list(cells)
        for k in range(1, len(cells)):
            if cells[k] // grid.plane != cells[k - 1] // grid.plane:
                _, cx, cy = grid.unpack(cells[k])
                for dx in range(-reach, reach + 1):
                    for dy in range(-reach, reach + 1):
                        if dx * dx + dy * dy <= reach * reach and grid.inside(cx + dx, cy + dy):
                            for layer in range(grid.layers):
                                spots.append(grid.index(layer, cx + dx, cy + dy))
        out: Set[int] = set()
        for idx in spots:
            pid = routed[idx]
            if pid and pid in self.paths and self.paths[pid].conn.net != conn.net:
                out.add(pid)
        return out

    def _remember_conflict(self, conn: Connection, cells: List[int], blockers: Set[int]) -> None:
        """Raise the history cost of the cells where a soft route ran through
        the paths it is about to rip up: the cells it crossed them on, and
        both ends of a via whose barrel lands on them."""
        routed = self.routed[self._key(conn)]
        grid = self.grid
        net = self.p.nets[conn.net]
        width = conn.width or net.rules.width
        reach = max(0, int(math.ceil((net.rules.via_diameter - width) / 2 / grid.pitch))) + 1
        if self.history is None:
            self.history = array("i", [0]) * grid.size
        hit: Set[int] = {idx for idx in cells if routed[idx] in blockers}
        for k in range(1, len(cells)):
            if cells[k] // grid.plane == cells[k - 1] // grid.plane:
                continue
            _, cx, cy = grid.unpack(cells[k])
            if any(routed[grid.index(layer, cx + dx, cy + dy)] in blockers
                   for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)
                   if dx * dx + dy * dy <= reach * reach and grid.inside(cx + dx, cy + dy)
                   for layer in range(grid.layers)):
                hit.update((cells[k - 1], cells[k]))
        for idx in hit:
            self.history[idx] += HISTORY_STEP

    def _rippable(self, pid: int, for_conn: Connection) -> bool:
        path = self.paths.get(pid)
        if path is None or path.paired:
            return False
        if path.conn.net == for_conn.net:
            return True                       # own net's copper is never in the way
        # a less important group does not push a pair, memory or high-speed
        # route off its line (those were routed first for a reason)
        if self.opts.protect_groups and path.conn.rank < for_conn.rank \
                and self.p.nets[path.conn.net].group in PROTECTED_GROUPS:
            return False
        key = (path.conn.net, path.conn.a, path.conn.b)
        return self.rips.get(key, 0) < self.opts.rip_limit

    def _commit(self, conn: Connection, cells: List[int], on_path) -> _Path:
        net = self.p.nets[conn.net]
        owners = self.p.owners[self._key(conn)]
        own_cells = set(cells)
        region = self._neck_region(conn)
        neck_owners = self.p.owners.get((conn.neck, net.rules.clearance), owners) if region else owners

        def free(idx):
            o = neck_owners[idx] if idx in region else owners[idx]
            return idx in own_cells or o in (0, net.code)

        cells = _smooth(self.grid, cells, free)
        width = conn.width or net.rules.width
        if region and conn.neck:
            # split the path where it leaves the neck region, so the narrow
            # part near the pin and the full-width part are separate segments
            segments, vias = [], []
            run, run_w = [cells[0]], (conn.neck if cells[0] in region else width)
            for c in cells[1:]:
                w_here = conn.neck if c in region else width
                if w_here != run_w:
                    segs, vs = _simplify(self.grid, run + [c])
                    segments += [sg + (run_w,) for sg in segs]
                    vias += vs
                    run, run_w = [c], w_here
                else:
                    run.append(c)
            segs, vs = _simplify(self.grid, run)
            segments += [sg + (run_w,) for sg in segs]
            vias += vs
        else:
            segments, vias = _simplify(self.grid, cells)
        segments = _with_pad_entries(self.grid, net, cells, segments)
        if net.layer_widths:
            # controlled impedance: each layer's own width (neck segments keep theirs)
            segments = [sg if len(sg) > 5 or net.layer_widths.get(sg[0], width) == width
                        else sg + (net.layer_widths[sg[0]],) for sg in segments]
        rp = RoutedPath(net.code, width, segments, vias, net.rules.via_diameter,
                        net.rules.via_drill, pid=self.next_pid)
        self.next_pid += 1
        path = _Path(rp.pid, conn, rp, cells)
        self.paths[rp.pid] = path
        self._claim(path)
        self.parent[conn.net][self._find(conn.net, conn.a)] = self._find(conn.net, conn.b)
        if on_path:
            on_path(rp)
        return path

    def _rip(self, pids: List[int], on_remove) -> List[Connection]:
        conns = []
        for pid in pids:
            path = self.paths.pop(pid, None)
            if path is None:
                continue
            if path.existing:
                # a board piece may have joined several clusters: each join
                # becomes a connection to route again
                self.shoved += 1
                for a, b in path.joins:
                    if a != b:
                        conns.append(Connection(path.conn.net, a, b, 0, rank=path.conn.rank))
            else:
                conns.append(path.conn)
            key = (path.conn.net, path.conn.a, path.conn.b)
            self.rips[key] = self.rips.get(key, 0) + 1
            if on_remove:
                on_remove(pid)
        for code in {c.net for c in conns}:
            self._rebuild_union(code)
        self._rebuild_maps()
        return conns

    # ---- differential pairs ----------------------------------------------

    def _exit_of(self, code: int, cluster: int) -> Optional[int]:
        """A pad exit cell of a cluster (the cell a route starts from)."""
        net = self.p.nets[code]
        cells = net.clusters[cluster].cells
        for c in cells:
            if c in net.endpoints:
                return c
        return next(iter(cells), None)

    def _try_pair(self, ca: Connection, cb: Connection, via_cost: int, cap: int, on_path, cancelled) -> bool:
        """Route two connections of a differential pair as one coupled track:
        a centreline as wide as both tracks and their gap is searched, then
        split into the two tracks offset to either side, each joined to its
        own pad exits. Falls back to separate routing when the pair cannot
        start or end side by side."""
        p, grid = self.p, self.grid
        na, nb = p.nets[ca.net], p.nets[cb.net]
        self.pair_log = getattr(self, 'pair_log', [])
        if self._find(ca.net, ca.a) == self._find(ca.net, ca.b) or self._find(cb.net, cb.a) == self._find(cb.net, cb.b):
            self.pair_log.append((na.name, 'already joined'))
            return False
        # match the ends: B's clusters that share footprints with A's
        a1, a2 = na.clusters[ca.a], na.clusters[ca.b]
        b1, b2 = nb.clusters[cb.a], nb.clusters[cb.b]
        if not (a1.refs & b1.refs) and (a1.refs & b2.refs):
            b1, b2 = b2, b1
            cb_a, cb_b = cb.b, cb.a
        else:
            cb_a, cb_b = cb.a, cb.b
        ea1, ea2 = self._exit_of(ca.net, ca.a), self._exit_of(ca.net, ca.b)
        eb1, eb2 = self._exit_of(cb.net, cb_a), self._exit_of(cb.net, cb_b)
        if None in (ea1, ea2, eb1, eb2):
            self.pair_log.append((na.name, 'no exits'))
            return False
        la, xa1, ya1 = grid.unpack(ea1)
        lb, xb1, yb1 = grid.unpack(eb1)
        la2, xa2, ya2 = grid.unpack(ea2)
        lb2, xb2, yb2 = grid.unpack(eb2)
        if la != lb or la2 != lb2:
            self.pair_log.append((na.name, 'exits on different layers'))
            return False
        w, gap = na.pair_width or na.rules.width, na.pair_gap
        W = 2 * w + gap
        key = (W, na.rules.clearance)
        owners = p.owners.get(key)
        if owners is None:
            self.pair_log.append((na.name, 'no owner map for pair width'))
            return False
        d = (w + gap) / 2.0

        span = math.hypot(xa2 - xa1, ya2 - ya1)          # cells between the two ends
        if span * grid.pitch < 3_000_000:
            self.pair_log.append((na.name, 'ends closer than 3 mm: routed separately'))
            return False
        walk = max(1, min(int(4_000_000 / grid.pitch), int(span * 0.45)))

        def starts(xa, ya, xb, yb, layer, code, cell):
            """Midpoint cells between the two exits, walked outward along the
            escape direction, so the pair can converge after leaving the pins."""
            ex, ey, px, py, _ = p.nets[code].endpoints.get(cell, (grid.nm_x(xa), grid.nm_y(ya), grid.nm_x(xa), grid.nm_y(ya), 0))
            dx, dy = grid.nm_x(xa) - px, grid.nm_y(ya) - py
            norm = math.hypot(dx, dy) or 1.0
            ux, uy = dx / norm, dy / norm
            out = []
            # walk out of the pin field (up to 4 mm, never past the middle);
            # one or two cells either side too, since the midpoint of two
            # exits need not be a cell centre. Each cell comes with its k.
            for k in range(0, walk):
                for lateral in (0, 1, -1, 2, -2):
                    mx = int(round((xa + xb) / 2 + ux * k - uy * lateral))
                    my = int(round((ya + yb) / 2 + uy * k + ux * lateral))
                    if grid.inside(mx, my):
                        out.append((k, grid.index(layer, mx, my)))
            return out

        def usable(cands):
            return [(k, c) for k, c in cands if owners[c] in (0, ca.net, cb.net)]

        s_list = usable(starts(xa1, ya1, xb1, yb1, la, ca.net, ea1))
        g_list = usable(starts(xa2, ya2, xb2, yb2, la2, ca.net, ea2))
        if not s_list or not g_list:
            self.pair_log.append((na.name, f'no coupled start/goal (starts {len(s_list)}, goals {len(g_list)})'))
            return False
        # couple as early as possible: starting further out costs extra, and
        # only the nearest usable goal rows are offered
        s_costs = {c: k * astar.STRAIGHT * 3 for k, c in s_list}
        kmin = min(k for k, _ in g_list)
        g_cells = {c for k, c in g_list if k <= kmin + 2}
        s_cells = set(s_costs)
        anchors = [grid.unpack(c)[1:] for c in list(g_cells)[:1]]
        cells = astar.route(grid, owners, p.via_block, ca.net, s_cells, g_cells, anchors,
                            self.horizontal, via_cost, False, 0, cap, self.opts.turn_cost, cancelled,
                            also_ok=cb.net, source_costs=s_costs, routable=self.routable, via_guard=p.via_guard, lane_guard=p.lane_guard)
        if not cells or len(cells) < 2:
            self.pair_log.append((na.name, 'no coupled path'))
            return False
        own_cells = set(cells)
        cells = _smooth(grid, cells, lambda idx: idx in own_cells or owners[idx] in (0, ca.net, cb.net))
        segments, _ = _simplify(grid, cells)
        if not segments:
            return False
        points = [(segments[0][1], segments[0][2])] + [(sg[3], sg[4]) for sg in segments]
        layer = segments[0][0]
        # which side of the centreline each net runs on, decided at the start
        # and checked at the end (a twist would need a crossing)
        def side_of(px, py, i):
            ux, uy = points[i + 1][0] - points[i][0], points[i + 1][1] - points[i][1]
            vx, vy = px - points[i][0], py - points[i][1]
            return 1 if ux * vy - uy * vx > 0 else -1
        sa = side_of(grid.nm_x(xa1), grid.nm_y(ya1), 0)
        sa_end = side_of(grid.nm_x(xa2), grid.nm_y(ya2), len(points) - 2)
        if sa != sa_end:
            self.pair_log.append((na.name, 'pair would cross'))
            return False
        paths = []
        for net, conn, side, e1, e2, cl_a, cl_b in ((na, ca, sa, ea1, ea2, ca.a, ca.b),
                                                   (nb, cb, -sa, eb1, eb2, cb_a, cb_b)):
            poly = _offset_polyline(points, d, side)
            segs = []
            for end_cell, at_start in ((e1, True), (e2, False)):
                ex, ey, px, py, sw = net.endpoints.get(end_cell, (None,) * 5)
                l_, cx, cy = grid.unpack(end_cell)
                gx, gy = grid.nm_x(cx), grid.nm_y(cy)
                entry = []
                if ex is not None:
                    if (ex, ey) != (px, py) and (ex, ey) != (gx, gy):
                        entry += [(layer, px, py, ex, ey, sw), (layer, ex, ey, gx, gy, sw)]
                    elif (px, py) != (gx, gy):
                        entry.append((layer, px, py, gx, gy, sw))
                join_to = poly[0] if at_start else poly[-1]
                if (gx, gy) != join_to:
                    # straight on along the escape, then a 45-degree step onto
                    # the coupled run (a designer's dog-leg, not a long slant)
                    if ex is not None and (gx, gy) != (px, py):
                        ux, uy = gx - px, gy - py
                    else:
                        ux, uy = join_to[0] - gx, join_to[1] - gy
                    norm = math.hypot(ux, uy) or 1.0
                    ux, uy = ux / norm, uy / norm
                    along = (join_to[0] - gx) * ux + (join_to[1] - gy) * uy
                    perp = math.hypot((join_to[0] - gx) - along * ux, (join_to[1] - gy) - along * uy)
                    straight = along - perp
                    if straight > grid.pitch:
                        qx, qy = int(round(gx + ux * straight)), int(round(gy + uy * straight))
                        entry.append((layer, gx, gy, qx, qy, w))
                        entry.append((layer, qx, qy, join_to[0], join_to[1], w))
                    else:
                        entry.append((layer, gx, gy, join_to[0], join_to[1], w))
                if at_start:
                    segs = entry + segs
                else:
                    segs = segs + [(l, x2, y2, x1, y1, ww) for l, x1, y1, x2, y2, ww in reversed(entry)]
                if at_start:
                    segs += [(layer, poly[i][0], poly[i][1], poly[i + 1][0], poly[i + 1][1], w) for i in range(len(poly) - 1)]
            rp = RoutedPath(net.code, w, _merge_collinear(segs), [], net.rules.via_diameter, net.rules.via_drill,
                            pid=self.next_pid)
            self.next_pid += 1
            paths.append((rp, conn, cl_a, cl_b))
        # the two halves must keep their clearance from each other everywhere
        # (joins and mitred corners can pinch); otherwise route them apart
        need = na.rules.clearance + w
        for sa_ in paths[0][0].segments:
            for sb_ in paths[1][0].segments:
                if _segment_distance(sa_[1:5], sb_[1:5]) < need:
                    self.pair_log.append((na.name, 'halves would pinch: routed separately'))
                    self.next_pid -= 2
                    return False
        for rp, conn, cl_a, cl_b in paths:
            path = _Path(rp.pid, conn, rp, cells, paired=True)
            self.paths[rp.pid] = path
            self._claim(path)
            self.parent[conn.net][self._find(conn.net, cl_a)] = self._find(conn.net, cl_b)
            if on_path:
                on_path(rp)
        return True

    def _try(self, conn: Connection, via_cost: int, cap: int, on_path, cancelled) -> bool:
        if self._find(conn.net, conn.a) == self._find(conn.net, conn.b):
            return True                        # already joined through other paths
        cells = self._search(conn, via_cost, cap, False, cancelled)
        if not cells:
            return False
        self._commit(conn, cells, on_path)
        return True

    def _why_failed(self, conn: Connection) -> dict:
        """What surrounds a failed connection's ends: how much room there is
        and which nets (or fixed copper) own the cells around each exit."""
        p, grid = self.p, self.grid
        net = p.nets[conn.net]
        own = p.owners[self._key(conn)]
        ends = []
        for cluster in (conn.a, conn.b):
            cell = self._exit_of(conn.net, cluster)
            if cell is None:
                continue
            layer, cx, cy = grid.unpack(cell)
            around: Dict[str, int] = {}
            free = total = 0
            for dx in range(-5, 6):
                for dy in range(-5, 6):
                    if not grid.inside(cx + dx, cy + dy):
                        continue
                    total += 1
                    o = own[grid.index(layer, cx + dx, cy + dy)]
                    if o in (0, net.code):
                        free += 1
                    else:
                        name = "fixed copper / keepout" if o == BLOCKED else p.nets[o].name if o in p.nets else f"net {o}"
                        around[name] = around.get(name, 0) + 1
            ends.append({"refs": sorted(net.clusters[cluster].refs),
                         "at_mm": [round(grid.nm_x(cx) / 1e6, 3), round(grid.nm_y(cy) / 1e6, 3)],
                         "layer": p.layers[layer].name,
                         "free_cells_around": f"{free}/{total}",
                         "blocked_by": dict(sorted(around.items(), key=lambda kv: -kv[1])[:5])})
        return {"net": net.name, "width_mm": round((conn.width or net.rules.width) / 1e6, 3), "ends": ends}

    def _cells_of_exit(self, rp: RoutedPath) -> List[int]:
        """The exit cell a fanout path started from (its first segment ends there)."""
        grid = self.grid
        out = []
        for seg in rp.segments[:2]:
            layer, x1, y1, x2, y2 = seg[:5]
            for x, y in ((x1, y1), (x2, y2)):
                cx, cy = grid.cell_x(x), grid.cell_y(y)
                if grid.inside(cx, cy):
                    out.append(grid.index(layer, cx, cy))
        return out

    def _tie_to_net(self, code: int, cell: int, cancelled) -> Optional[RoutedPath]:
        """Route from a pad exit to the nearest copper of the same net within
        a few millimetres: what a designer does when a GND pad has no room
        for its own via."""
        p, grid = self.p, self.grid
        net = p.nets[code]
        owners = p.owners[net.rules.key]
        layer, cx, cy = grid.unpack(cell)
        reach = int(6_000_000 / grid.pitch)                    # 6 mm
        x0, x1 = max(0, cx - reach), min(grid.w - 1, cx + reach)
        y0, y1 = max(0, cy - reach), min(grid.h - 1, cy + reach)
        # Real copper of the net within reach (not its clearance zone). Copper
        # that is already tied to the plane comes first: routed centrelines,
        # fanout and stitching vias. Other pads' exits (the neighbouring pin
        # of a QFP, half a millimetre away) are the fallback: a pad-to-pad
        # link, the way a designer bridges two adjacent power pins.
        wired: Set[int] = set()
        for path in self.paths.values():
            if path.conn.net == code:
                wired.update(path.cells)
                for x, y in path.path.vias:
                    vx, vy = grid.cell_x(x), grid.cell_y(y)
                    if grid.inside(vx, vy):
                        wired.add(grid.index(layer, vx, vy))
        own_cluster: Set[int] = set()
        others: Set[int] = set()
        for cl in net.clusters:
            if cell in cl.cells:
                own_cluster |= cl.cells
            else:
                others |= cl.cells

        def in_reach(copper: Set[int]) -> Set[int]:
            out: Set[int] = set()
            for idx in copper:
                l_, gx, gy = grid.unpack(idx)
                if l_ != layer or idx in own_cluster or abs(gx - cx) + abs(gy - cy) <= 2:
                    continue
                if x0 <= gx <= x1 and y0 <= gy <= y1:
                    out.add(idx)
            return out

        cells = None
        self._tie_wired = False
        for stage, goals in enumerate((in_reach(wired), in_reach(others))):
            if not goals:
                continue
            self._tie_wired = stage == 0
            anchors = [grid.unpack(g)[1:] for g in list(goals)[:8]]
            cells = astar.route(grid, owners, p.via_block, code, {cell}, goals, anchors, self.horizontal,
                                self.opts.via_cost, False, 0, self.opts.max_expansions // 4, self.opts.turn_cost,
                                cancelled, routable=self.routable, via_guard=p.via_guard, lane_guard=p.lane_guard)
            if cells and len(cells) >= 2:
                break
            cells = None
        if not cells:
            return None
        own_cells = set(cells)
        cells = _smooth(grid, cells, lambda idx: idx in own_cells or owners[idx] in (0, code))
        self._tie_cells = set(cells)
        segments, _ = _simplify(grid, cells)
        segments = _with_pad_entries(grid, net, cells, segments)
        rp = RoutedPath(code, net.rules.width, segments, [], net.rules.via_diameter, net.rules.via_drill,
                        pid=self.next_pid)
        self.next_pid += 1
        path = _Path(rp.pid, Connection(code, 0, 0, 0), rp, cells)
        self._claim(path)
        return rp

    # ---- fanout vias ------------------------------------------------------

    def _fanout_via(self, code: int, cell: int) -> Optional[RoutedPath]:
        """A stub from the pad through its exit cell to a via that ties the
        pad to the net's plane: straight out along the escape first, else the
        nearest spot within 1.5 mm where the barrel and the run to it fit."""
        p, grid = self.p, self.grid
        net = p.nets[code]
        own = p.owners[net.rules.key]
        ex, ey, px, py, stub_w = net.endpoints[cell]
        layer, ecx, ecy = grid.unpack(cell)
        gx, gy = grid.nm_x(ecx), grid.nm_y(ecy)
        reach = max(0, int(math.ceil((net.rules.via_diameter - net.rules.width) / 2 / grid.pitch))) + 1
        dx, dy = gx - px, gy - py
        norm = math.hypot(dx, dy) or 1.0
        step = (dx / norm, dy / norm)

        run_pts = [None]                          # the exit-to-via run fits() accepted

        def fits(vx: int, vy: int) -> bool:
            if not grid.inside(vx, vy) or p.via_block[vy * grid.w + vx]:
                return False
            if p.via_guard is not None and p.via_guard[vy * grid.w + vx] not in (0, code):
                return False
            for ddx in range(-reach, reach + 1):
                for ddy in range(-reach, reach + 1):
                    if ddx * ddx + ddy * ddy > reach * reach:
                        continue
                    if not grid.inside(vx + ddx, vy + ddy):
                        return False
                    for li in range(grid.layers):
                        if own[grid.index(li, vx + ddx, vy + ddy)] not in (0, code):
                            return False
            # the run from the exit to the via at 0/45/90 degrees: the jog at
            # whichever end leaves it free
            for at_end in (False, True):
                pts = jog(gx, gy, grid.nm_x(vx), grid.nm_y(vy), at_end=at_end)
                if all(own[i] in (0, code)
                       for k in range(len(pts) - 1)
                       for i in grid.segment_cells(layer, pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1],
                                                   grid.pitch // 2)):
                    run_pts[0] = pts
                    return True
            return False

        candidates = [(ecx + int(round(step[0] * k)), ecy + int(round(step[1] * k))) for k in range(0, 6)]
        radius = max(2, int(1_500_000 / grid.pitch))
        ring = sorted(((ddx * ddx + ddy * ddy, ecx + ddx, ecy + ddy)
                       for ddx in range(-radius, radius + 1) for ddy in range(-radius, radius + 1)
                       if 0 < ddx * ddx + ddy * ddy <= radius * radius))
        candidates += [(x, y) for _, x, y in ring]
        seen = set()
        for vx, vy in candidates:
            if (vx, vy) in seen:
                continue
            seen.add((vx, vy))
            if fits(vx, vy):
                break
        else:
            return None
        vxn, vyn = grid.nm_x(vx), grid.nm_y(vy)
        segments = []
        if (ex, ey) != (px, py) and (ex, ey) != (gx, gy):
            segments += [(layer, px, py, ex, ey, stub_w), (layer, ex, ey, gx, gy, stub_w)]
        else:
            segments.append((layer, px, py, gx, gy, stub_w))
        if (vxn, vyn) != (gx, gy):
            pts = run_pts[0] or jog(gx, gy, vxn, vyn)
            segments += [(layer, pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1], stub_w) for k in range(len(pts) - 1)]
        rp = RoutedPath(code, net.rules.width, _merge_collinear(segments), [(vxn, vyn)],
                        net.rules.via_diameter, net.rules.via_drill, pid=self.next_pid)
        self.next_pid += 1
        fake = Connection(code, 0, 0, 0)
        path = _Path(rp.pid, fake, rp, [cell])
        self._claim(path)                       # vias and stubs become obstacles for later fanouts
        return rp

    def _fanout_search(self, code: int, cell: int, cancelled, starts: Optional[Set[int]] = None,
                       goals: Optional[Set[int]] = None) -> Optional[RoutedPath]:
        """When no via fits next to the pad: a short search from the exit
        (or from every cell of a tie the pad is on) that may drop a via
        anywhere within 3 mm where the net's pour lies under it (the via lands
        on the pour layer, the refilled pour joins it)."""
        p, grid = self.p, self.grid
        goals = goals or p.fanout_goals.get(cell)
        if not goals:
            return None
        net = p.nets[code]
        owners = p.owners[net.rules.key]
        layer, cx, cy = grid.unpack(cell)
        goals = {g for g in goals if owners[g] in (0, code)}
        if not goals:
            return None
        anchors = [(cx, cy)]
        via_reach = max(0, int(math.ceil((net.rules.via_diameter - net.rules.width) / 2 / grid.pitch))) + 1
        cells = astar.route(grid, owners, p.via_block, code, starts or {cell}, goals, anchors, self.horizontal,
                            max(5, self.opts.via_cost // 3), True, via_reach, self.opts.max_expansions // 4,
                            self.opts.turn_cost, cancelled, routable=self.routable,
                            via_guard=p.via_guard, lane_guard=p.lane_guard)
        if not cells or len(cells) < 2:
            return None
        segments, vias = _simplify(grid, cells)
        if not vias:
            return None                             # it never left the pad's layer
        segments = [s for s in segments if s[0] == layer]   # the pour takes over after the via
        segments = _with_pad_entries(grid, net, cells, segments)
        rp = RoutedPath(code, net.rules.width, segments, vias, net.rules.via_diameter, net.rules.via_drill,
                        pid=self.next_pid)
        self.next_pid += 1
        path = _Path(rp.pid, Connection(code, 0, 0, 0), rp, cells)
        self._claim(path)
        return rp

    def _stitch_via(self, code: int, cx: int, cy: int) -> Optional[RoutedPath]:
        """A lone via of a pour net at a grid spot, if the barrel and hole fit
        on every layer; the refilled pours connect it on both sides."""
        p, grid = self.p, self.grid
        net = p.nets[code]
        own = p.owners[net.rules.key]
        reach = max(0, int(math.ceil((net.rules.via_diameter - net.rules.width) / 2 / grid.pitch))) + 1
        if not grid.inside(cx, cy) or p.via_block[cy * grid.w + cx]:
            return None
        if p.via_guard is not None and p.via_guard[cy * grid.w + cx] not in (0, code):
            return None
        for ddx in range(-reach, reach + 1):
            for ddy in range(-reach, reach + 1):
                if ddx * ddx + ddy * ddy > reach * reach:
                    continue
                if not grid.inside(cx + ddx, cy + ddy):
                    return None
                for li in range(grid.layers):
                    if own[grid.index(li, cx + ddx, cy + ddy)] not in (0, code):
                        return None
        x, y = grid.nm_x(cx), grid.nm_y(cy)
        rp = RoutedPath(code, net.rules.width, [], [(x, y)], net.rules.via_diameter, net.rules.via_drill,
                        pid=self.next_pid)
        self.next_pid += 1
        path = _Path(rp.pid, Connection(code, 0, 0, 0), rp, [grid.index(0, cx, cy)])
        self._claim(path)
        return rp

    # ---- the whole run --------------------------------------------------

    def run(self, progress: Optional[ProgressFn] = None, on_path: Optional[PathFn] = None,
            on_remove: Optional[RemoveFn] = None, cancelled: Optional[Callable[[], bool]] = None) -> RouteResult:
        opts = self.opts
        result = RouteResult()
        started = time.time()
        # the routing groups in their order (pairs, memory, high speed, clocks,
        # power, the rest); within a group fine-pitch escapes first (they have
        # the least room), then shortest first
        queue = sorted(self.p.connections, key=lambda c: (c.rank, c.priority, c.estimate))
        total = len(queue)
        failed: List[Connection] = []
        done = 0

        def report(name, stage=""):
            # the stage goes in front of the net name so a status line (or the
            # dialog log) shows which phase of the run is working, not only
            # the counter that stands still through the rip-up rounds
            if progress:
                progress(done, total, len(self.paths), len(failed), f"{stage}{name}")

        deadline = started + opts.time_limit_s if opts.time_limit_s and opts.time_limit_s > 0 else None

        def late():
            if deadline is not None and time.time() > deadline:
                result.timed_out = True
                return True
            return False

        user_cancel = cancelled

        def halted():
            return bool(user_cancel and user_cancel())

        def stop():
            return halted() or late()

        # The searches check this every few thousand expansions: the time
        # limit then also ends a long rip-up or last-chance search midway,
        # not only between connections.
        cancelled = stop

        # 1. plain pass; a differential pair's two connections go together
        partners = {}
        for conn in queue:
            if conn.pair:
                partners.setdefault(conn.pair, []).append(conn)
        handled = set()
        self.pairs_routed = 0
        for conn in queue:
            if id(conn) in handled:
                continue
            if stop():
                if self._find(conn.net, conn.a) != self._find(conn.net, conn.b):
                    failed.append(conn)               # out of time: left open, reported as such
                continue
            mates = partners.get(conn.pair, []) if conn.pair else []
            if len(mates) == 2:
                ca, cb = mates
                handled.add(id(ca)); handled.add(id(cb))
                if self._try_pair(ca, cb, opts.via_cost, opts.max_expansions, on_path, cancelled):
                    self.pairs_routed += 1
                    done += 2
                    report(self.p.nets[ca.net].name)
                    continue
                for c in (ca, cb):                 # fall back to separate routes
                    if not self._try(c, opts.via_cost, opts.max_expansions, on_path, cancelled):
                        failed.append(c)
                    done += 1
                report(self.p.nets[ca.net].name)
                continue
            if not self._try(conn, opts.via_cost, opts.max_expansions, on_path, cancelled):
                failed.append(conn)
            done += 1
            report(self.p.nets[conn.net].name)

        # 2. rip-up rounds
        rounds = opts.rip_rounds if opts.rip_up else 0
        for rnd in range(rounds):
            if not failed or stop():
                break
            report(f"{len(failed)} open", f"rip-up round {rnd + 1}/{rounds}: ")
            still: List[Connection] = []
            progressed = False
            for conn in failed:
                if stop():
                    still.append(conn)
                    continue
                if self._find(conn.net, conn.a) == self._find(conn.net, conn.b):
                    continue
                cells = self._search(conn, opts.via_cost, opts.max_expansions * 2, True, cancelled)
                if not cells:
                    still.append(conn)
                    continue
                blockers = sorted(self._blockers(conn, cells))
                if len(blockers) > MAX_BLOCKERS:
                    still.append(conn)
                    continue
                self._remember_conflict(conn, cells, set(blockers))
                ripped = self._rip(blockers, on_remove)
                result.ripped += len(ripped)
                if self._try(conn, opts.via_cost, opts.max_expansions * 2, on_path, cancelled):
                    progressed = True
                else:
                    still.append(conn)
                for other in ripped:
                    if not self._try(other, opts.via_cost, opts.max_expansions, on_path, cancelled):
                        still.append(other)
                report(self.p.nets[conn.net].name, f"rip-up round {rnd + 1}/{rounds}: ")
            failed = still
            if not progressed:
                break

        # 3. last chance with a bigger budget and cheaper vias
        if failed and opts.retry_failed and not stop():
            still = []
            report(f"{len(failed)} open", "last chance, bigger budget: ")
            for conn in failed:
                if stop():
                    still.append(conn)
                    continue
                if not self._try(conn, max(5, opts.via_cost // 3), opts.max_expansions * 3, on_path, cancelled):
                    still.append(conn)
                report(self.p.nets[conn.net].name, "last chance: ")
            failed = still

        # 4. fanout vias for pour nets, in whatever room is left (the time
        # limit does not cut the pour stages: they are bounded and quick)
        fanout_paths: List[RoutedPath] = []
        fanned: Set[int] = set()                       # primary exit cells of pads that got a via
        if not halted():
            for i, (code, cell) in enumerate(self.p.fanout):
                if halted():
                    break
                if i % 10 == 0:
                    report(f"{i}/{len(self.p.fanout)}", "fanout vias ")
                for exit_cell in [cell] + self.p.fanout_alt.get(cell, []):
                    rp = self._fanout_via(code, exit_cell)
                    if rp is not None:
                        fanout_paths.append(rp)
                        fanned.add(cell)
                        if on_path:
                            on_path(rp)
                        break
                else:
                    rp = self._fanout_search(code, cell, user_cancel)
                    if _DEBUG:
                        print(f"[fanout-search] net {self.p.nets[code].name} cell {self.grid.unpack(cell)} goals {len(self.p.fanout_goals.get(cell, ()))} -> {'via' if rp else 'none'}", flush=True)
                    if rp is not None:
                        fanout_paths.append(rp)
                        fanned.add(cell)
                        if on_path:
                            on_path(rp)

        # 5. stitching vias on the pour nets, wherever they still fit
        stitch_paths: List[RoutedPath] = []
        if not halted():
            n_spots = sum(len(v) for v in self.p.stitch.values())
            for code, spots in self.p.stitch.items():
                if halted():
                    break
                report(f"{len(stitch_paths)} placed of {n_spots} sites", "stitching vias: ")
                for cx, cy in spots:
                    rp = self._stitch_via(code, cx, cy)
                    if rp is not None:
                        stitch_paths.append(rp)
                        if on_path:
                            on_path(rp)

        # 6. pour-net pads that got no via: a short track to the nearest
        # copper of their net (a via, another pad, a stub) instead
        tied_paths: List[RoutedPath] = []
        if not halted():
            for code, cell in self.p.fanout:
                if cell in fanned:
                    continue
                if halted():
                    break
                report(self.p.nets[code].name, "tying pour pads: ")
                for exit_cell in [cell] + self.p.fanout_alt.get(cell, []):
                    rp = self._tie_to_net(code, exit_cell, user_cancel)
                    if _DEBUG:
                        print(f"[tie] net {self.p.nets[code].name} exit {self.grid.unpack(exit_cell)} -> {'wired' if rp and self._tie_wired else 'pad' if rp else 'none'}", flush=True)
                    if rp is not None:
                        tied_paths.append(rp)
                        if on_path:
                            on_path(rp)
                        # a tie to another pad that has no via either leaves an
                        # island: look for a via spot from anywhere along it
                        if not self._tie_wired:
                            tie_cells = set(self._tie_cells)
                            # within 3 mm of any pad the tie touches
                            union: Set[int] = set()
                            for pc, c in self.p.fanout:
                                if pc == code and (c == cell or c in tie_cells or
                                                   any(a in tie_cells for a in self.p.fanout_alt.get(c, []))):
                                    union |= self.p.fanout_goals.get(c, set())
                            rp2 = self._fanout_search(code, cell, user_cancel, starts=tie_cells, goals=union)
                            if _DEBUG:
                                print(f"[tie-island] net {self.p.nets[code].name} cell {self.grid.unpack(cell)} tie cells {len(tie_cells)} union goals {len(union)} -> {'via' if rp2 else 'none'}", flush=True)
                            if rp2 is not None:
                                tied_paths.append(rp2)
                                fanned.add(cell)
                                if on_path:
                                    on_path(rp2)
                        break
        fanout_paths += tied_paths

        result.cancelled = halted()
        result.failed = [c for c in failed if self._find(c.net, c.a) != self._find(c.net, c.b)]
        # the same connection may be queued twice (a pair member that failed twice)
        seen: Set[int] = set()
        result.failed = [c for c in result.failed if not (id(c) in seen or seen.add(id(c)))]
        result.failed_detail = [self._why_failed(c) for c in result.failed]
        new_paths = [self.paths[pid] for pid in sorted(self.paths) if not self.paths[pid].existing]
        result.paths = [path.path for path in new_paths] + fanout_paths + stitch_paths
        result.stitched = len(stitch_paths)
        result.routed = len(new_paths)
        result.shoved = self.shoved
        result.fanout = len(fanout_paths)
        result.pairs = getattr(self, "pairs_routed", 0)
        result.pair_log = getattr(self, "pair_log", [])
        result.seconds = time.time() - started
        return result


def run(problem: RoutingProblem, progress: Optional[ProgressFn] = None,
        on_path: Optional[PathFn] = None, cancelled: Optional[Callable[[], bool]] = None,
        on_remove: Optional[RemoveFn] = None) -> RouteResult:
    return Router(problem).run(progress=progress, on_path=on_path, on_remove=on_remove, cancelled=cancelled)
