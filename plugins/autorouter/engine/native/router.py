"""Routing every connection of a RoutingProblem, in order, with retries.

Runs on a worker thread; it never touches pcbnew. Progress and each routed
path go out through callbacks (the dialog marshals them to the UI thread
and adds the copper to the board as it arrives).
"""
import math
import time
from typing import Callable, List, Optional, Tuple

from . import astar
from .grid import Grid
from .problem import Connection, NetInfo, RoutedPath, RoutingProblem

ProgressFn = Callable[[int, int, int, int, str], None]    # done, total, routed, failed, net name
PathFn = Callable[[RoutedPath], None]


class RouteResult:
    def __init__(self):
        self.routed = 0
        self.failed: List[Connection] = []
        self.paths: List[RoutedPath] = []
        self.seconds = 0.0
        self.cancelled = False


def _simplify(grid: Grid, cells: List[int]) -> Tuple[List[Tuple[int, int, int, int, int]], List[Tuple[int, int]]]:
    """Cell path -> straight segments per layer plus via positions."""
    points = [grid.unpack(i) for i in cells]          # (layer, cx, cy)
    segments, vias = [], []
    i = 0
    n = len(points)
    while i < n - 1:
        layer, x0, y0 = points[i]
        j = i + 1
        if points[j][0] != layer:                    # layer change: a via at this cell
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


def _claim_path(problem: RoutingProblem, net: NetInfo, path: RoutedPath) -> None:
    """Make the new copper an obstacle in every owner map."""
    grid = problem.grid
    for key, owners in problem.owners.items():
        width, clearance = key
        r_track = path.width // 2 + max(clearance, net.rules.clearance) + width // 2
        r_via = path.via_diameter // 2 + max(clearance, net.rules.clearance) + width // 2
        for layer, x1, y1, x2, y2 in path.segments:
            grid.mark(owners, grid.segment_cells(layer, x1, y1, x2, y2, r_track), net.code)
        for x, y in path.vias:
            for layer in range(grid.layers):
                grid.mark(owners, grid.disc_cells(layer, x, y, r_via), net.code)
    r_hole = path.via_drill // 2 + problem.hole_to_hole + problem.max_via_drill // 2
    for x, y in path.vias:
        for idx in grid.disc_cells(0, x, y, r_hole):
            problem.via_block[idx] = 1


def run(problem: RoutingProblem, progress: Optional[ProgressFn] = None,
        on_path: Optional[PathFn] = None, cancelled: Optional[Callable[[], bool]] = None) -> RouteResult:
    opts = problem.options
    grid = problem.grid
    result = RouteResult()
    started = time.time()
    horizontal = [layer.horizontal for layer in problem.layers]
    queue = sorted(problem.connections, key=lambda c: c.estimate)
    total = len(queue)
    attempts = [(queue, opts.via_cost, opts.max_expansions)]
    if opts.retry_failed:
        attempts.append((None, max(5, opts.via_cost // 3), opts.max_expansions * 3))
    done = 0

    for round_no, (conns, via_cost, cap) in enumerate(attempts):
        if conns is None:
            conns, result.failed = result.failed, []
            if not conns:
                break
        for conn in conns:
            if cancelled and cancelled():
                result.cancelled = True
                break
            net = problem.nets[conn.net]
            owners = problem.owners[net.rules.key]
            a, b = net.clusters[conn.a], net.clusters[conn.b]
            if a is b:                                   # merged by an earlier connection
                if round_no == 0:
                    done += 1
                continue
            if len(a.cells) > len(b.cells):              # search from the smaller side
                a, b = b, a
            via_reach = max(0, int(math.ceil((net.rules.via_diameter - net.rules.width) / 2 / grid.pitch)))
            cells = astar.route(grid, owners, problem.via_block, net.code, a.cells, b.cells, b.anchors,
                                horizontal, via_cost, opts.allow_vias, via_reach, cap, opts.turn_cost,
                                cancelled)
            if cells:
                segments, vias = _simplify(grid, cells)
                path = RoutedPath(net.code, net.rules.width, segments, vias,
                                  net.rules.via_diameter, net.rules.via_drill)
                _claim_path(problem, net, path)
                merged = a.cells | b.cells | set(cells)
                a.cells = merged
                a.anchors = a.anchors + b.anchors
                for i, c in enumerate(net.clusters):      # both names now point at one cluster
                    if c is b:
                        net.clusters[i] = a
                result.routed += 1
                result.paths.append(path)
                if on_path:
                    on_path(path)
            else:
                result.failed.append(conn)
            if round_no == 0:
                done += 1
            if progress:
                progress(done, total, result.routed, len(result.failed), net.name)
        if result.cancelled:
            break
    result.seconds = time.time() - started
    return result
