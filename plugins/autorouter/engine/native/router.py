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
import dataclasses
import math
import time
from array import array
from typing import Callable, Dict, List, Optional, Set, Tuple

import os

from . import astar
from .grid import BLOCKED, BODY_LANE, Grid, jog
from .problem import Cluster, Connection, NetInfo, RoutedPath, RoutingProblem

ProgressFn = Callable[[int, int, int, int, str], None]    # done, total, routed, failed, net name
PathFn = Callable[[RoutedPath], None]
RemoveFn = Callable[[int], None]                            # path id
_DEBUG = bool(os.environ.get("AUTOROUTER_DEBUG"))
OUTLET_NM = 600_000              # a pin's outlet: how far past its exit other copper keeps off (extract)
RIP_PENALTY = 400                                           # per cell, in cost units (40 straight cells)
HISTORY_STEP = 100                                          # per rip-up, on each contested cell (10 straight cells)
MAX_BLOCKERS = 10
# Why a differential pair is connected directly instead of as a coupled run.
PAIR_EXCEPTIONS = {
    "short": "ends too close together: coupling is not needed",
    "joined": "already connected through other copper",
    "no_exits": "a pad has no exit to start a coupled run from",
    "layers": "the ends leave on different layers",
    "no_map": "no clearance map for the pair width",
    "no_start": "no room to start or end side by side",
    "no_path": "no room for a coupled run",
    "cross": "the pins are in opposite order at the two ends (the lines would cross)",
    "pinch": "the two lines would come too close at a corner",
    "blocked": "the coupled lines' pad entries would run into other copper",
}
PAIR_VIA_SPAN_NM = 8_000_000   # a coupled run shorter than this stays on one layer (two via pairs are not worth it)
PAIR_VIA_STRETCH_NM = 2_000_000  # a coupled stretch between two via pairs is at least this long
LOOSE_TRIM_NM = 1_000_000  # a coupled run finished single-ended at one end stops this far short of it
LOOSE_TRIM_STEPS = (1, 1.5, 2, 3, 4)
PAIR_APPROACH_NM = 2_000_000     # in front of a pair's pins, other pairs' runs pay to pass (the pair's way in)
PAIR_APPROACH_HALF_NM = 300_000   # ...this far either side of each pin's outlet line
PAIR_EARLY_SWAP_NM = 50_000_000   # a pair run this long may swap / end single-ended in the first pass   # ...or this many times as far, when the lines would still touch the pins
PAIR_BUDGET = 8          # a pair's share of the per-connection time: two lines, several runs tried (layers, swaps)
PAIR_UNNECESSARY = ("short", "joined")          # no skew tuning or coupling rules for these either
PROTECTED_GROUPS = ("pairs", "memory", "high_speed")          # routing groups lower groups may not rip up
# A route with vias is tried again on one outer layer alone; the flat route
# wins when it is at most this times the straight distance between the ends
# (plus the slack, for the pad stubs). A free top-layer corridor then beats
# two vias and an inner-layer run, without a higher via cost everywhere.
FLAT_DETOUR = 1.25
FLAT_SLACK_NM = 2_000_000
FLAT_MIN_EXPANSIONS = 20_000


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
        self.pair_exceptions: List[dict] = []            # pairs connected directly instead of coupled, and why
        self.direct_links = 0                            # connections joined pad to pad before the search


class _Path:
    """A routed connection and everything needed to undo it."""
    def __init__(self, pid: int, conn: Connection, path: RoutedPath, cells: List[int], paired: bool = False):
        self.pid, self.conn, self.path, self.cells = pid, conn, path, cells
        self.claims: Dict[Tuple[int, int], List[int]] = {}      # owner-map key -> claimed cells
        self.via_cells: List[int] = []
        self.joins: List[Tuple[int, int]] = [(conn.a, conn.b)]  # cluster pairs this path connects
        self.existing = False                                   # copper that was on the board already
        self.pad_exits: List[List[int]] = []                    # existing piece: exit cells per pad it touches
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


def _necked_end(segments, width: int, at_start: bool, neck_nm: int, end):
    """The first (or last) segment of a path, narrowed to `width` for up to
    neck_nm from its pad end `end` (layer, x, y) and split there if it is
    longer. Segments that carry their own width are left alone."""
    if not segments:
        return segments
    seg = segments[0] if at_start else segments[-1]
    if len(seg) > 5:
        return segments
    layer, x1, y1, x2, y2 = seg
    if not at_start:
        x1, y1, x2, y2 = x2, y2, x1, y1                  # walk from the pad end
    if (layer, x1, y1) != end:
        return segments                                  # the path leaves the pad cell by a via
    length = math.hypot(x2 - x1, y2 - y1)
    if length <= neck_nm:
        pieces = [(layer, x1, y1, x2, y2, width)]
    else:
        mx = int(round(x1 + (x2 - x1) * neck_nm / length))
        my = int(round(y1 + (y2 - y1) * neck_nm / length))
        pieces = [(layer, x1, y1, mx, my, width), (layer, mx, my, x2, y2)]
    if at_start:
        return pieces + list(segments[1:])
    return list(segments[:-1]) + [(l, bx, by, ax, ay) + tuple(rest) for l, ax, ay, bx, by, *rest in reversed(pieces)]


def _with_pad_entries(grid: Grid, net: NetInfo, cells: List[int], segments, neck_nm: int = 0):
    """Extend a cell path at both ends: grid cell -> exit point -> pad centre,
    when the end cell is a pad endpoint of this net. With neck_nm, a path
    leaving a fine-pitch pad whose stub is narrower than the net's width keeps
    the stub's width for that far (the routes of the main passes get this
    from their neck region; ties and fanout searches have none)."""
    out = list(segments)
    for cell, at_start in ((cells[0], True), (cells[-1], False)):
        entry = net.endpoints.get(cell)
        if entry is None:
            continue
        ex, ey, px, py, width = entry
        layer, cx, cy = grid.unpack(cell)
        gx, gy = grid.nm_x(cx), grid.nm_y(cy)
        if neck_nm and width < net.rules.width:
            out = _necked_end(out, width, at_start, neck_nm, (layer, gx, gy))
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


OCTI = [(1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]


def _cut_acute(grid: Grid, segments, free) -> list:
    """No corner sharper than 90 degrees: where a route turns back on itself
    (a pad stub straight up, then a diagonal down and away), the corner is
    cut: from the point before it, a 0/45/90-degree run straight onto the
    next segment, where that run is clear. A designer never leaves an acute
    corner; it also traps etchant and pinches clearance."""
    segs = list(segments)
    changed = True
    rounds = 0
    while changed and rounds < 8:
        changed = False
        rounds += 1
        for i in range(len(segs) - 1):
            s1, s2 = segs[i], segs[i + 1]
            if s1[0] != s2[0] or (s1[3], s1[4]) != (s2[1], s2[2]):
                continue
            ax, ay, bx, by, cx, cy = s1[1], s1[2], s1[3], s1[4], s2[3], s2[4]
            u1 = (bx - ax, by - ay)
            u2 = (cx - bx, cy - by)
            if u1[0] * u2[0] + u1[1] * u2[1] >= 0:
                continue                                   # 90 degrees or wider
            l2 = math.hypot(*u2)
            if l2 == 0:
                continue
            best = None
            for dx, dy in OCTI:
                # A + s*d == B + t*u2/|u2|, solve for s, t
                ex, ey = u2[0] / l2, u2[1] / l2
                det = dx * (-ey) - dy * (-ex)
                if abs(det) < 1e-9:
                    continue
                rx, ry = bx - ax, by - ay
                sv = (rx * (-ey) - ry * (-ex)) / det
                tv = (dx * ry - dy * rx) / det
                if sv <= 0 or tv <= 0 or tv > l2:
                    continue
                qx, qy = int(round(bx + ex * tv)), int(round(by + ey * tv))
                if best is not None and tv >= best[0]:
                    continue
                cells = grid.segment_cells(s1[0], ax, ay, qx, qy, grid.pitch // 2)
                if all(free(c) for c in cells):
                    best = (tv, qx, qy)
            if best is None:
                # skip ahead instead: from A straight (or with one 45-degree
                # jog) to the end of one of the next few segments, which takes
                # out the little snapping steps a route turns back after
                done = False
                for k in range(1, 4):
                    if i + k >= len(segs):
                        break
                    sk = segs[i + k]
                    prev = segs[i + k - 1]
                    if sk[0] != s1[0] or (prev[3], prev[4]) != (sk[1], sk[2]):
                        break
                    dx_, dy_ = sk[3], sk[4]
                    for at_end in (False, True):
                        pts = jog(ax, ay, dx_, dy_, at_end)
                        if any((pts[j], pts[j + 1]) == (pts[j + 1], pts[j]) for j in range(len(pts) - 1)):
                            continue
                        clear = all(free(c) for j in range(len(pts) - 1)
                                    for c in grid.segment_cells(s1[0], *pts[j], *pts[j + 1], grid.pitch // 2))
                        if not clear:
                            continue
                        w1 = s1[5:] if len(s1) > 5 else ()
                        segs[i:i + k + 1] = [(s1[0], pts[j][0], pts[j][1], pts[j + 1][0], pts[j + 1][1]) + w1
                                             for j in range(len(pts) - 1) if pts[j] != pts[j + 1]]
                        done = True
                        break
                    if done:
                        break
                if done:
                    changed = True
                    break
                continue
            _, qx, qy = best
            w1 = s1[5:] if len(s1) > 5 else ()
            new1 = (s1[0], ax, ay, qx, qy) + w1
            new2 = (s2[0], qx, qy, cx, cy) + (s2[5:] if len(s2) > 5 else ())
            segs[i:i + 2] = [new1] + ([new2] if (qx, qy) != (cx, cy) else [])
            changed = True
            break
    return segs


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
        # (net, exit cell) of plane-net pads whose via a rip-up took out: they
        # get a via again after the rip-up rounds, like the fanout list
        self.restore: List[Tuple[int, int]] = []
        self.ripped_existing: List[_Path] = []       # board pieces shove took out, in case they must come back
        self.revived: List[_Path] = []               # ...and board pieces a rolled-back rip-up put back
        self._rip_planes = False          # the second tier of a rip-up search: plane vias may go too
        self.restore_possible = False     # ...there are such vias on the board
        # new paths get ids above every board piece's, even one that is not
        # entered below: the pipeline removes board items by path id
        self.next_pid = max([self.next_pid] + [ex.path.pid + 1 for ex in problem.existing])
        for ex in problem.existing:
            net = problem.nets.get(ex.path.net)
            if net is None or not net.clusters:
                continue
            if not net.route:
                self.restore_possible = True
            c = max(ex.cluster, 0)
            path = _Path(ex.path.pid, Connection(ex.path.net, c, c, 0, rank=net.rank), ex.path, list(ex.cells))
            path.joins = list(ex.joins)
            path.pad_exits = [list(e) for e in ex.pad_exits]
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

    def _fits(self, path: _Path) -> bool:
        """Is this path's copper (with clearance) still clear of every other net?"""
        grid, p, net = self.grid, self.p, self.p.nets[path.conn.net]
        rp = path.path
        for key, owners in p.owners.items():
            width, clearance = key
            for seg in rp.segments:
                layer, x1, y1, x2, y2 = seg[:5]
                seg_w = seg[5] if len(seg) > 5 else rp.width
                r = seg_w // 2 + max(clearance, net.rules.clearance) + width // 2 + p.safety
                if any(owners[idx] not in (0, net.code) for idx in grid.segment_cells(layer, x1, y1, x2, y2, r)):
                    return False
            r_via = rp.via_diameter // 2 + max(clearance, net.rules.clearance) + width // 2 + p.safety
            for x, y in rp.vias:
                for layer in range(grid.layers):
                    if any(owners[idx] not in (0, net.code) for idx in grid.disc_cells(layer, x, y, r_via)):
                        return False
        r_hole = rp.via_drill // 2 + p.hole_to_hole + p.max_via_drill // 2
        for x, y in rp.vias:
            if any(p.via_block[idx] for idx in grid.disc_cells(0, x, y, r_hole)):
                return False
        return True

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

    def _unclaim(self, removed: List[_Path]) -> None:
        """Take paths just dropped from self.paths off the maps: the cells
        they claimed go back to the fixed copper, and the surviving paths
        that claim any of those cells claim them again, in path id order.
        The same maps _rebuild_maps draws (a cell's owner and first claimer
        depend only on the paths claiming it, in id order), without redrawing
        every path: that took seconds per rip-up on a big board, more on the
        second pass's finer grid."""
        if not removed:
            return
        p = self.p
        survivors = [self.paths[pid] for pid in sorted(self.paths)]
        for key, owners in p.owners.items():
            touched: Set[int] = set()
            for path in removed:
                touched.update(path.claims.get(key, ()))
            if not touched:
                continue
            fixed, routed = self.fixed[key], self.routed[key]
            for idx in touched:
                owners[idx] = fixed[idx]
                routed[idx] = 0
            for path in survivors:
                cells = path.claims.get(key)
                if not cells or touched.isdisjoint(cells):
                    continue
                code, pid = p.nets[path.conn.net].code, path.pid
                for idx in cells:
                    if idx not in touched:
                        continue
                    cur = owners[idx]
                    if cur == 0:
                        owners[idx] = code
                        routed[idx] = pid
                    elif cur != code and cur != BLOCKED:
                        owners[idx] = BLOCKED
        touched = set()
        for path in removed:
            touched.update(path.via_cells)
        if touched:
            for idx in touched:
                p.via_block[idx] = self.fixed_via_block[idx]
            for path in survivors:
                if path.via_cells and not touched.isdisjoint(path.via_cells):
                    for idx in path.via_cells:
                        if idx in touched:
                            p.via_block[idx] = 1

    # ---- routing one connection ------------------------------------------

    def _key(self, conn: Connection) -> Tuple[int, int]:
        net = self.p.nets[conn.net]
        return (conn.width or net.rules.width, net.rules.clearance)

    def _search(self, conn: Connection, via_cost: int, cap: int, soft: bool, cancelled,
                only_layer: Optional[int] = None):
        net = self.p.nets[conn.net]
        owners = self.p.owners[self._key(conn)]
        a_cells, a_anchors = self._component(conn.net, conn.a)
        b_cells, b_anchors = self._component(conn.net, conn.b)
        if not a_cells or not b_cells:
            return None
        # The search leaves from the fine-pitch end (a CM5 row pin, a QFN pin)
        # when only one end is one: the hard part of the route, out of the
        # crowded row, then costs a little flood around that pin, and the
        # rest heads straight for the open end. From the open end, every
        # detour the crowded end needs widens the flood along the whole run
        # (a failing search wandered the board to its cap). Otherwise the
        # smaller end leaves.
        tight_a, tight_b = self._tight(conn.net, conn.a), self._tight(conn.net, conn.b)
        if (tight_b if tight_a != tight_b else len(a_cells) > len(b_cells)):
            a_cells, b_cells, b_anchors = b_cells, a_cells, a_anchors
        routable = self.routable
        allow_vias = self.opts.allow_vias
        if only_layer is not None:
            # one layer, no vias: the flat route a designer draws when it is direct
            routable = [i == only_layer and r for i, r in enumerate(self.routable)]
            allow_vias = False
            plane = self.grid.plane
            a_cells = {c for c in a_cells if c // plane == only_layer}
            b_cells = {c for c in b_cells if c // plane == only_layer}
            if not a_cells or not b_cells:
                return None
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
                           self.horizontal, via_cost, allow_vias, via_reach, cap,
                           self.opts.turn_cost, cancelled, routable=routable, via_guard=self.p.via_guard, lane_guard=self.p.lane_guard,
                           history=self.history, **kwargs)

    def _tight(self, code: int, i: int) -> bool:
        """Does the component holding initial cluster i hold a fine-pitch
        (necked) pad escape?"""
        net = self.p.nets[code]
        root = self._find(code, i)
        return any(cl.tight for j, cl in enumerate(net.clusters) if self._find(code, j) == root)

    def _flat_route(self, conn: Connection, cells: List[int], cap: int, cancelled) -> List[int]:
        """A route that changes layers is tried again on one outer layer
        alone: a designer keeps a connection on the top (or bottom) layer
        whenever the flat track is nearly direct, and spends vias only where
        it is not. The flat route replaces the via route when it is no longer
        than FLAT_DETOUR times the straight distance between the ends (plus
        FLAT_SLACK_NM for the pad stubs), else the via route stands."""
        grid, p = self.grid, self.p
        plane = grid.plane
        if grid.layers < 2 or all(cells[k] // plane == cells[k - 1] // plane for k in range(1, len(cells))):
            return cells
        _, a_anchors = self._component(conn.net, conn.a)
        _, b_anchors = self._component(conn.net, conn.b)
        if not a_anchors or not b_anchors:
            return cells
        # the straight distance between the ends, in cells (octile)
        direct = min(max(abs(ax - bx), abs(ay - by)) + (math.sqrt(2) - 1) * min(abs(ax - bx), abs(ay - by))
                     for ax, ay in a_anchors for bx, by in b_anchors)
        limit = FLAT_DETOUR * direct + FLAT_SLACK_NM / grid.pitch
        net = p.nets[conn.net]
        lane_guard = p.lane_guard
        flat_cap = min(cap, max(FLAT_MIN_EXPANSIONS, int(4 * direct * direct)))
        for layer in (0, grid.layers - 1):
            if not self.routable[layer]:
                continue
            flat = self._search(conn, 0, flat_cap, False, cancelled, only_layer=layer)
            if not flat:
                continue
            length = 0.0
            for k in range(1, len(flat)):
                _, x1, y1 = grid.unpack(flat[k - 1])
                _, x2, y2 = grid.unpack(flat[k])
                length += math.sqrt(2) if x1 != x2 and y1 != y2 else 1.0
            if length > limit:
                continue
            if lane_guard is not None and any(
                    lane_guard[c] not in (0, net.code, BODY_LANE) for c in flat):
                continue                           # it runs through another pin's outlet: not that
            return flat
        return cells

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
        if path.existing and not self.p.nets[path.conn.net].route:
            # a plane net's pad via (this job's own): only when nothing else helps
            return self._rip_planes
        # a less important group does not push a pair, memory or high-speed
        # route off its line (those were routed first for a reason)
        if self.opts.protect_groups and path.conn.rank < for_conn.rank \
                and self.p.nets[path.conn.net].group in PROTECTED_GROUPS:
            return False
        key = (path.conn.net, path.conn.a, path.conn.b)
        return self.rips.get(key, 0) < self.opts.rip_limit

    def _trim_to_pads(self, conn: Connection, cells: List[int]) -> List[int]:
        """A search runs between pad exits, and a pad's stub (centre to exit)
        is written with the route that ends there. A route that reaches its
        far pad from the other side (the under-row bridge joining a USB-C
        connector's doubled pins) would run on over the pad's own copper to
        the exit, doubling the stub: copper KiCad's length tuner and skew
        rule count twice, and a loop a designer never leaves. Such a route
        ends where it first enters the far pad's copper (and starts where it
        last leaves its own), the way the designer lands on the pad's edge."""
        net = self.p.nets[conn.net]
        copper_of: Dict[int, Set[int]] = {}
        for j, cl in enumerate(net.clusters):
            if cl.copper:
                copper_of.setdefault(self._find(conn.net, j), set()).update(cl.copper)
        if not copper_of or len(cells) < 3:
            return cells
        root_a, root_b = self._find(conn.net, conn.a), self._find(conn.net, conn.b)
        a_cells, _ = self._component(conn.net, conn.a)
        start_root, end_root = (root_a, root_b) if cells[0] in a_cells else (root_b, root_a)
        end_copper = copper_of.get(end_root, set())
        if end_copper and cells[-1] not in end_copper:
            for k, c in enumerate(cells):
                if c in end_copper:
                    cells = cells[:k + 1]
                    break
        start_copper = copper_of.get(start_root, set())
        if start_copper and cells[0] not in start_copper:
            for k in range(len(cells) - 1, 0, -1):
                if cells[k] in start_copper:
                    cells = cells[k:]
                    break
        return cells

    def _commit(self, conn: Connection, cells: List[int], on_path) -> _Path:
        net = self.p.nets[conn.net]
        owners = self.p.owners[self._key(conn)]
        cells = self._trim_to_pads(conn, cells)
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
        if self.opts.cut_acute:
            segments = _cut_acute(self.grid, segments, free)
        if net.layer_widths:
            # controlled impedance: each layer's own width (neck segments keep theirs)
            segments = [sg if len(sg) > 5 or net.layer_widths.get(sg[0], width) == width
                        else sg + (net.layer_widths[sg[0]],) for sg in segments]
        rp = RoutedPath(net.code, width, segments, vias, net.rules.via_diameter,
                        net.rules.via_drill, pid=self.next_pid)
        self.next_pid += 1
        # The cells another route of the net may end on: the ones under the
        # copper as drawn (acute corners cut, pad entries), not the search's
        # cells, or a later route T-joins a spot the drawing left and dangles.
        grid = self.grid
        drawn: List[int] = []
        for sg in segments:
            drawn += grid.segment_cells(sg[0], sg[1], sg[2], sg[3], sg[4], grid.pitch // 2)
        for vx, vy in vias:
            vcx, vcy = grid.cell_x(vx), grid.cell_y(vy)
            drawn += [grid.index(l, vcx, vcy) for l in range(grid.layers)]
        # The route's own ends stay first and last: _dependents reads them
        # there to find the route a T-joint hangs on (dict.fromkeys alone
        # keeps a cell's first place, and the far end is under the drawing
        # already, so a route ending on another one was not ripped with it)
        ends = {cells[0], cells[-1]}
        drawn = [cells[0]] + [c for c in dict.fromkeys(drawn) if c not in ends] + ([cells[-1]] if len(ends) > 1 else [])
        path = _Path(rp.pid, conn, rp, drawn)
        # The clusters the path really joins: the pads its ends touch. A
        # search may end on any pad of the far component, not on the
        # connection's own cluster (three pads joined by one ripped-up piece
        # come back as two connections, and the second may land on the first's
        # pad); the union must follow the copper, or ripping up the other
        # route later leaves a pad open that the union still counts as joined.
        a, b = self._end_clusters(conn, cells)
        path.joins = [(a, b)]
        self.paths[rp.pid] = path
        self._claim(path)
        self.parent[conn.net][self._find(conn.net, a)] = self._find(conn.net, b)
        if on_path:
            on_path(rp)
        return path

    def _dependents(self, pids: List[int]) -> List[int]:
        """The paths to rip up with these: a later route of the same net may
        have ended on one of them (a T-joint onto its track or via) instead
        of on a pad; taken out from under it, it would hang in the air while
        the union still counted it as joined. Those go too, transitively,
        and are rerouted with the rest."""
        out = list(pids)
        seen = set(out)
        cluster_cells: Dict[int, Set[int]] = {}
        k = 0
        while k < len(out):
            path = self.paths.get(out[k])
            k += 1
            if path is None:
                continue
            code = path.conn.net
            if code not in cluster_cells:
                cluster_cells[code] = set().union(*(cl.cells | cl.copper for cl in self.p.nets[code].clusters))
            on_pads = cluster_cells[code]
            cells = set(path.cells)
            for pid, other in self.paths.items():
                if pid in seen or other.conn.net != code or other.paired or other.existing or not other.cells:
                    continue
                if any(end in cells and end not in on_pads for end in (other.cells[0], other.cells[-1])):
                    seen.add(pid)
                    out.append(pid)
        return out

    def _end_clusters(self, conn: Connection, cells: List[int]) -> Tuple[int, int]:
        """The cluster at each end of a cell path: the pad the end lies on,
        or, for an end on another route's copper, the connection's cluster
        on that side (the one in the other end's component it is not)."""
        net = self.p.nets[conn.net]
        ends = [next((j for j, cl in enumerate(net.clusters) if cell in cl.cells or cell in cl.copper), None)
                for cell in (cells[0], cells[-1])]
        if ends[0] is None and ends[1] is None:
            return conn.a, conn.b
        if ends[0] is not None and ends[1] is not None:
            return ends[0], ends[1]
        known = ends[0] if ends[0] is not None else ends[1]
        other = conn.b if self._find(conn.net, conn.a) == self._find(conn.net, known) else conn.a
        return known, other

    def _rip(self, pids: List[int], on_remove) -> List[Connection]:
        conns = []
        removed: List[_Path] = []
        for pid in self._dependents(list(pids)):
            path = self.paths.pop(pid, None)
            if path is None:
                continue
            removed.append(path)
            if _DEBUG:
                print(f"[rip] pid {pid} {self.p.nets[path.conn.net].name} existing={path.existing} vias={[(x/1e6, y/1e6) for x, y in path.path.vias]}", flush=True)
            if path.existing and not self.p.nets[path.conn.net].route:
                # a plane net's pad via from an earlier pass of this job: the
                # pad gets its via back after the rip-up rounds, from its exits
                self.shoved += 1
                for exits in path.pad_exits:
                    if exits and (path.conn.net, exits[0]) not in self.restore:
                        self.restore.append((path.conn.net, exits[0]))
            elif path.existing:
                # a board piece may have joined several clusters: each join
                # becomes a connection to route again (and the piece comes
                # back as it was if that fails and its room is still free)
                self.shoved += 1
                self.ripped_existing.append(path)
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
        self._unclaim(removed)
        return conns

    def _undo_rip(self, before: Dict[int, _Path], marks: Tuple[int, int, int], on_path, on_remove) -> None:
        """Back to the paths of `before`: a rip-up attempt that cost more
        connections than it won. The paths it added come off, the ones it
        ripped go back where they were (their room is free again)."""
        new = [self.paths.pop(pid) for pid in sorted(self.paths) if pid not in before]
        for path in new:
            if on_remove:
                on_remove(path.pid)
        self._unclaim(new)
        back = [before[pid] for pid in sorted(before) if pid not in self.paths]
        for path in back:
            path.claims, path.via_cells = {}, []
            self.paths[path.pid] = path
            self._claim(path)
            if path.existing:
                self.revived.append(path)        # its board items were removed: it is drawn again
            if on_path:
                on_path(path.path)
        n_restore, n_existing, self.shoved = marks
        del self.restore[n_restore:]
        del self.ripped_existing[n_existing:]
        for code in {path.conn.net for path in new + back}:
            self._rebuild_union(code)

    # ---- differential pairs ----------------------------------------------

    def _exit_of(self, code: int, cluster: int, layers=()) -> Optional[int]:
        """A pad exit cell of a cluster (the cell a route starts from), on
        the first of `layers` it has one on (a through-hole pin can be left
        on any layer), else the lowest layer it has. Deterministic: the
        cluster's cells are a set."""
        net = self.p.nets[code]
        cells = net.clusters[cluster].cells
        exits = sorted(c for c in cells if c in net.endpoints)
        if not exits:
            return min(cells) if cells else None
        for layer in layers:
            for c in exits:
                if c // self.grid.plane == layer:
                    return c
        return exits[0]

    def _direct(self, conn: Connection, on_path) -> bool:
        """Two pads of a connection close together with nothing in between
        (a resistor facing its LED, a capacitor beside the IC pin it
        decouples): joined by a straight track, or one with a single 45-degree
        jog, from pad to pad on a layer both pads have, the way a designer
        draws it, instead of leaving each pad along its escape and meeting
        round the back. Tried at the full width, then at the neck width."""
        grid, p = self.grid, self.p
        net = p.nets[conn.net]
        if conn.pair or self._find(conn.net, conn.a) == self._find(conn.net, conn.b):
            return False
        ca, cb = net.clusters[conn.a], net.clusters[conn.b]
        limit = self.opts.direct_max_mm * 1_000_000 / grid.pitch
        widths = [conn.width or net.rules.width]
        if conn.neck and conn.neck < widths[0]:
            widths.append(conn.neck)
        def pads(cluster):
            # pad centres (nm) per layer, from the escape records of the
            # cluster's exit cells (the cluster's anchors are those exit cells)
            out = {}
            for idx in cluster.cells:
                e = net.endpoints.get(idx)
                if e is not None:
                    layer = grid.unpack(idx)[0]
                    out.setdefault((layer, e[2], e[3]), None)
            return list(out)

        pa, pb = pads(ca), pads(cb)
        best = None
        for (la, ax, ay) in pa:
            for (lb, bx, by) in pb:
                if la != lb or not self.routable[la]:
                    continue                             # both pads on the same layer
                layer = la
                d = max(abs(ax - bx), abs(ay - by)) / grid.pitch
                if d == 0 or d > limit:
                    continue
                if True:
                    for w in widths:
                        owners = p.owners.get((w, net.rules.clearance))
                        if owners is None:
                            continue
                        for at_end in (False, True):
                            pts = jog(ax, ay, bx, by, at_end)
                            cells = []
                            ok = True
                            for i in range(len(pts) - 1):
                                run = grid.segment_cells(layer, *pts[i], *pts[i + 1], grid.pitch // 2)
                                for idx in run:
                                    if owners[idx] not in (0, net.code):
                                        ok = False
                                        break
                                # the whole width of the track stays out of other pads' outlet lanes
                                if ok and p.lane_guard is not None:
                                    for idx in grid.segment_cells(layer, *pts[i], *pts[i + 1], max(grid.pitch // 2, w // 2)):
                                        g = p.lane_guard[idx]
                                        if g and g != net.code and g != BODY_LANE:
                                            ok = False
                                            break
                                if not ok:
                                    break
                                cells += run
                            if not ok:
                                continue
                            length = sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
                                         for i in range(len(pts) - 1))
                            cand = (length, -w, layer, pts, cells, w)
                            if best is None or cand[:2] < best[:2]:
                                best = cand
                            break                        # the first jog that fits will do
                        if best is not None and best[5] == w:
                            break                        # full width fits: no need for the neck
        if best is None:
            return False
        _, _, layer, pts, cells, w = best
        lw = net.layer_widths.get(layer, w) if net.layer_widths else w
        segments = [(layer, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]) + ((lw,) if lw != w else ())
                    for i in range(len(pts) - 1)]
        rp = RoutedPath(net.code, w, segments, [], net.rules.via_diameter, net.rules.via_drill, pid=self.next_pid)
        self.next_pid += 1
        path = _Path(rp.pid, conn, rp, cells)
        self.paths[rp.pid] = path
        self._claim(path)
        self.parent[conn.net][self._find(conn.net, conn.a)] = self._find(conn.net, conn.b)
        if on_path:
            on_path(rp)
        return True

    def _pair_fail(self, name: str, code: str, text: str) -> None:
        """Why a pair could not be routed coupled (the router then routes it as an exception)."""
        self.pair_log = getattr(self, "pair_log", [])
        self.pair_log.append((name, text))
        self._pair_why = code

    def _route_exception(self, ca: Connection, cb: Connection, via_cost: int, cap: int, on_path, cancelled,
                         failed: List[Connection]) -> None:
        """A pair that cannot or need not run coupled: each line is connected
        directly, as a designer would draw a short or awkward pair by hand,
        staying on its layer where it can (vias cost several times more, then
        the normal cost if that finds nothing)."""
        why = getattr(self, "_pair_why", "") or "no_path"
        na, nb = self.p.nets[ca.net], self.p.nets[cb.net]
        where = " - ".join("/".join(sorted(na.clusters[i].refs)) or "?" for i in (ca.a, ca.b))
        if not any(e["nets"] == [na.name, nb.name] and e["reason"] == why and e.get("where") == where
                   for e in self.pair_exceptions):
            # exempt_skew: coupling was not needed here; whether the skew is
            # waived too depends on the whole pair being short (decided after routing)
            self.pair_exceptions.append({"nets": [na.name, nb.name], "reason": why,
                                         "text": PAIR_EXCEPTIONS.get(why, why), "where": where,
                                         "exempt_skew": why in PAIR_UNNECESSARY})
        factor = max(1, self.opts.pair_direct_via_factor)
        for c in (ca, cb):
            if self.opts.direct_links and self._direct(c, on_path):
                continue
            if self._try(c, via_cost * factor, cap, on_path, cancelled):
                continue
            if factor > 1 and self._try(c, via_cost, cap, on_path, cancelled):
                continue
            failed.append(c)

    def _try_pair(self, ca: Connection, cb: Connection, via_cost: int, cap: int, on_path, cancelled) -> bool:
        """A coupled run between pins that sit side by side at both ends. A
        part with each line on two pins (a flow-through ESD array, a USB-C
        receptacle) offers a choice: the two lines land on neighbouring pins
        (never one on each row), and when the run would have to cross or
        pinch it is tried to the other pair of pins there, since entering the
        part from its other side swaps the lines' order; a designer uses those
        pins to avoid a crossing."""
        na, nb = self.p.nets[ca.net], self.p.nets[cb.net]

        def keeps_tree(conn, end, new):
            """Does the net stay connected with this connection landing on
            cluster `new` instead? The rest of its connections must join
            `new` to the end it replaces."""
            if new == getattr(conn, end):
                return True
            k = len(self.p.nets[conn.net].clusters)
            parent = list(range(k))

            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i
            for c in self.p.connections:
                if c.net == conn.net and c is not conn:
                    parent[find(c.a)] = find(c.b)
            return find(new) == find(getattr(conn, end)) and find(conn.a) != find(conn.b)

        def choices(net, conn, end):
            ci = getattr(conn, end)
            refs = net.clusters[ci].refs
            out = [ci] + [i for i, cl in enumerate(net.clusters) if i != ci and refs and cl.refs & refs]
            return [i for i in out if keeps_tree(conn, end, i)]

        def gap(i, j):
            (x1, y1), (x2, y2) = na.clusters[i].anchors[0], nb.clusters[j].anchors[0]
            return math.hypot(x1 - x2, y1 - y2)

        # which of B's ends faces A's a-end
        if not (na.clusters[ca.a].refs & nb.clusters[cb.a].refs) and (na.clusters[ca.a].refs & nb.clusters[cb.b].refs):
            pairs_ends = (("a", "b"), ("b", "a"))
        else:
            pairs_ends = (("a", "a"), ("b", "b"))
        # per end: (A's cluster, B's cluster) combinations, neighbours first
        per_end = []
        for end_a, end_b in pairs_ends:
            combos = [(gap(i, j), i, j) for i in choices(na, ca, end_a) for j in choices(nb, cb, end_b)]
            combos.sort()
            best = combos[0][0] if combos else 0
            # side by side only: within twice the closest spacing (and 3 of them at most)
            per_end.append([(i, j) for g, i, j in combos if g <= max(best * 2, best + 1)][:3]
                           or [(getattr(ca, end_a), getattr(cb, end_b))])
        why = ""
        tried = 0
        for i0, j0 in per_end[0]:
            for i1, j1 in per_end[1]:
                if tried and cancelled is not None and cancelled():
                    return False
                ca2 = dataclasses.replace(ca, **{pairs_ends[0][0]: i0, pairs_ends[1][0]: i1})
                cb2 = dataclasses.replace(cb, **{pairs_ends[0][1]: j0, pairs_ends[1][1]: j1})
                if ca2.a == ca2.b or cb2.a == cb2.b:
                    continue
                tried += 1
                if self._try_pair_once(ca2, cb2, via_cost, cap, on_path, cancelled):
                    return True
                why = why or self._pair_why
                if self._pair_why not in ("cross", "pinch"):
                    self._pair_why = why
                    return False
        self._pair_why = why or self._pair_why
        return False

    def _commit_loose(self, cand, loose, cells, via_cost, cap, on_path, cancelled) -> bool:
        """Commit a coupled run that stops short of one end (loose True: the
        start, False: the far end, "both": either end), then finish each line
        to its pad there single-ended. All or nothing: if any line cannot be
        finished, everything goes again and the pair is left to the caller's
        exception route."""
        grid = self.grid
        before = set(self.paths)
        self.next_pid += 2
        sides = (True, False) if loose == "both" else (loose,)
        committed = []                        # (conn, the loose end's cluster, the pad cluster it finishes to)
        spans = []                            # (rp, conn, the run's two ends as clusters)
        for rp, conn, cl_a, cl_b in cand:
            w_ = rp.width
            net = self.p.nets[conn.net]
            ks = {}
            own_cells = []
            for at_start in sides:
                # the finish leaves from the run's loose end only (joining the
                # line further along would leave its tail hanging)
                sg = rp.segments[0] if at_start else rp.segments[-1]
                lx, ly = (sg[1], sg[2]) if at_start else (sg[3], sg[4])
                ccx, ccy = grid.cell_x(lx), grid.cell_y(ly)
                cell = grid.index(sg[0], ccx, ccy)
                gx_, gy_ = grid.nm_x(ccx), grid.nm_y(ccy)
                if (gx_, gy_) != (lx, ly):
                    # a stub onto the cell centre, where the finishing search starts
                    tie = (sg[0], gx_, gy_, lx, ly, w_) if at_start else (sg[0], lx, ly, gx_, gy_, w_)
                    rp.segments = ([tie] + rp.segments) if at_start else (rp.segments + [tie])
                # the run's loose end becomes a cluster of its own for the
                # finishing search to reach (the line's far pad must not count
                # as a goal); the run joins its two ends once the finish is in
                k = len(net.clusters)
                net.clusters.append(Cluster(cells={cell}, anchors=[(ccx, ccy)]))
                self.parent[conn.net].append(k)
                ks[at_start] = k
                own_cells.append(cell)
                committed.append((conn, k, cl_a if at_start else cl_b))
            k0 = ks[sides[0]]
            path = _Path(rp.pid, dataclasses.replace(conn, a=k0, b=k0), rp, [], paired=True)
            path.joins = [(k0, k0)]
            self.paths[rp.pid] = path
            self._claim(path)
            spans.append((rp, conn, ks.get(True, cl_a), ks.get(False, cl_b), own_cells))
        ok = True
        for conn, k, loose_pad in committed:
            if cancelled is not None and cancelled():
                ok = False
                break
            if not self._try(dataclasses.replace(conn, a=loose_pad, b=k, pair=0), via_cost, cap, None, cancelled):
                if _DEBUG:
                    cl_ = self.p.nets[conn.net].clusters[k]
                    print(f"[loose-finish] {self.p.nets[conn.net].name} {loose}: no way from "
                          f"{'/'.join(sorted(self.p.nets[conn.net].clusters[loose_pad].refs))} to the run end at "
                          f"{[(round(self.grid.nm_x(x) / 1e6, 2), round(self.grid.nm_y(y) / 1e6, 2)) for x, y in cl_.anchors]}", flush=True)
                ok = False
                break
        new = [pid for pid in self.paths if pid not in before]
        if not ok:
            removed = [self.paths.pop(pid) for pid in new]
            for conn, k, loose_pad in reversed(committed):
                net = self.p.nets[conn.net]
                if len(net.clusters) == k + 1:
                    net.clusters.pop()
                    self.parent[conn.net].pop()
            for code in {c[0].net for c in committed}:
                self._rebuild_union(code)
            self._unclaim(removed)
            return False
        for rp, conn, ka, kb, own_cells in spans:
            path = self.paths[rp.pid]
            path.joins = [(ka, kb)]
            path.cells = own_cells
            self.parent[conn.net][self._find(conn.net, ka)] = self._find(conn.net, kb)
        for pid in new:
            self.paths[pid].paired = True        # the single-ended finishes stay with their run (no rip-up)
        if on_path:
            for pid in sorted(new):
                on_path(self.paths[pid].path)
        return True

    def _wide_lane_guard(self, reach: int):
        """The pad outlet lanes grown by `reach` cells: only a coupled run's
        centreline is searched, and its two lines run either side of it, so
        a run passing a pin row must keep that much further off the outlets
        (else it hugs the row and walls the neighbouring pins in)."""
        p, grid = self.p, self.grid
        if p.lane_guard is None or reach <= 0:
            return p.lane_guard
        cache = self.__dict__.setdefault('_lane_guard_cache', {})
        got = cache.get(reach)
        if got is None:
            src = p.lane_guard
            got = array("i", src)
            w, h, plane = grid.w, grid.h, grid.plane
            offs = [(dx, dy) for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)
                    if dx * dx + dy * dy <= reach * reach and (dx or dy)]
            for idx in (i for i, v in enumerate(src) if v and v != BODY_LANE):
                code = src[idx]
                layer, rest = divmod(idx, plane)
                cy, cx = divmod(rest, w)
                for dx, dy in offs:
                    x, y = cx + dx, cy + dy
                    if 0 <= x < w and 0 <= y < h:
                        j = layer * plane + y * w + x
                        cur = got[j]
                        if cur == 0:
                            got[j] = code
                        elif cur != code and src[j] == 0:
                            got[j] = -1                # two outlets' lanes meet: nobody's
            cache[reach] = got
        return got

    def _pair_guard(self, base, reach: int):
        """The pair runs' lane guard: the wide outlet lanes, plus each pair
        pin's approach, PAIR_APPROACH_NM straight out of its exits. Another
        pair's run passing there takes the room the pair needs to come in
        side by side; it pays the lane penalty, so a run that can go round
        (or under, on another layer) does, as a designer keeps a second
        pair's way to the same connector open."""
        if base is None:
            return None
        cache = self.__dict__.setdefault('_pair_guard_cache', {})
        got = cache.get(reach)
        if got is None:
            grid = self.grid
            got = array("i", base)
            for code, net in self.p.nets.items():
                if not net.partner:
                    continue
                for idx, (ex, ey, px, py, _w) in net.endpoints.items():
                    dx, dy = ex - px, ey - py
                    span = math.hypot(dx, dy)
                    if span == 0:
                        continue
                    tx, ty = int(ex + dx / span * PAIR_APPROACH_NM), int(ey + dy / span * PAIR_APPROACH_NM)
                    for j in grid.segment_cells(idx // grid.plane, ex, ey, tx, ty, PAIR_APPROACH_HALF_NM):
                        if got[j] == 0:
                            got[j] = code
            cache[reach] = got
        return got

    def _try_pair_once(self, ca: Connection, cb: Connection, via_cost: int, cap: int, on_path, cancelled) -> bool:
        """Route two connections of a differential pair as one coupled track:
        a centreline as wide as both tracks and their gap is searched, then
        split into the two tracks offset to either side, each joined to its
        own pad exits. Falls back to separate routing when the pair cannot
        start or end side by side."""
        p, grid = self.p, self.grid
        na, nb = p.nets[ca.net], p.nets[cb.net]
        self.pair_log = getattr(self, 'pair_log', [])
        self._pair_why = ""
        if self._find(ca.net, ca.a) == self._find(ca.net, ca.b) or self._find(cb.net, cb.a) == self._find(cb.net, cb.b):
            self._pair_fail(na.name, 'joined', 'already joined')
            return False
        # match the ends: B's clusters that share footprints with A's
        a1, a2 = na.clusters[ca.a], na.clusters[ca.b]
        b1, b2 = nb.clusters[cb.a], nb.clusters[cb.b]
        if not (a1.refs & b1.refs) and (a1.refs & b2.refs):
            b1, b2 = b2, b1
            cb_a, cb_b = cb.b, cb.a
        else:
            cb_a, cb_b = cb.a, cb.b
        # both lines leave each end on one layer: the partner's pin is taken on
        # the layer this net's pin leaves on, and the far end on the near
        # end's layer when it can be (a through-hole jack's pins can be left
        # on any layer: the pair need not change layers to reach them)
        plane = grid.plane
        ea1 = self._exit_of(ca.net, ca.a)
        eb1 = self._exit_of(cb.net, cb_a, (ea1 // plane,) if ea1 is not None else ())
        if ea1 is not None and eb1 is not None and ea1 // plane != eb1 // plane:
            # the partner has no exit there: try this net on the partner's layer
            ea1 = self._exit_of(ca.net, ca.a, (eb1 // plane,))
        near = (ea1 // plane,) if ea1 is not None else ()
        ea2 = self._exit_of(ca.net, ca.b, near)
        eb2 = self._exit_of(cb.net, cb_b, ((ea2 // plane,) if ea2 is not None else ()) + near)
        if ea2 is not None and eb2 is not None and ea2 // plane != eb2 // plane:
            ea2 = self._exit_of(ca.net, ca.b, (eb2 // plane,))
        if None in (ea1, ea2, eb1, eb2):
            self._pair_fail(na.name, 'no_exits', 'no exits')
            return False
        la, xa1, ya1 = grid.unpack(ea1)
        lb, xb1, yb1 = grid.unpack(eb1)
        la2, xa2, ya2 = grid.unpack(ea2)
        lb2, xb2, yb2 = grid.unpack(eb2)
        if la != lb or la2 != lb2:
            self._pair_fail(na.name, 'layers', 'exits on different layers')
            return False
        w, gap = na.pair_width or na.rules.width, na.pair_gap
        W = 2 * w + gap
        key = (W, na.rules.clearance)
        owners = p.owners.get(key)
        if owners is None:
            self._pair_fail(na.name, 'no_map', 'no owner map for pair width')
            return False
        d = (w + gap) / 2.0

        span = math.hypot(xa2 - xa1, ya2 - ya1)          # cells between the two ends
        if span * grid.pitch < self.opts.pair_min_span_mm * 1_000_000:
            self._pair_fail(na.name, 'short', f'ends closer than {self.opts.pair_min_span_mm:g} mm')
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

        s_all = starts(xa1, ya1, xb1, yb1, la, ca.net, ea1)
        s_list = usable(s_all)
        # the stretch the coupled run may start on: no via within a lead of it
        lead_from = (min(s_all)[1], max(s_all)[1]) if s_all else None
        g_list = usable(starts(xa2, ya2, xb2, yb2, la2, ca.net, ea2))
        if not s_list or not g_list:
            self._pair_fail(na.name, 'no_start', f'no coupled start/goal (starts {len(s_list)}, goals {len(g_list)})')
            return False
        # couple as early as possible: starting further out costs extra, and
        # only the nearest usable goal rows are offered
        # ...but past the pins' outlets: a run that turns right at the pad
        # row hugs it and walls the neighbouring pins in, where a designer
        # leaves the row straight and couples a little way out
        k_out = int(math.ceil(OUTLET_NM / grid.pitch))
        s_costs = {c: (k + max(0, k_out - k) * 4) * astar.STRAIGHT * 3 for k, c in s_list}
        out_rows = [k for k, _ in g_list if k >= k_out]
        kmin = min(out_rows) if out_rows else min(k for k, _ in g_list)
        g_cells = {c for k, c in g_list if kmin <= k <= kmin + 2}
        s_cells = set(s_costs)
        anchors = [grid.unpack(c)[1:] for c in list(g_cells)[:1]]
        reach = int(math.ceil(d / grid.pitch))
        guard = self._pair_guard(self._wide_lane_guard(reach), reach)
        # A coupled run may change layers: the two lines flare apart to a via
        # each, side by side across the run, and close up again beyond (a
        # pair's way through a pin row it cannot cross on its own layer).
        # dv is the vias' distance from the centreline, flare the 45-degree
        # step out to it.
        vr = na.rules.via_diameter / 2.0
        dv = max(d, vr + na.rules.clearance / 2.0, (na.rules.via_drill + p.hole_to_hole) / 2.0) + grid.pitch / 2.0
        flare = dv - d
        via_reach = int(math.ceil((dv + vr - W / 2.0) / grid.pitch)) + 1

        def owner_map(width):
            """The clearance map for a track this wide (or the nearest narrower)."""
            keys = [k for k in p.owners if k[1] == na.rules.clearance and k[0] <= width]
            return p.owners[max(keys)] if keys else None

        def via_ok(x, y, code):
            cx, cy = grid.cell_x(x), grid.cell_y(y)
            if not grid.inside(cx, cy) or p.via_block[cy * grid.w + cx]:
                return False
            if p.via_guard is not None and p.via_guard[cy * grid.w + cx] not in (0, code):
                return False
            om = owner_map(w)
            if om is None:
                return False
            r = max(0, int(math.ceil((vr - w / 2.0) / grid.pitch)))
            for ddx in range(-r, r + 1):
                for ddy in range(-r, r + 1):
                    if ddx * ddx + ddy * ddy > r * r:
                        continue
                    if not grid.inside(cx + ddx, cy + ddy):
                        return False
                    for li in range(grid.layers):
                        if om[grid.index(li, cx + ddx, cy + ddy)] not in (0, code):
                            return False
            return True

        def unit(a, b):
            ux, uy = b[0] - a[0], b[1] - a[1]
            n_ = math.hypot(ux, uy) or 1.0
            return ux / n_, uy / n_

        def trim(poly, t):
            """The polyline with its first t nanometres cut off."""
            left = t
            for i in range(len(poly) - 1):
                (x1, y1), (x2, y2) = poly[i], poly[i + 1]
                seg = math.hypot(x2 - x1, y2 - y1)
                if seg > left:
                    f = left / seg
                    q = (int(round(x1 + (x2 - x1) * f)), int(round(y1 + (y2 - y1) * f)))
                    return [q] + poly[i + 1:]
                left -= seg
            return None

        lead_nm = 1_000_000 + flare                 # a straight lead before a via pair

        def station():
            """Where a run can turn about beyond the far end's pins: the
            first spot out along the pins' escape, past the lead, where the
            via pair fits either side of the escape axis and the lead back
            to the pins is free. (sx, sy) nm and its cell, with the on-axis
            goal point the lead runs to; None when nothing fits."""
            ex, ey, px, py, _ = p.nets[ca.net].endpoints.get(ea2, (None,) * 5)
            if ex is None:
                return None
            gx, gy = grid.nm_x(xa2), grid.nm_y(ya2)
            if (gx, gy) == (px, py):
                return None
            ux, uy = unit((px, py), (gx, gy))
            mx, my = (grid.nm_x(xa2) + grid.nm_x(xb2)) / 2.0, (grid.nm_y(ya2) + grid.nm_y(yb2)) / 2.0
            ax_, ay_ = mx + ux * kmin * grid.pitch, my + uy * kmin * grid.pitch
            om = owner_map(W)
            approach = max(500_000, flare + 2 * grid.pitch)   # straight in to the station on the via layer
            others = [l_ for l_ in range(grid.layers) if l_ != la2 and self.routable[l_]]

            def free(l_, x1, y1, x2, y2):
                return om is None or all(om[i] in (0, ca.net, cb.net)
                                         for i in grid.segment_cells(l_, int(x1), int(y1), int(x2), int(y2), grid.pitch // 2))
            for t in range(0, 13):
                dist = lead_nm + t * 250_000
                sx, sy = ax_ + ux * dist, ay_ + uy * dist
                scx, scy = grid.cell_x(sx), grid.cell_y(sy)
                if not grid.inside(scx, scy):
                    break
                sx, sy = grid.nm_x(scx), grid.nm_y(scy)
                if not free(la2, sx, sy, ax_, ay_):
                    continue
                v1 = (int(round(sx - uy * dv)), int(round(sy + ux * dv)))
                v2 = (int(round(sx + uy * dv)), int(round(sy - ux * dv)))
                if not all(via_ok(x, y, ca.net) or via_ok(x, y, cb.net) for x, y in (v1, v2)):
                    continue
                # the approach comes from the pins' side: the run passes under the pins and turns about
                acx, acy = grid.cell_x(sx - ux * approach), grid.cell_y(sy - uy * approach)
                if not grid.inside(acx, acy):
                    continue
                apx, apy = grid.nm_x(acx), grid.nm_y(acy)
                layers = [l_ for l_ in others if free(l_, apx, apy, sx, sy)]
                if layers:
                    return sx, sy, scx, scy, int(round(ax_)), int(round(ay_)), acx, acy, layers
            return None

        def attempt(allow, factor=1, about=False):
            """The coupled run on one layer (allow False) or with layer
            changes at `factor` times the via cost; routes the pair and
            returns True, or says why not. `about`: the run ends with a turn
            about through a via pair beyond the far end's pins (the pins
            are the other way round there): the station is chosen first and
            the search aims at it on another layer."""
            pair_via_cost = via_cost * factor
            goals_, anchors_, tail = g_cells, anchors, None
            if about:
                st = station()
                if st is None:
                    if _DEBUG:
                        print(f"[pair-via] {na.name}: no room to turn about beyond the far pins", flush=True)
                    self._pair_fail(na.name, 'no_path', 'no room to turn about beyond the pins')
                    return False
                sx, sy, scx, scy, gx_, gy_, acx, acy, layers = st
                goals_ = {grid.index(l_, acx, acy) for l_ in layers}
                anchors_ = [(acx, acy)]
                tail = ((grid.nm_x(acx), grid.nm_y(acy)), (int(sx), int(sy)), (gx_, gy_))
            # with layer changes the search has every layer to look through
            pair_cap = cap * (3 if allow else 1)
            def via_runs(side_):
                """One line of a run with layer changes: its first stretch up to
                its first via, the middle stretches with their vias, its last
                stretch from its last via; and the vias. None where a stretch is
                too short to flare out and back. Where the run turns about at
                a via pair (down before a part's pin rows, straight back up
                beyond them to come into the pins from the far side) each line
                keeps its own side of the board, which is the other side of
                the direction of travel: that is how a pair whose pins are the
                other way round at one part comes in without crossing."""
                sides, vs = [side_], []
                for k in range(len(pieces) - 1):
                    pts_in, pts_out = pieces[k][1], pieces[k + 1][1]
                    jx, jy = pts_in[-1]
                    ux, uy = unit(pts_in[-2], pts_in[-1])
                    vx, vy = unit(pts_out[0], pts_out[1])
                    about = ux * vx + uy * vy < -0.7                # turning about (more than 135 degrees)
                    if about:
                        nx, ny, n_ = vy, -vx, 1.0                  # square to the stretch going back
                    elif ux * vx + uy * vy < 0.999:
                        return None                                # a via pair sits on a straight run, not at a bend
                    else:
                        nx, ny, n_ = -uy, ux, 1.0
                    sd = sides[-1]
                    vs.append((int(round(jx + nx / n_ * sd * dv)), int(round(jy + ny / n_ * sd * dv))))
                    sides.append(-sd if about else sd)
                polys = [_offset_polyline(pts, d, sd) for (_, pts), sd in zip(pieces, sides)]
                heads = []
                for k, poly in enumerate(polys):
                    if 0 < k < len(polys) - 1 and sum(math.hypot(q[0] - o[0], q[1] - o[1]) for o, q in zip(poly, poly[1:])) \
                            < PAIR_VIA_STRETCH_NM:
                        return None                                # two via pairs within a few mm: not worth it
                    if k > 0:
                        poly = trim(poly, flare)
                    if poly and k < len(polys) - 1:
                        poly = trim(poly[::-1], flare)
                        poly = poly[::-1] if poly else None
                    if not poly or len(poly) < 2:
                        return None
                    heads.append(poly)
                mid = []
                for k in range(1, len(pieces) - 1):
                    lay = pieces[k][0]
                    pts = [vs[k - 1]] + heads[k] + [vs[k]]
                    mid += [(lay, a[0], a[1], b[0], b[1], w) for a, b in zip(pts, pts[1:])]
                return heads[0] + [vs[0]], [vs[-1]] + heads[-1], mid, vs

            cells = astar.route(grid, owners, p.via_block, ca.net, s_cells, goals_, anchors_,
                                self.horizontal, pair_via_cost, allow, via_reach, pair_cap, self.opts.turn_cost, cancelled,
                                also_ok=cb.net, source_costs=s_costs, routable=self.routable, via_guard=p.via_guard,
                                lane_guard=guard, via_lead=int(math.ceil(lead_nm / grid.pitch)),
                                lead_from=lead_from)
            if not cells or len(cells) < 2:
                if _DEBUG:
                    print(f"[pair-path] {na.name} vias {allow}: no path after {astar.LAST['expansions']} expansions"
                          f"{' (cap)' if astar.LAST['capped'] else ''}, {len(s_cells)} starts, {len(g_cells)} goals", flush=True)
                self._pair_fail(na.name, 'no_path', 'no coupled path')
                return False
            own_cells = set(cells)
            # straightening keeps out of other pins' outlets as the search did
            cells = _smooth(grid, cells, lambda idx: idx in own_cells or (
                owners[idx] in (0, ca.net, cb.net) and (guard is None or guard[idx] in (0, ca.net, cb.net))))
            segments, _ = _simplify(grid, cells)
            if not segments:
                return False
            # the run's stretches, one layer each; a layer change between two is a via pair
            pieces = []
            for sg in segments:
                a_, b_ = (sg[1], sg[2]), (sg[3], sg[4])
                if pieces and pieces[-1][0] == sg[0]:
                    pieces[-1][1].append(b_)
                else:
                    pieces.append((sg[0], [a_, b_]))
            if tail is not None:
                # the chosen station: straight in on the layer the search arrived on,
                # the via pair, and the lead back to the pins on their layer
                ap_, st_, g_ = tail
                if pieces[-1][1][-1] != ap_:
                    pieces[-1][1].append(ap_)
                pieces[-1][1].append(st_)
                pieces.append((la2, [st_, g_]))
            candidates = [pieces]
            if len(pieces) > 1:
                # a short first or last stretch runs straight along the pad's
                # escape, so the via pair sits square before the pins and the
                # lines come in straight (the search may have come up a little
                # to one side); the stretches as found stay as the fallback
                pieces = [(l_, list(pts)) for l_, pts in pieces]
                candidates.insert(0, pieces)
                for at_start in (True, False):
                    lay, pts = pieces[0] if at_start else pieces[-1]
                    length = sum(math.hypot(q[0] - o[0], q[1] - o[1]) for o, q in zip(pts, pts[1:]))
                    if length > 2_500_000:
                        continue
                    cell = ea1 if at_start else ea2
                    ex, ey, px, py, _ = p.nets[ca.net].endpoints.get(cell, (None,) * 5)
                    l_, cx_, cy_ = grid.unpack(cell)
                    if ex is None or (grid.nm_x(cx_), grid.nm_y(cy_)) == (px, py):
                        continue
                    ux, uy = unit((px, py), (grid.nm_x(cx_), grid.nm_y(cy_)))
                    anchor = pts[0] if at_start else pts[-1]
                    far = (int(round(anchor[0] + ux * length)), int(round(anchor[1] + uy * length)))
                    # straight through the station on the other layer too, then
                    # an octilinear jog back onto the path the search found
                    on = max(500_000, flare + 2 * grid.pitch)
                    beyond = (int(round(far[0] + ux * on)), int(round(far[1] + uy * on)))
                    if at_start:
                        nxt = pieces[1][1]
                        rest = nxt[2:] if len(nxt) > 2 else []
                        pieces[0] = (lay, [anchor, far])
                        pieces[1] = (pieces[1][0], [far] + jog(*beyond, *nxt[1], at_end=False) + rest)
                    else:
                        prv = pieces[-2][1]
                        rest = prv[:-2] if len(prv) > 2 else []
                        pieces[-1] = (lay, [far, anchor])
                        pieces[-2] = (pieces[-2][0], rest + jog(*prv[-2], *beyond, at_end=True) + [far])
            via_cache = {}
            on = max(500_000, flare + 2 * grid.pitch)
            for pieces in candidates:
                # every via pair sits on a straight stretch: where the search
                # turned at a layer change, the run carries straight on past the
                # station on the new layer and jogs back to the path after it
                for k in range(len(pieces) - 1):
                    pts_in, (lay_out, pts_out) = pieces[k][1], pieces[k + 1]
                    ux, uy = unit(pts_in[-2], pts_in[-1])
                    vx, vy = unit(pts_out[0], pts_out[1])
                    dot = ux * vx + uy * vy
                    if dot >= 0.999 or dot < -0.7 or len(pts_out) < 2:
                        continue
                    jx, jy = pts_in[-1]
                    beyond = (int(round(jx + ux * on)), int(round(jy + uy * on)))
                    pieces[k + 1] = (lay_out, [(jx, jy)] + jog(*beyond, *pts_out[1], at_end=False) + pts_out[2:])
                points = [pt for _, pts in pieces for pt in pts]
                if len(pieces) == 1:
                    break
                if _DEBUG:
                    print(f"[pair-via] {na.name}: w {w} gap {gap} d {d:.0f} dv {dv:.0f} via {na.rules.via_diameter}/{na.rules.via_drill} clr {na.rules.clearance}", flush=True)
                    print(f"[pair-via] {na.name}: pieces " + " | ".join(
                        f"L{l_} ({pts[0][0] / 1e6:.1f},{pts[0][1] / 1e6:.1f})->({pts[-1][0] / 1e6:.1f},{pts[-1][1] / 1e6:.1f})"
                        for l_, pts in pieces), flush=True)
                for side_ in (1, -1):
                    got = via_runs(side_)
                    via_cache[side_] = got if got is not None and all(
                        via_ok(x, y, ca.net) or via_ok(x, y, cb.net) for x, y in got[3]) else None
                if all(v is not None for v in via_cache.values()):
                    break
            else:
                if _DEBUG:
                    for side_ in (1, -1):
                        got = via_runs(side_)
                        if got is None:
                            print(f"[pair-via] {na.name} side {side_}: a stretch is too short to flare "
                                  f"(pieces {[(l_, round(sum(math.hypot(q[0] - o[0], q[1] - o[1]) for o, q in zip(pts, pts[1:])) / 1e6, 2)) for l_, pts in pieces]})", flush=True)
                        else:
                            for x, y in got[3]:
                                if not (via_ok(x, y, ca.net) or via_ok(x, y, cb.net)):
                                    cx_, cy_ = grid.cell_x(x), grid.cell_y(y)
                                    om = owner_map(w)
                                    who = {p.nets[om[grid.index(li, cx_, cy_)]].name if om[grid.index(li, cx_, cy_)] in p.nets else om[grid.index(li, cx_, cy_)] for li in range(grid.layers)}
                                    print(f"[pair-via] {na.name} side {side_}: via at ({x / 1e6:.2f},{y / 1e6:.2f}) blocked: "
                                          f"via_block {p.via_block[cy_ * grid.w + cx_]} guard {p.via_guard[cy_ * grid.w + cx_] if p.via_guard is not None else None} owners {who}", flush=True)
                self._pair_fail(na.name, 'no_path', 'no room for the via pairs')
                return False
            layer_s, layer_e = pieces[0][0], pieces[-1][0]
            layer = layer_s                                # the swap works on a single-layer run
            # which side of the centreline each net runs on, decided at the start
            # and checked at the end (a twist would need a crossing)
            # The direction is the run's over its first (last) millimetre, not
            # its first segment alone: a one-cell jog where the run leaves the
            # midpoint between the pins would otherwise decide the side at random.
            def heading(pts, reach):
                x0, y0 = pts[0]
                left = reach
                for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                    seg = math.hypot(x2 - x1, y2 - y1)
                    if seg >= left:
                        f = left / seg
                        return x1 + (x2 - x1) * f - x0, y1 + (y2 - y1) * f - y0
                    left -= seg
                return pts[-1][0] - x0, pts[-1][1] - y0

            reach = max(1_000_000, 4 * grid.pitch)

            def side_at(px, py, at_end):
                pts = points[::-1] if at_end else points
                ux, uy = heading(pts, reach)
                if at_end:
                    ux, uy = -ux, -uy                      # the direction of travel, arriving
                vx, vy = px - pts[0][0], py - pts[0][1]
                # which side of the run a line is on: its pin against the partner's
                return ux * vy - uy * vx

            def side(xa, ya, xb, yb, at_end):
                da = side_at(grid.nm_x(xa), grid.nm_y(ya), at_end)
                db = side_at(grid.nm_x(xb), grid.nm_y(yb), at_end)
                return 1 if da - db > 0 else -1
            # which side each line runs on: a first guess from the run's heading,
            # then the other; a side is right when the two finished tracks (pad
            # entries included) neither cross nor come within clearance: that
            # is what a crossing pair really is, whatever the run's direction
            first = side(xa1, ya1, xb1, yb1, False)
            need = na.rules.clearance + w - 10           # 10 nm: the offsets are rounded to whole nanometres

            clear_cache = {}

            def seg_clear(seg, code):
                """A track piece keeps clear of every net but its own: the
                partner's pins and its tracks on other stretches of the pair are
                copper like any other (only the centreline was searched)."""
                key_ = (seg, code)
                ok = clear_cache.get(key_)
                if ok is None:
                    lay, x1, y1, x2, y2, ww = seg
                    om = owner_map(ww)
                    ok = True
                    if om is not None:
                        for idx in grid.segment_cells(lay, x1, y1, x2, y2, grid.pitch // 2):
                            o = om[idx]
                            if o != 0 and o != code:
                                if _DEBUG:
                                    who = p.nets[o].name if o in p.nets else o
                                    print(f"[pair-blocked] {p.nets[code].name} seg {seg} hits {who}", flush=True)
                                ok = False
                                break
                    clear_cache[key_] = ok
                return ok

            def apart(segs_a, segs_b):
                return min((_segment_distance(a[1:5], b[1:5]) for a in segs_a for b in segs_b), default=need)

            def run_length(poly):
                return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(poly, poly[1:]))

            def octilinear(ax, ay, bx, by):
                return ax == bx or ay == by or abs(abs(bx - ax) - abs(by - ay)) <= 2

            def entries(net, end_cell, join_to, layer):
                """Ways from a pin's exit onto the coupled run, best first: on
                along the escape then a 45-degree step (a designer's dog-leg),
                the 45 first then straight, the two other octilinear bends, and
                last a single slanted step. Each starts at the pad."""
                ex, ey, px, py, sw = net.endpoints.get(end_cell, (None,) * 5)
                l_, cx, cy = grid.unpack(end_cell)
                gx, gy = grid.nm_x(cx), grid.nm_y(cy)
                pad = []
                if ex is not None:
                    if (ex, ey) != (px, py) and (ex, ey) != (gx, gy):
                        pad += [(layer, px, py, ex, ey, sw), (layer, ex, ey, gx, gy, sw)]
                    elif (px, py) != (gx, gy):
                        pad.append((layer, px, py, gx, gy, sw))
                # the step onto the run stays at the pin's escape width (necked at fine pitch)
                ew = min(w, sw) if (ex is not None and sw) else w
                jx, jy = join_to
                if (gx, gy) == (jx, jy):
                    return [pad]
                out, seen = [], set()

                def add(bends):
                    pts = [(gx, gy)] + [b for b in bends if b not in ((gx, gy), (jx, jy))] + [(jx, jy)]
                    key_ = tuple(pts)
                    if key_ in seen:
                        return
                    seen.add(key_)
                    out.append(pad + [(layer, a[0], a[1], b[0], b[1], ew) for a, b in zip(pts, pts[1:])])

                if ex is not None and (gx, gy) != (px, py):
                    ux, uy = gx - px, gy - py
                else:
                    ux, uy = jx - gx, jy - gy
                norm = math.hypot(ux, uy) or 1.0
                ux, uy = ux / norm, uy / norm
                along = (jx - gx) * ux + (jy - gy) * uy
                perp = math.hypot((jx - gx) - along * ux, (jy - gy) - along * uy)
                straight = along - perp
                if straight > grid.pitch:
                    add([(int(round(gx + ux * straight)), int(round(gy + uy * straight)))])
                dx, dy = jx - gx, jy - gy
                sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
                m = min(abs(dx), abs(dy))
                if octilinear(gx, gy, jx, jy):
                    add([])
                else:
                    # diagonal first, then straight on; and straight first, then the diagonal
                    add([(gx + sx * m, gy + sy * m)])
                    add([(jx - sx * m, jy - sy * m)])
                    add([])
                return out

            # a via swap: where the pins are in opposite order at the two ends,
            # one line drops through a via, crosses under its partner at 45
            # degrees on another layer and comes back up on the other side; the
            # partner steps across on the run's layer. e keeps the vias clear
            # of the partner line, and swap_half is half the stretch it takes.
            need_v = vr + na.rules.clearance + w / 2.0
            e_sw = max(need_v - 2 * d, need_v / math.sqrt(2) - d, 0) + grid.pitch / 2
            swap_half = d + 2 * e_sw

            def point_at(s_):
                """The centreline point s_ nm along it, and the direction there."""
                for (x1, y1), (x2, y2) in zip(points, points[1:]):
                    seg = math.hypot(x2 - x1, y2 - y1)
                    if seg and s_ <= seg:
                        ux, uy = (x2 - x1) / seg, (y2 - y1) / seg
                        return x1 + ux * s_, y1 + uy * s_, ux, uy
                    s_ -= seg
                return None

            def swap_spots():
                """Where a swap fits: the middle of a straight stretch of the run
                long enough for it, nearest the ends first (a designer swaps
                next to the part whose pins are the other way round)."""
                total = run_length(points)
                out, s0 = [], 0.0
                margin = 2 * grid.pitch
                lead = 1_000_000                           # clear of the pad entries: they need the run's ends
                for (x1, y1), (x2, y2) in zip(points, points[1:]):
                    seg = math.hypot(x2 - x1, y2 - y1)
                    s_ = s0 + swap_half + margin
                    while s_ <= s0 + seg - swap_half - margin:
                        if lead <= s_ - swap_half and s_ + swap_half <= total - lead:
                            out.append(s_)
                        s_ += 250_000
                    s0 += seg
                out.sort(key=lambda v: min(v, total - v))
                if _DEBUG:
                    print(f"[pair-swap] {na.name}: run {total / 1e6:.2f} mm, spots {[round(v / 1e6, 2) for v in out[:12]]}", flush=True)
                return out[:12]

            def swap_runs(sa, hopper, s_c):
                got = point_at(s_c)
                if got is None:
                    return None
                cx_, cy_, ux, uy = got
                nx, ny = -uy, ux                           # side +1, as _offset_polyline has it

                def at(al, lat):
                    return (int(round(cx_ + ux * al + nx * lat)), int(round(cy_ + uy * al + ny * lat)))
                before = trim(points[::-1], total_c - (s_c - swap_half))
                after = trim(points, s_c + swap_half)
                if not before or not after:
                    return None
                before = before[::-1]
                runs_s, runs_e, middle, vias = {}, {}, {}, {}
                for code, s_ in ((ca.net, sa), (cb.net, -sa)):
                    b_off = _offset_polyline(before, d, s_)
                    a_off = _offset_polyline(after, d, -s_)
                    if code == hopper:
                        v1 = at(-d - e_sw, s_ * (d + e_sw))
                        v2 = at(d + e_sw, -s_ * (d + e_sw))
                        if not (via_ok(*v1, code) and via_ok(*v2, code)):
                            return None
                        under = None
                        others = [l for l in range(grid.layers) if l != layer and self.routable[l]]
                        # an outer layer first: a plane's layer is best left whole
                        others.sort(key=lambda l: (0 if l in (0, grid.layers - 1) else 1, -abs(l - layer)))
                        for l in others:
                            sg = (l, v1[0], v1[1], v2[0], v2[1], w)
                            if seg_clear(sg, code):
                                under = sg
                                break
                        if under is None:
                            return None
                        runs_s[code] = b_off + [v1]
                        runs_e[code] = [v2] + a_off
                        middle[code] = [under]
                        vias[code] = [v1, v2]
                    else:
                        full = b_off + [at(-d, s_ * d), at(d, -s_ * d)] + a_off
                        runs_s[code] = runs_e[code] = full
                return runs_s, runs_e, middle, vias

            total_c = run_length(points)

            def acute(segs):
                """A line that doubles back (a corner sharper than 90 degrees)."""
                for a, b in zip(segs, segs[1:]):
                    if a[0] != b[0] or (a[3], a[4]) != (b[1], b[2]):
                        continue
                    ux, uy, vx, vy = a[3] - a[1], a[4] - a[2], b[3] - b[1], b[4] - b[2]
                    if ux * vx + uy * vy < -0.01 * math.hypot(ux, uy) * math.hypot(vx, vy):
                        return True
                return False

            def build(sa, swap=None, loose=None):
                """Both lines for one choice of sides, or None. Each end gets its
                pad entries chosen together: the first shapes, joined to the run
                where it begins or a little further along it, that keep clear of
                other copper and of the partner line. With a swap (the line
                that hops, where along the run) the lines change sides there.
                With `loose` (True: the start, False: the far end) that end
                gets no entries: the run stops short of it and each line is
                finished there single-ended by the caller."""
                runs_s, runs_e, middle, vias = {}, {}, {}, {}
                total = run_length(points)
                room = {True: total, False: total}
                room_keep = grid.pitch
                if swap is None and len(pieces) == 1:
                    for net, code, side_ in ((na, ca.net, sa), (nb, cb.net, -sa)):
                        runs_s[code] = runs_e[code] = _offset_polyline(points, d, side_)
                elif swap is None:
                    for net, code, side_ in ((na, ca.net, sa), (nb, cb.net, -sa)):
                        got = via_cache.get(side_)
                        if got is None or not all(via_ok(x, y, code) for x, y in got[3]):
                            return None, "blocked"
                        runs_s[code], runs_e[code], middle[code], vias[code] = got
                    room = {True: run_length(runs_s[ca.net]), False: run_length(runs_e[ca.net])}
                    room_keep = flare + grid.pitch
                else:
                    got = swap_runs(sa, *swap)
                    if got is None:
                        return None, "blocked"
                    runs_s, runs_e, middle, vias = got
                    room = {True: swap[1] - swap_half, False: total - swap[1] - swap_half}
                    room_keep = 500_000                    # the run leads into the swap straight
                step = max(grid.pitch * 2, 250_000)
                trims = {end: [k * step for k in range(0, 7)
                               if k == 0 or (k * step < total * 0.3 and k * step < room[end] - room_keep)]
                         for end in (True, False)}
                ends = {}
                for at_start, (a_cell, b_cell) in ((True, (ea1, eb1)), (False, (ea2, eb2))):
                    if loose is not None and (loose == "both" or at_start == loose):
                        ends[at_start] = None
                        continue
                    best = None
                    options = {}
                    for net, code, cell in ((na, ca.net, a_cell), (nb, cb.net, b_cell)):
                        run = runs_s[code] if at_start else runs_e[code][::-1]
                        opts = []
                        for ti, t in enumerate(trims[at_start]):
                            rest = trim(run, t) if t else run
                            if rest is None or len(rest) < 2:
                                continue
                            for si_, ent in enumerate(entries(net, cell, rest[0], layer_s if at_start else layer_e)):
                                if all(seg_clear(sg, code) for sg in ent):
                                    # an entry that meets the run at an acute angle (doubling back
                                    # onto it) only if nothing else will do
                                    bent = 0
                                    if ent:
                                        l_ = ent[-1]
                                        ux, uy = l_[3] - l_[1], l_[4] - l_[2]
                                        vx, vy = rest[1][0] - rest[0][0], rest[1][1] - rest[0][1]
                                        if ux * vx + uy * vy < -0.01 * math.hypot(ux, uy) * math.hypot(vx, vy):
                                            bent = 30
                                    opts.append((ti * 3 + si_ + bent, t, ent))
                        options[code] = opts
                    near_cache = {}

                    def near(code, t):
                        """The first stretch of a line's coupled run from this end."""
                        got = near_cache.get((code, t))
                        if got is None:
                            run = runs_s[code] if at_start else runs_e[code][::-1]
                            run = (trim(run, t) if t else run) or []
                            got = [(layer_s if at_start else layer_e, a[0], a[1], b[0], b[1], w)
                                   for a, b in list(zip(run, run[1:]))[:3]]
                            near_cache[(code, t)] = got
                        return got
                    widest = None
                    for ca_cost, ta, ent_a in options[ca.net]:
                        for cb_cost, tb, ent_b in options[cb.net]:
                            cost = ca_cost + cb_cost + 2 * abs(ta - tb) / step
                            if best is not None and cost >= best[0]:
                                continue
                            g_ = min(apart(ent_a, ent_b), apart(ent_a, near(cb.net, tb)), apart(ent_b, near(ca.net, ta)))
                            widest = g_ if widest is None else max(widest, g_)
                            if g_ < need:
                                continue
                            best = (cost, ta, ent_a, tb, ent_b)
                    if best is None:
                        if _DEBUG:
                            print(f"[pair-end] {na.name} {'start' if at_start else 'end'} side {sa}: "
                                  f"options {len(options[ca.net])}/{len(options[cb.net])} widest {widest}", flush=True)
                            for code in (ca.net, cb.net):
                                for cost_, t_, ent_ in options[code][:2]:
                                    pts_ = ([(round(sg[1] / 1e6, 2), round(sg[2] / 1e6, 2)) for sg in ent_]
                                            + [(round(ent_[-1][3] / 1e6, 2), round(ent_[-1][4] / 1e6, 2))]) if ent_ else []
                                    nr_ = [(round(sg[1] / 1e6, 2), round(sg[2] / 1e6, 2), round(sg[3] / 1e6, 2), round(sg[4] / 1e6, 2))
                                           for sg in near(code, t_)[:2]]
                                    print(f"[pair-end]   {p.nets[code].name} cost {cost_} trim {t_ / 1e6:.2f}: entry {pts_} run {nr_}", flush=True)
                        if not options[ca.net] or not options[cb.net]:
                            return None, "blocked"
                        return None, ("cross" if widest is not None and widest <= 0 else "close")
                    ends[at_start] = best
                def assemble(ends, loose_trim):
                    """The two lines from the chosen pad entries (an end without
                    entries is left loose_trim short)."""
                    paths = []
                    for net, conn, e_key, cl_a, cl_b in ((na, ca, 0, ca.a, ca.b), (nb, cb, 1, cb_a, cb_b)):
                        s_end, g_end = ends[True], ends[False]
                        ts, ent_s = (s_end[1 + 2 * e_key], s_end[2 + 2 * e_key]) if s_end else (loose_trim, [])
                        tg, ent_g = (g_end[1 + 2 * e_key], g_end[2 + 2 * e_key]) if g_end else (loose_trim, [])
                        def piece(run, lay=layer_s):
                            return [(lay, run[i][0], run[i][1], run[i + 1][0], run[i + 1][1], w) for i in range(len(run) - 1)]
                        rs, re_ = runs_s[conn.net], runs_e[conn.net]
                        if rs is re_:
                            run = rs
                            if ts:
                                run = trim(run, ts)
                            if tg and run:
                                run = trim(run[::-1], tg)
                                run = run[::-1] if run else None
                            if not run or len(run) < 2:
                                return None, "close"
                            core = piece(run)
                        else:
                            # the line that hops: its run to the first via, the
                            # crossing underneath, its run on from the second
                            head = trim(rs, ts) if ts else rs
                            tail = trim(re_[::-1], tg) if tg else re_[::-1]
                            if not head or not tail or len(head) < 2 or len(tail) < 2:
                                return None, "close"
                            core = piece(head, layer_s) + middle[conn.net] + piece(tail[::-1], layer_e)
                        if not all(seg_clear(sg, conn.net) for sg in core):
                            return None, "blocked"
                        back = [(l, x2, y2, x1, y1, ww) for l, x1, y1, x2, y2, ww in reversed(ent_g)]
                        segs = ent_s + core + back
                        if swap is not None and acute(core):
                            if _DEBUG:
                                print(f"[pair-swap] {net.name} {swap}: doubles back", flush=True)
                            return None, "close"
                        rp = RoutedPath(net.code, w, _merge_collinear(segs), list(vias.get(conn.net, [])),
                                        net.rules.via_diameter, net.rules.via_drill, pid=self.next_pid + len(paths))
                        paths.append((rp, conn, cl_a, cl_b))
                    return paths, ""

                if loose is None:
                    return assemble(ends, 0)
                # the loose end: the run stops short of it, as little as lets
                # both lines clear the pins there (big through-hole pads need more)
                got = (None, "blocked")
                for k in LOOSE_TRIM_STEPS:
                    t = min(LOOSE_TRIM_NM, total * 0.2) if k == 1 else LOOSE_TRIM_NM * k
                    if t > total * 0.35:
                        break
                    got = assemble(ends, t)
                    if got[0] is not None:
                        break
                return got

            def pair_apart(ra, rb):
                """How far apart two finished lines are, as a track-to-track
                centre distance: segments on one layer, and each line's vias
                against the other's segments on any layer."""
                g_ = min((_segment_distance(a[1:5], b[1:5]) for a in ra.segments for b in rb.segments
                          if a[0] == b[0]), default=need)
                for r1, r2 in ((ra, rb), (rb, ra)):
                    for vx, vy in r1.vias:
                        for b in r2.segments:
                            dv = _segment_distance((vx, vy, vx, vy), b[1:5]) - vr + w / 2.0
                            g_ = min(g_, dv)
                return g_

            paths, closest, blocked = None, None, False
            attempts = [(sa, None) for sa in (first, -first)]
            swaps = []
            if self.opts.pair_via_swap and getattr(self, '_allow_swap', True) and len(pieces) == 1:
                spots = swap_spots()
                swaps = [(sa, (hop, s_)) for s_ in spots for sa in (first, -first) for hop in (ca.net, cb.net)]
            swapped, plain = False, None
            for n_try, (sa, swap) in enumerate(attempts + swaps):
                if swap is not None:
                    # only when coupling failed for crossing (or pinching) lines,
                    # never for a run walled in by other copper
                    if n_try == len(attempts):
                        if closest is None:
                            break
                        plain = (closest, blocked)
                    if cancelled is not None and cancelled():
                        break
                cand, why = build(sa, swap)
                if _DEBUG and swap is not None and cand is None:
                    print(f"[pair-swap] {na.name} side {sa} {swap}: {why}", flush=True)
                if cand is None:
                    blocked = blocked or why == "blocked"
                    if why == "cross":
                        closest = 0 if closest is None else max(closest, 0)
                    elif why == "close":
                        closest = 1 if closest is None else max(closest, 1)
                    continue
                gap_ = pair_apart(cand[0][0], cand[1][0])
                if _DEBUG and gap_ < need:
                    print(f"[pair-halves] {na.name} side {sa} swap {swap}: halves {gap_:.0f} apart, need {need}", flush=True)
                if gap_ >= need:
                    paths = cand
                    swapped = swap is not None
                    break
                closest = gap_ if closest is None else max(closest, gap_)
            loose_end = None
            if paths is None and self.opts.pair_loose_ends and getattr(self, '_allow_swap', True) \
                    and (closest is not None or blocked) \
                    and not (cancelled is not None and cancelled()):
                # The run fits but an end does not let both lines in side by
                # side: keep the coupled run and finish that end single-ended
                # (the result notes it), rather than give the whole pair up.
                for loose in (False, True, "both"):
                    for sa in (first, -first):
                        cand, why = build(sa, None, loose)
                        if _DEBUG:
                            print(f"[pair-loose] {na.name} loose {loose} side {sa}: "
                                  f"{why or round(pair_apart(cand[0][0], cand[1][0]))}", flush=True)
                        if cand is None or pair_apart(cand[0][0], cand[1][0]) < need:
                            continue
                        if self._commit_loose(cand, loose, cells, via_cost, cap, on_path, cancelled):
                            at = [ca.a, ca.b] if loose == "both" else [ca.a if loose else ca.b]
                            ends_refs = set().union(*(na.clusters[e].refs for e in at))
                            where = "/".join(sorted(ends_refs)) or "?"
                            self.pair_loose = getattr(self, "pair_loose", [])
                            self.pair_loose.append({"nets": [na.name, nb.name], "end": where})
                            self.pair_log.append((na.name, f"coupled; the {where} end{'s' if loose == 'both' else ''} connected single-ended "
                                                           f"(no room to enter the pins side by side)"))
                            return True
                        break
            if paths is None:
                if plain is not None:
                    closest, blocked = plain           # why the plain run failed, not the swaps
                # the halves cross (touch) both ways round, or only come too close
                if closest is not None and closest <= 0:
                    self._pair_fail(na.name, 'cross', 'pair would cross')
                elif blocked:
                    self._pair_fail(na.name, 'blocked', 'entries would run into other copper')
                else:
                    self._pair_fail(na.name, 'pinch', 'halves would pinch: routed separately')
                return False
            if len(pieces) > 1:
                self.pair_vias = getattr(self, "pair_vias", 0) + len(pieces) - 1
                self.pair_log.append((na.name, f"coupled through {len(pieces) - 1} via pair(s)"))
            if swapped:
                self.pair_swaps = getattr(self, "pair_swaps", 0) + 1
                self.pair_log.append((na.name, "coupled with a via swap (pins in opposite order at the two ends)"))
            self.next_pid += 2
            for rp, conn, cl_a, cl_b in paths:
                path = _Path(rp.pid, conn, rp, cells, paired=True)
                self.paths[rp.pid] = path
                self._claim(path)
                self.parent[conn.net][self._find(conn.net, cl_a)] = self._find(conn.net, cl_b)
                if on_path:
                    on_path(rp)
            return True

        # on one layer first; then with layer changes, dear ones unless the
        # single-layer run would cross: a pair whose pins are the other way
        # round at one part turns about through a via pair (down before the
        # part's pin rows, up beyond them, back in from the far side), and
        # that is worth two vias
        first_why = ""
        plan = [(False, 1)]
        long_run = span * grid.pitch >= PAIR_VIA_SPAN_NM
        if self.opts.allow_vias and long_run:
            plan.append((True, max(1, self.opts.pair_direct_via_factor)))
        for n_, step_ in enumerate(plan):
            if attempt(*step_):
                return True
            first_why = first_why or self._pair_why
            if cancelled is not None and cancelled():
                break
            if n_ == 0 and self.opts.allow_vias and long_run and self._pair_why in ("cross", "pinch"):
                plan.append((True, 1, True))
        if first_why and self._pair_why == 'no_path':
            self._pair_why = first_why                 # the single-layer run's reason says more
        return False

    def _try(self, conn: Connection, via_cost: int, cap: int, on_path, cancelled) -> bool:
        if self._find(conn.net, conn.a) == self._find(conn.net, conn.b):
            return True                        # already joined through other paths
        cells = self._search(conn, via_cost, cap, False, cancelled)
        if not cells:
            return False
        if self.opts.flat_first:
            cells = self._flat_route(conn, cells, cap, cancelled)
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
        segments = _with_pad_entries(grid, net, cells, segments, int(self.opts.neck_length_mm * 1_000_000))
        if self.opts.cut_acute:
            segments = _cut_acute(grid, segments, lambda idx: idx in own_cells or owners[idx] in (0, code))
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
        # the direction the stub arrives at the exit in: the run to the via
        # must not turn back past 90 degrees against it (a via dropped a cell
        # behind the exit made a 45-degree hairpin at the mouth of the stub)
        if (ex, ey) != (px, py) and (ex, ey) != (gx, gy):
            arrive = (gx - ex, gy - ey)
        else:
            arrive = (gx - px, gy - py)

        def fits(vx: int, vy: int, hairpin_ok: bool) -> bool:
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
                if not hairpin_ok and len(pts) >= 2 \
                        and (pts[1][0] - pts[0][0]) * arrive[0] + (pts[1][1] - pts[0][1]) * arrive[1] < 0:
                    continue                      # the first leg would double back on the stub
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
        # The nearest spot whose run leaves the exit without turning back on
        # the stub; a spot that needs a hairpin is kept only as the fallback,
        # and a clean spot is preferred only up to two cells farther out, so
        # the vias stay where they were for the signals routed after them.
        seen = set()
        fallback = None                           # (vx, vy, run) of the nearest hairpin spot
        for vx, vy in candidates:
            if (vx, vy) in seen:
                continue
            seen.add((vx, vy))
            if fallback is not None:
                limit = math.hypot(fallback[0] - ecx, fallback[1] - ecy) + 2.0
                if math.hypot(vx - ecx, vy - ecy) > limit:
                    break
            if fits(vx, vy, hairpin_ok=False):
                fallback = None
                break
            if fallback is None and fits(vx, vy, hairpin_ok=True):
                fallback = (vx, vy, run_pts[0])
        else:
            if fallback is None:
                return None
        if fallback is not None:
            vx, vy, run_pts[0] = fallback
        vxn, vyn = grid.nm_x(vx), grid.nm_y(vy)
        segments = []
        if (ex, ey) != (px, py) and (ex, ey) != (gx, gy):
            segments += [(layer, px, py, ex, ey, stub_w), (layer, ex, ey, gx, gy, stub_w)]
        else:
            segments.append((layer, px, py, gx, gy, stub_w))
        if (vxn, vyn) != (gx, gy):
            pts = run_pts[0] or jog(gx, gy, vxn, vyn)
            segments += [(layer, pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1], stub_w) for k in range(len(pts) - 1)]
        if self.opts.cut_acute:
            segments = _cut_acute(grid, segments, lambda idx: own[idx] in (0, code))
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
        segments = _with_pad_entries(grid, net, cells, segments, int(self.opts.neck_length_mm * 1_000_000))
        if self.opts.cut_acute:
            own_cells = set(cells)
            segments = _cut_acute(grid, segments, lambda idx: idx in own_cells or owners[idx] in (0, code))
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
        started, clock_started = time.time(), astar.clock()
        # the routing groups in their order (pairs, memory, high speed, clocks,
        # power, the rest); within a group fine-pitch escapes first (they have
        # the least room), then shortest first
        # A differential pair's stretches go longest first: the coupled run
        # between the two parts gets the room at the pins before the short
        # hops (an ESD array's pass-through, a connector's twin pins) do.
        # After the pairs, a connection with a fine-pitch end (a connector
        # row, a QFN walled in by its neighbours) of any group goes before
        # the open-field connections of every group: its escape cell is the
        # one place it can leave, and a long bus or power run of an earlier
        # group would otherwise take it on the way past.
        queue = sorted(self.p.connections,
                       key=lambda c: (0 if c.pair else 1 if c.priority == 0 else 2, c.rank,
                                      -c.estimate if c.pair else c.priority, c.estimate))
        total = len(queue)
        failed: List[Connection] = []
        done = 0

        def report(name, stage=""):
            # the stage goes in front of the net name so a status line (or the
            # dialog log) shows which phase of the run is working, not only
            # the counter that stands still through the rip-up rounds
            if progress:
                progress(done, total, len(self.paths), len(failed), f"{stage}{name}")

        deadline = clock_started + opts.time_limit_s if opts.time_limit_s and opts.time_limit_s > 0 else None

        def late():
            if deadline is not None and astar.clock() > deadline:
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

        def budget(scale: float = 1.0):
            """stop(), or this connection's own time is up: one search that
            wanders a big board (or a rip-up attempt rerouting everything it
            took out) cannot hold up the whole job."""
            if not opts.conn_time_s or opts.conn_time_s <= 0:
                return stop
            until = astar.clock() + opts.conn_time_s * scale

            def over():
                if astar.clock() > until:
                    self.budget_cuts += 1
                    return True
                return stop()
            return over

        self.budget_cuts = 0
        self.undone = 0

        # 0. pads close together with a clear line between them: a direct
        # track. Drawn once the differential pairs have their runs: a wide
        # power link across a pin field would otherwise take the room a pair
        # needs to leave its pins side by side.
        self.direct_links = 0
        direct_done = [not opts.direct_links]

        def direct_pass():
            nonlocal done
            direct_done[0] = True
            for conn in sorted(queue, key=lambda c: c.estimate):
                if stop():
                    break
                if self._direct(conn, on_path):
                    self.direct_links += 1
                    done += 1
            report(f"{self.direct_links} pads joined directly", "direct links: ")

        # 1. plain pass; a differential pair's two connections go together
        partners = {}
        for conn in queue:
            if conn.pair:
                partners.setdefault(conn.pair, []).append(conn)
        handled = set()
        self.pairs_routed = 0
        self.pair_exceptions: List[dict] = []
        # pairs whose lines cross: a via swap is tried once every other pair
        # has its plain run, so a long swapped pair cannot take the room a
        # pair that runs straight through needs
        deferred: List[Tuple[Connection, Connection]] = []

        def swapped_pairs():
            self._allow_swap = True
            while deferred:
                ca, cb = deferred.pop(0)
                if stop():
                    failed.extend(c for c in (ca, cb) if self._find(c.net, c.a) != self._find(c.net, c.b))
                    continue
                if self._try_pair(ca, cb, opts.via_cost, opts.max_expansions, on_path, budget(PAIR_BUDGET)):
                    self.pairs_routed += 1
                else:
                    self._route_exception(ca, cb, opts.via_cost, opts.max_expansions, on_path, budget(PAIR_BUDGET), failed)
                report(self.p.nets[ca.net].name)

        self._allow_swap = not (opts.pair_via_swap or opts.pair_loose_ends)   # first pass: plain coupled runs only...
        early = PAIR_EARLY_SWAP_NM
        for conn in queue:
            if id(conn) in handled:
                continue
            if deferred and not conn.pair:
                swapped_pairs()
            if not direct_done[0] and not conn.pair:
                direct_pass()
            if stop():
                if self._find(conn.net, conn.a) != self._find(conn.net, conn.b):
                    failed.append(conn)               # out of time: left open, reported as such
                continue
            mates = partners.get(conn.pair, []) if conn.pair else []
            if len(mates) == 2:
                ca, cb = mates
                handled.add(id(ca)); handled.add(id(cb))
                # ...but a long run (where coupling matters most) may swap or end
                # single-ended at once: deferred, it finds the room taken
                allow = self._allow_swap
                self._allow_swap = allow or ca.estimate * self.grid.pitch >= early
                try:
                    ok = self._try_pair(ca, cb, opts.via_cost, opts.max_expansions, on_path, budget(PAIR_BUDGET))
                finally:
                    self._allow_swap = allow
                if ok:
                    self.pairs_routed += 1
                    done += 2
                    report(self.p.nets[ca.net].name)
                    continue
                if not self._allow_swap and ca.estimate * self.grid.pitch < early \
                        and (self._pair_why in ("cross", "pinch")
                             or (opts.pair_loose_ends and self._pair_why == "blocked")):
                    deferred.append((ca, cb))
                    done += 2
                    continue
                # an exception: coupling impossible or unnecessary, connect each line directly
                self._route_exception(ca, cb, opts.via_cost, opts.max_expansions, on_path, budget(PAIR_BUDGET), failed)
                done += 2
                report(self.p.nets[ca.net].name)
                continue
            if not self._try(conn, opts.via_cost, opts.max_expansions, on_path, budget()):
                failed.append(conn)
            done += 1
            report(self.p.nets[conn.net].name)
        swapped_pairs()
        if not direct_done[0]:
            direct_pass()

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
                attempt = budget(2)
                cells = self._search(conn, opts.via_cost, opts.max_expansions * 2, True, attempt)
                if not cells and self.restore_possible and not stop():
                    # walled in by the plane nets' pad vias (a decoupling via in
                    # the only corridor): a designer moves the via, so may the
                    # router, once no route through signal tracks alone exists
                    self._rip_planes = True
                    try:
                        cells = self._search(conn, opts.via_cost, opts.max_expansions * 2, True, attempt)
                    finally:
                        self._rip_planes = False
                if not cells:
                    if _DEBUG:
                        print(f"[rip-up {rnd + 1}] {self.p.nets[conn.net].name}: no soft path "
                              f"(expansions {astar.LAST['expansions']}, capped {astar.LAST['capped']})", flush=True)
                    still.append(conn)
                    continue
                blockers = sorted(self._blockers(conn, cells))
                if _DEBUG:
                    names = [f"{self.p.nets[self.paths[b].conn.net].name}{'*' if self.paths[b].existing else ''}"
                             for b in blockers]
                    print(f"[rip-up {rnd + 1}] {self.p.nets[conn.net].name}: {len(cells)} cells through {names}", flush=True)
                if len(blockers) > MAX_BLOCKERS:
                    still.append(conn)
                    continue
                self._remember_conflict(conn, cells, set(blockers))
                before = dict(self.paths)
                marks = (len(self.restore), len(self.ripped_existing), self.shoved)
                ripped = self._rip(blockers, on_remove)
                sizes = {c.net: len(self.p.nets[c.net].clusters) for c in [conn] + ripped}
                won = self._try(conn, opts.via_cost, opts.max_expansions * 2, on_path, attempt)
                lost = []
                for other in ripped:
                    # each one ripped up gets its own time to find its way back
                    joined = self._find(other.net, other.a) == self._find(other.net, other.b)
                    ok = self._try(other, opts.via_cost, opts.max_expansions, on_path, budget())
                    if _DEBUG:
                        print(f"[reroute] {self.p.nets[other.net].name} {other.a}-{other.b} joined-before={joined} -> {ok}", flush=True)
                    if not ok:
                        lost.append(other)
                lost = [c for c in lost if self._find(c.net, c.a) != self._find(c.net, c.b)]
                if (0 if won else 1) + len(lost) > 1 and all(len(self.p.nets[n].clusters) == k for n, k in sizes.items()):
                    # more open than before: put the board back as it was (a
                    # reshuffle that costs nothing stays, it may make room)
                    self._undo_rip(before, marks, on_path, on_remove)
                    self.undone += 1
                    still.append(conn)
                    continue
                result.ripped += len(ripped)
                if won:
                    progressed = True
                else:
                    still.append(conn)
                still.extend(lost)
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
                if not self._try(conn, max(5, opts.via_cost // 3), opts.max_expansions * 3, on_path, budget(3)):
                    still.append(conn)
                report(self.p.nets[conn.net].name, "last chance: ")
            failed = still

        # 4. fanout vias for pour nets, in whatever room is left (the time
        # limit does not cut the pour stages: they are bounded and quick)
        fanout_paths: List[RoutedPath] = []
        fanned: Set[int] = set()                       # primary exit cells of pads that got a via
        # the pads a rip-up took the via from come after the fanout list
        wanted = list(self.p.fanout) + [rc for rc in self.restore if rc not in self.p.fanout]
        if not halted():
            for i, (code, cell) in enumerate(wanted):
                if halted():
                    break
                if i % 10 == 0:
                    report(f"{i}/{len(wanted)}", "fanout vias ")
                # a pad whose via is stranded in a pour island: a spot next to
                # the pad would land in that island again, only the search
                # to the plane's main fill helps
                strict = cell in self.p.fanout_strict
                for exit_cell in ([] if strict else [cell] + self.p.fanout_alt.get(cell, [])):
                    rp = self._fanout_via(code, exit_cell)
                    if _DEBUG and (code, cell) in self.restore:
                        print(f"[restore-via] net {self.p.nets[code].name} exit {self.grid.unpack(exit_cell)} -> {rp.vias if rp else None}", flush=True)
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

        # 5. stitching vias on the pour nets, where they serve: a candidate
        # spot gets a via only when a signal changes layer near it (the
        # signal's return current needs a path between the planes there)
        # and the pour has no via within reach yet (a pad's fanout via, or
        # a stitch placed before). The rest of the plane is left alone, as
        # a designer leaves it; a blanket of vias is not artwork.
        stitch_paths: List[RoutedPath] = []
        if self.p.stitch and not halted():
            reach = int(self.opts.stitch_pitch_mm * 1_000_000 * 0.75)      # beyond the candidate grid's half diagonal
            changes: List[Tuple[int, int]] = []                              # where the signals change layer
            pour_vias: Dict[int, List[Tuple[int, int]]] = {}                 # pour net -> its vias so far

            def _is_pour(code: int) -> bool:
                return code in self.p.stitch or (code in self.p.nets and not self.p.nets[code].route)

            # the vias on the board already (the signal passes before this
            # one, or a routed board), then this pass's own
            for code, vias in self.p.board_vias.items():
                if _is_pour(code):
                    pour_vias.setdefault(code, []).extend(vias)
                elif code > 0:
                    changes.extend(vias)
            for path in self.paths.values():
                if path.existing:
                    continue
                code = path.conn.net
                if _is_pour(code):
                    pour_vias.setdefault(code, []).extend(path.path.vias)
                else:
                    changes.extend(path.path.vias)
            for rp in fanout_paths:
                pour_vias.setdefault(rp.net, []).extend(rp.vias)

            def _near(points, x, y) -> bool:
                for vx, vy in points:
                    if abs(vx - x) <= reach and abs(vy - y) <= reach and (vx - x) ** 2 + (vy - y) ** 2 <= reach * reach:
                        return True
                return False

            def _nearest_change(x, y) -> int:
                best = -1
                for vx, vy in changes:
                    if abs(vx - x) > reach or abs(vy - y) > reach:
                        continue
                    d = (vx - x) ** 2 + (vy - y) ** 2
                    if d <= reach * reach and (best < 0 or d < best):
                        best = d
                return best

            for code, spots in self.p.stitch.items():
                if halted():
                    break
                # the spots closest to a layer change first: the via lands
                # beside the change, and the spots around it are then covered
                ranked = []
                for cx, cy in spots:
                    d = _nearest_change(self.grid.nm_x(cx), self.grid.nm_y(cy))
                    if d >= 0:
                        ranked.append((d, cy, cx))
                ranked.sort()
                report(f"{len(ranked)} of {len(spots)} sites beside a layer change", "stitching vias: ")
                mine = pour_vias.setdefault(code, [])
                for _, cy, cx in ranked:
                    x, y = self.grid.nm_x(cx), self.grid.nm_y(cy)
                    if _near(mine, x, y):
                        continue
                    rp = self._stitch_via(code, cx, cy)
                    if rp is not None:
                        stitch_paths.append(rp)
                        mine.extend(rp.vias)
                        if on_path:
                            on_path(rp)

        # 6. pour-net pads that got no via: a short track to the nearest
        # copper of their net (a via, another pad, a stub) instead
        tied_paths: List[RoutedPath] = []
        if not halted():
            for code, cell in wanted:
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
                else:
                    if (code, cell) in self.restore:
                        # a pad whose via a rip-up took: the route that needed
                        # the room may sit on its exit now, so the via search
                        # may also leave from the pad copper itself
                        starts = set()
                        for exit_cell in [cell] + self.p.fanout_alt.get(cell, []):
                            layer = exit_cell // self.grid.plane
                            _, _, px, py, _ = self.p.nets[code].endpoints[exit_cell]
                            pcx, pcy = self.grid.cell_x(px), self.grid.cell_y(py)
                            if self.grid.inside(pcx, pcy):
                                starts.add(self.grid.index(layer, pcx, pcy))
                        rp = self._fanout_search(code, cell, user_cancel, starts=starts) if starts else None
                        if _DEBUG:
                            print(f"[restore] net {self.p.nets[code].name} exit {self.grid.unpack(cell)} from pad copper "
                                  f"{[self.grid.unpack(s) for s in starts]} goals {len(self.p.fanout_goals.get(cell, ()))} "
                                  f"-> {'via' if rp else 'none'}", flush=True)
                        if rp is not None:
                            tied_paths.append(rp)
                            fanned.add(cell)
                            if on_path:
                                on_path(rp)
        fanout_paths += tied_paths

        result.cancelled = halted()
        result.failed = [c for c in failed if self._find(c.net, c.a) != self._find(c.net, c.b)]
        # the same connection may be queued twice (a pair member that failed twice)
        seen: Set[int] = set()
        result.failed = [c for c in result.failed if not (id(c) in seen or seen.add(id(c)))]
        result.failed_detail = [self._why_failed(c) for c in result.failed]
        if _DEBUG:
            for d in result.failed_detail:
                print(f"[failed] {d}", flush=True)
        # a board piece shove ripped up whose connection did not come back:
        # the piece returns as it was when its room is still free (the board
        # must not end with less than it had)
        restored = []
        for path in self.ripped_existing:
            code = path.conn.net
            if path.pid in self.paths or all(self._find(code, a) == self._find(code, b) for a, b in path.joins if a != b):
                continue
            if not self._fits(path):
                continue
            self.paths[path.pid] = path
            self._claim(path)
            for a, b in path.joins:
                if a != b:
                    self.parent[code][self._find(code, a)] = self._find(code, b)
            restored.append(path.path)
            self.shoved -= 1
            if on_path:
                on_path(path.path)
        if restored:
            result.failed = [c for c in result.failed if self._find(c.net, c.a) != self._find(c.net, c.b)]
            result.failed_detail = [self._why_failed(c) for c in result.failed]
        new_paths = [self.paths[pid] for pid in sorted(self.paths) if not self.paths[pid].existing]
        revived = {path.pid: path.path for path in self.revived if self.paths.get(path.pid) is path}
        result.paths = [path.path for path in new_paths] + fanout_paths + stitch_paths + restored + list(revived.values())
        result.paired_pids = {pid for pid, path in self.paths.items() if path.paired}
        result.stitched = len(stitch_paths)
        result.routed = len(new_paths)
        result.shoved = self.shoved
        result.fanout = len(fanout_paths)
        result.pairs = getattr(self, "pairs_routed", 0)
        result.pair_swaps = getattr(self, "pair_swaps", 0)
        result.pair_vias = getattr(self, "pair_vias", 0)
        result.pair_loose = list(getattr(self, "pair_loose", []))
        result.pair_exceptions = list(getattr(self, "pair_exceptions", []))
        result.direct_links = getattr(self, "direct_links", 0)
        result.pair_log = getattr(self, "pair_log", [])
        result.seconds = time.time() - started
        result.budget_cuts = self.budget_cuts
        return result


def run(problem: RoutingProblem, progress: Optional[ProgressFn] = None,
        on_path: Optional[PathFn] = None, cancelled: Optional[Callable[[], bool]] = None,
        on_remove: Optional[RemoveFn] = None) -> RouteResult:
    return Router(problem).run(progress=progress, on_path=on_path, on_remove=on_remove, cancelled=cancelled)
