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

from . import astar
from .grid import BLOCKED, Grid
from .problem import Connection, NetInfo, RoutedPath, RoutingProblem

ProgressFn = Callable[[int, int, int, int, str], None]    # done, total, routed, failed, net name
PathFn = Callable[[RoutedPath], None]
RemoveFn = Callable[[int], None]                            # path id
RIP_PENALTY = 400                                           # per cell, in cost units (40 straight cells)
MAX_BLOCKERS = 6


class RouteResult:
    def __init__(self):
        self.routed = 0
        self.failed: List[Connection] = []
        self.paths: List[RoutedPath] = []
        self.ripped = 0
        self.seconds = 0.0
        self.cancelled = False


class _Path:
    """A routed connection and everything needed to undo it."""
    def __init__(self, pid: int, conn: Connection, path: RoutedPath, cells: List[int]):
        self.pid, self.conn, self.path, self.cells = pid, conn, path, cells
        self.claims: Dict[Tuple[int, int], List[int]] = {}      # owner-map key -> claimed cells
        self.via_cells: List[int] = []


def _smooth(grid: Grid, cells: List[int], passable) -> List[int]:
    """Straighten staircases. A run of cells that only uses two neighbouring
    directions (say east and north-east) is a zig-zag the grid search left
    behind; a hand-routed trace would make it one straight piece and one
    diagonal piece. Each such run is replaced by the two-piece version whose
    cells are all passable (tried with either piece first), so clearances
    still hold. Layer changes are never touched."""
    if len(cells) < 4:
        return cells
    plane, w = grid.plane, grid.w
    out = [cells[0]]
    i, n = 0, len(cells)
    while i < n - 1:
        layer = cells[i] // plane
        dirs = []
        j = i
        while j + 1 < n and cells[j + 1] // plane == layer:
            d = cells[j + 1] - cells[j]
            if d not in dirs:
                if len(dirs) == 2:
                    break
                dirs.append(d)
            j += 1
        if len(dirs) == 2 and j - i >= 3:
            counts = {d: 0 for d in dirs}
            for k in range(i, j):
                counts[cells[k + 1] - cells[k]] += 1
            replaced = None
            for first, second in ((dirs[0], dirs[1]), (dirs[1], dirs[0])):
                seq = [first] * counts[first] + [second] * counts[second]
                cur, new_cells, ok = cells[i], [], True
                for d in seq:
                    nxt = cur + d
                    if abs(nxt % w - cur % w) > 1 or not (0 <= nxt < grid.size) or not passable(nxt):
                        ok = False
                        break
                    # no corner cutting on diagonal pieces
                    dx = (nxt % w) - (cur % w)
                    dy = ((nxt % plane) // w) - ((cur % plane) // w)
                    if dx and dy and not (passable(cur + dx) and passable(cur + dy * w)):
                        ok = False
                        break
                    new_cells.append(nxt)
                    cur = nxt
                if ok and cur == cells[j]:
                    replaced = new_cells
                    break
            if replaced is not None:
                out.extend(replaced)
                i = j
                continue
        out.append(cells[i + 1])
        i += 1
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
    return out


class Router:
    def __init__(self, problem: RoutingProblem):
        self.p = problem
        self.grid = problem.grid
        self.opts = problem.options
        self.horizontal = [l.horizontal for l in problem.layers]
        # fixed copper as read from the board; the live maps start as copies
        self.fixed = {key: array("i", own) for key, own in problem.owners.items()}
        self.fixed_via_block = bytearray(problem.via_block)
        # cell -> path id, per owner map, for cells whose owner is routed copper
        self.routed = {key: array("i", [0]) * self.grid.size for key in problem.owners}
        self.paths: Dict[int, _Path] = {}
        self.next_pid = 1
        self.rips: Dict[Tuple[int, int, int], int] = {}        # (net, a, b) -> times ripped
        # union-find over each net's initial clusters, joined by surviving paths
        self.parent: Dict[int, List[int]] = {code: list(range(len(n.clusters)))
                                             for code, n in problem.nets.items()}

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
                self.parent[code][self._find(code, path.conn.a)] = self._find(code, path.conn.b)

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
                    owners[idx] = BLOCKED
                    if routed[idx] == 0:
                        routed[idx] = path.pid
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
        return astar.route(self.grid, owners, self.p.via_block, net.code, sources, goals, b_anchors,
                           self.horizontal, via_cost, self.opts.allow_vias, via_reach, cap,
                           self.opts.turn_cost, cancelled, **kwargs)

    def _rippable(self, pid: int, for_conn: Connection) -> bool:
        path = self.paths.get(pid)
        if path is None:
            return False
        if path.conn.net == for_conn.net:
            return True                       # own net's copper is never in the way
        key = (path.conn.net, path.conn.a, path.conn.b)
        return self.rips.get(key, 0) < self.opts.rip_limit

    def _commit(self, conn: Connection, cells: List[int], on_path) -> _Path:
        net = self.p.nets[conn.net]
        owners = self.p.owners[self._key(conn)]
        own_cells = set(cells)
        cells = _smooth(self.grid, cells, lambda idx: idx in own_cells or owners[idx] in (0, net.code))
        segments, vias = _simplify(self.grid, cells)
        segments = _with_pad_entries(self.grid, net, cells, segments)
        rp = RoutedPath(net.code, conn.width or net.rules.width, segments, vias, net.rules.via_diameter,
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
            conns.append(path.conn)
            key = (path.conn.net, path.conn.a, path.conn.b)
            self.rips[key] = self.rips.get(key, 0) + 1
            if on_remove:
                on_remove(pid)
        for code in {c.net for c in conns}:
            self._rebuild_union(code)
        self._rebuild_maps()
        return conns

    def _try(self, conn: Connection, via_cost: int, cap: int, on_path, cancelled) -> bool:
        if self._find(conn.net, conn.a) == self._find(conn.net, conn.b):
            return True                        # already joined through other paths
        cells = self._search(conn, via_cost, cap, False, cancelled)
        if not cells:
            return False
        self._commit(conn, cells, on_path)
        return True

    # ---- the whole run --------------------------------------------------

    def run(self, progress: Optional[ProgressFn] = None, on_path: Optional[PathFn] = None,
            on_remove: Optional[RemoveFn] = None, cancelled: Optional[Callable[[], bool]] = None) -> RouteResult:
        opts = self.opts
        result = RouteResult()
        started = time.time()
        # fine-pitch escapes first (they have the least room), then shortest first
        queue = sorted(self.p.connections, key=lambda c: (c.priority, c.estimate))
        total = len(queue)
        failed: List[Connection] = []
        done = 0

        def report(name):
            if progress:
                progress(done, total, len(self.paths), len(failed), name)

        def stop():
            return bool(cancelled and cancelled())

        # 1. plain pass
        for conn in queue:
            if stop():
                break
            if not self._try(conn, opts.via_cost, opts.max_expansions, on_path, cancelled):
                failed.append(conn)
            done += 1
            report(self.p.nets[conn.net].name)

        # 2. rip-up rounds
        rounds = opts.rip_rounds if opts.rip_up else 0
        for _ in range(rounds):
            if not failed or stop():
                break
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
                routed = self.routed[self._key(conn)]
                blockers = sorted({routed[c] for c in cells
                                   if routed[c] and self.paths.get(routed[c]) and
                                   self.paths[routed[c]].conn.net != conn.net})
                if len(blockers) > MAX_BLOCKERS:
                    still.append(conn)
                    continue
                ripped = self._rip(blockers, on_remove)
                result.ripped += len(ripped)
                if self._try(conn, opts.via_cost, opts.max_expansions * 2, on_path, cancelled):
                    progressed = True
                else:
                    still.append(conn)
                for other in ripped:
                    if not self._try(other, opts.via_cost, opts.max_expansions, on_path, cancelled):
                        still.append(other)
                report(self.p.nets[conn.net].name)
            failed = still
            if not progressed:
                break

        # 3. last chance with a bigger budget and cheaper vias
        if failed and opts.retry_failed and not stop():
            still = []
            for conn in failed:
                if not self._try(conn, max(5, opts.via_cost // 3), opts.max_expansions * 3, on_path, cancelled):
                    still.append(conn)
                report(self.p.nets[conn.net].name)
            failed = still

        result.cancelled = stop()
        result.failed = [c for c in failed if self._find(c.net, c.a) != self._find(c.net, c.b)]
        result.paths = [self.paths[pid].path for pid in sorted(self.paths)]
        result.routed = len(result.paths)
        result.seconds = time.time() - started
        return result


def run(problem: RoutingProblem, progress: Optional[ProgressFn] = None,
        on_path: Optional[PathFn] = None, cancelled: Optional[Callable[[], bool]] = None,
        on_remove: Optional[RemoveFn] = None) -> RouteResult:
    return Router(problem).run(progress=progress, on_path=on_path, on_remove=on_remove, cancelled=cancelled)
