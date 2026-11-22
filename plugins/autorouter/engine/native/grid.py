"""A multi-layer occupancy grid in board coordinates (nanometres).

Each routable layer has a flat array of cell owners:
    0   free
   -1   blocked for every net (board edge, keepouts, netless copper, or two
        different nets' clearance zones overlapping)
   >0   the net code whose copper (plus clearance) covers the cell

Obstacles are rasterized already inflated by the clearance and half the
routing width, so the router only ever asks "may net N put a track
centreline on this cell?". One grid is kept per distinct (width, clearance)
pair in use, because the inflation depends on the net being routed.
"""
import math
from array import array
from typing import Callable, Iterable, List, Sequence, Tuple

BLOCKED = -1
WIDE_NM = 1_000_000       # radii from this up are rasterized by row spans (isolation clearances)
# lane_guard value of a cell under an IC body on the part's own layer (inside
# its pin ring): passable, but a track there pays astar.BODY_PENALTY per cell
BODY_LANE = -2
# segment_cells: a cell centre this close to the end of a row's closed-form
# interval is put to the per-cell test (the closed form's float error is
# nanometres at worst; a cell farther in or out is decided by the margin)
_TIE_NM = 1_000


class SpanCells(list):
    """A cell list that also knows its row spans [a, b), for Grid.mark's slice path."""

    def __init__(self):
        super().__init__()
        self.spans: List[Tuple[int, int]] = []

    def add_span(self, a: int, b: int) -> None:
        self.spans.append((a, b))
        list.extend(self, range(a, b))

    # Changed in place, the list no longer matches its spans: it becomes a
    # plain cell list (spans None), which Grid.mark / block walk cell by cell.
    def _loose(self):
        self.spans = None

    def __iadd__(self, other):
        spans, other_spans = self.spans, getattr(other, "spans", None)
        list.extend(self, other)
        self.spans = spans + other_spans if spans is not None and other_spans is not None else None
        return self

    def extend(self, other):
        self._loose()
        list.extend(self, other)

    def append(self, x):
        self._loose()
        list.append(self, x)

    def insert(self, i, x):
        self._loose()
        list.insert(self, i, x)

    def __setitem__(self, i, x):
        self._loose()
        list.__setitem__(self, i, x)

    def __delitem__(self, i):
        self._loose()
        list.__delitem__(self, i)

    def pop(self, *a):
        self._loose()
        return list.pop(self, *a)

    def remove(self, x):
        self._loose()
        list.remove(self, x)

    def clear(self):
        list.clear(self)
        self.spans = []

    def __add__(self, other):
        """Two span lists joined stay a span list (a stub's two pieces)."""
        other_spans = getattr(other, "spans", None)
        if other_spans is None:
            return list(self) + list(other)
        if self.spans is None:
            return list(self) + list(other)
        out = SpanCells()
        out.spans = self.spans + other_spans
        list.extend(out, self)
        list.extend(out, other)
        return out


_ZEROS = array("i")


_BLOCKED_ROW = memoryview(array("i"))


def _blocked_row(n: int) -> memoryview:
    global _BLOCKED_ROW
    if len(_BLOCKED_ROW) < n:
        _BLOCKED_ROW = memoryview(array("i", [BLOCKED]) * max(n, 8192))
    return _BLOCKED_ROW


_OWNER_ROWS: dict = {}


def _owner_row(owner: int, n: int) -> memoryview:
    """A shared row of owner values, at least n long (slices of it fill spans
    without building an array per span)."""
    row = _OWNER_ROWS.get(owner)
    if row is None or len(row) < n:
        if len(_OWNER_ROWS) > 4096:
            _OWNER_ROWS.clear()
        row = _OWNER_ROWS[owner] = memoryview(array("i", [owner]) * max(n, 4096))
    return row


def _zeros(n: int) -> array:
    global _ZEROS
    if len(_ZEROS) < n:
        _ZEROS = array("i", [0]) * max(n, 4096)
    return _ZEROS[:n]


class Grid:
    def __init__(self, origin_x: int, origin_y: int, pitch: int, width: int, height: int, layers: int):
        self.ox, self.oy, self.pitch = origin_x, origin_y, pitch
        self.w, self.h, self.layers = width, height, layers
        self.plane = width * height
        self.size = self.plane * layers

    # ---- coordinates --------------------------------------------------------

    def cell_x(self, x_nm: int) -> int:
        return int(round((x_nm - self.ox) / self.pitch))

    def cell_y(self, y_nm: int) -> int:
        return int(round((y_nm - self.oy) / self.pitch))

    def nm_x(self, cx: int) -> int:
        return self.ox + cx * self.pitch

    def nm_y(self, cy: int) -> int:
        return self.oy + cy * self.pitch

    def index(self, layer: int, cx: int, cy: int) -> int:
        return layer * self.plane + cy * self.w + cx

    def unpack(self, idx: int) -> Tuple[int, int, int]:
        layer, rest = divmod(idx, self.plane)
        cy, cx = divmod(rest, self.w)
        return layer, cx, cy

    def inside(self, cx: int, cy: int) -> bool:
        return 0 <= cx < self.w and 0 <= cy < self.h

    def cells_in_box(self, x0: int, y0: int, x1: int, y1: int) -> Tuple[int, int, int, int]:
        """Cell index range (inclusive) covering a nm box, clipped to the grid."""
        cx0 = max(0, int(math.floor((x0 - self.ox) / self.pitch)))
        cy0 = max(0, int(math.floor((y0 - self.oy) / self.pitch)))
        cx1 = min(self.w - 1, int(math.ceil((x1 - self.ox) / self.pitch)))
        cy1 = min(self.h - 1, int(math.ceil((y1 - self.oy) / self.pitch)))
        return cx0, cy0, cx1, cy1

    # ---- owner maps ---------------------------------------------------------

    def new_owner_map(self) -> array:
        return array("i", [0]) * self.size

    @staticmethod
    def block(owners: array, cells: Iterable[int]) -> None:
        """Close cells to every net of a map: an isolation rule's ring around
        another side's copper, which no net of that map may enter (the copper's
        own net routes on other maps). Row spans are written as slices."""
        spans = getattr(cells, "spans", None)
        if spans is not None:
            view = memoryview(owners)
            row = _blocked_row(0)
            for a, b in spans:
                try:
                    view[a:b] = row[:b - a]       # a view into one shared row: no copy per row
                except ValueError:                # the shared row is shorter than this span
                    row = _blocked_row(b - a)
                    view[a:b] = row[:b - a]
            return
        for idx in cells:
            owners[idx] = BLOCKED

    @staticmethod
    def mark(owners: array, cells: Iterable[int], owner: int) -> None:
        """Claim cells for owner; a cell two nets both need becomes BLOCKED."""
        spans = getattr(cells, "spans", None)
        if spans is not None:
            # row spans: a span still empty or wholly one owner's is written
            # in one slice (of a shared row of the value: no copy per span)
            view = memoryview(owners)
            mine = _owner_row(owner, 0)
            blocked = _blocked_row(0)
            for a, b in spans:
                n = b - a
                part = owners[a:b]
                first = part[0]
                if part.count(first) == n:            # one owner along the whole row
                    if first == 0:
                        if len(mine) < n:
                            mine = _owner_row(owner, n)
                        view[a:b] = mine[:n]
                    elif first != owner and first != BLOCKED:
                        if len(blocked) < n:
                            blocked = _blocked_row(n)
                        view[a:b] = blocked[:n]
                    continue
                for idx in range(a, b):
                    cur = owners[idx]
                    if cur == 0 or cur == owner:
                        owners[idx] = owner
                    elif cur != BLOCKED:
                        owners[idx] = BLOCKED
            return
        for idx in cells:
            cur = owners[idx]
            if cur == 0 or cur == owner:
                owners[idx] = owner
            elif cur != BLOCKED:
                owners[idx] = BLOCKED

    # ---- rasterizers (cell indices on one layer) ----------------------------

    def cells_where(self, layer: int, box: Tuple[int, int, int, int],
                    hit: Callable[[int, int], bool], convex: bool = False) -> List[int]:
        """Cells of a layer inside a nm box whose centre satisfies hit(x_nm, y_nm),
        row by row (as row spans, so Grid.mark / block can write them by slices).

        convex: hit's cells form a convex shape (a round, rectangular, oval,
        trapezoid or chamfered pad grown by a clearance), so each row's hits
        are one run: its two ends are walked to from the row before's ends,
        a few hit() calls a row instead of one per cell. A row whose middle
        guess misses is scanned cell by cell, so a row with hits is never lost."""
        cx0, cy0, cx1, cy1 = self.cells_in_box(*box)
        base = layer * self.plane
        out = SpanCells()
        ox, p = self.ox, self.pitch
        if convex:
            lo_prev, hi_prev = -1, -1
            for cy in range(cy0, cy1 + 1):
                y = self.oy + cy * p
                row = base + cy * self.w
                if lo_prev < 0:
                    lo_prev = hi_prev = (cx0 + cx1) // 2
                m = (lo_prev + hi_prev) // 2
                if cx0 <= m <= cx1 and hit(ox + m * p, y):
                    a = min(lo_prev, m)
                    if hit(ox + a * p, y):
                        while a > cx0 and hit(ox + (a - 1) * p, y):
                            a -= 1
                    else:
                        while not hit(ox + (a + 1) * p, y):
                            a += 1
                        a += 1
                    b = max(hi_prev, m)
                    if hit(ox + b * p, y):
                        while b < cx1 and hit(ox + (b + 1) * p, y):
                            b += 1
                    else:
                        while not hit(ox + (b - 1) * p, y):
                            b -= 1
                        b -= 1
                    out.add_span(row + a, row + b + 1)
                    lo_prev, hi_prev = a, b
                    continue
                a = b = -1
                for cx in range(cx0, cx1 + 1):
                    if hit(ox + cx * p, y):
                        if a < 0:
                            a = cx
                        b = cx
                    elif a >= 0:
                        break
                if a >= 0:
                    out.add_span(row + a, row + b + 1)
                    lo_prev, hi_prev = a, b
                else:
                    lo_prev = hi_prev = -1
            return out
        for cy in range(cy0, cy1 + 1):
            y = self.oy + cy * p
            row = base + cy * self.w
            start = -1
            for cx in range(cx0, cx1 + 1):
                if hit(ox + cx * p, y):
                    if start < 0:
                        start = cx
                elif start >= 0:
                    out.add_span(row + start, row + cx)
                    start = -1
            if start >= 0:
                out.add_span(row + start, row + cx1 + 1)
        return out

    def rounded_box_cells(self, layer: int, box: Tuple[int, int, int, int], r: int) -> List[int]:
        """Cells within r of a nm box (row spans, no per-cell test): for the
        wide clearances of isolation rules, where an exact test of every cell
        of an 8 mm ring is too slow. Holds the box's corners, so for a round
        or chamfered shape it covers a little more than the exact ring."""
        x0, y0, x1, y1 = box
        cy0 = max(0, int(math.floor((y0 - r - self.oy) / self.pitch)))
        cy1 = min(self.h - 1, int(math.ceil((y1 + r - self.oy) / self.pitch)))
        base = layer * self.plane
        out = SpanCells()
        for cy in range(cy0, cy1 + 1):
            y = self.oy + cy * self.pitch
            dy = y0 - y if y < y0 else (y - y1 if y > y1 else 0)
            if dy > r:
                continue
            half = math.sqrt(r * r - dy * dy)
            cx0 = max(0, int(math.ceil((x0 - half - self.ox) / self.pitch)))
            cx1 = min(self.w - 1, int(math.floor((x1 + half - self.ox) / self.pitch)))
            if cx1 >= cx0:
                row = base + cy * self.w
                out.add_span(row + cx0, row + cx1 + 1)
        return out

    def disc_cells(self, layer: int, x: int, y: int, r: int) -> List[int]:
        """Cells whose centre is within r of (x, y), by row spans: the same
        cells as the per-cell test (integer square roots), without a Python
        call per cell (an 8 mm isolation disc is 25 000 cells)."""
        x, y, r = int(x), int(y), int(r)
        if r < 0:
            return SpanCells()
        p, ox, oy = self.pitch, self.ox, self.oy
        cy0 = max(0, -((oy - (y - r)) // p))           # ceil((y - r - oy) / p)
        cy1 = min(self.h - 1, (y + r - oy) // p)
        cx_lo, cx_hi = 0, self.w - 1
        base = layer * self.plane
        r2 = r * r
        out = SpanCells()
        for cy in range(cy0, cy1 + 1):
            dy = oy + cy * p - y
            d = r2 - dy * dy
            if d < 0:
                continue
            half = math.isqrt(d)
            cx0 = max(cx_lo, -((ox - (x - half)) // p))
            cx1 = min(cx_hi, (x + half - ox) // p)
            if cx1 >= cx0:
                row = base + cy * self.w
                out.add_span(row + cx0, row + cx1 + 1)
        return out

    def segment_cells(self, layer: int, x1: int, y1: int, x2: int, y2: int, r: int) -> List[int]:
        """Cells within r of the segment (a track of width 2r around its centreline)."""
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        r2 = r * r
        if r >= WIDE_NM:
            return self._capsule_rows(layer, x1, y1, x2, y2, r)

        def hit(px, py):
            if length2 == 0:
                t = 0.0
            else:
                t = ((px - x1) * dx + (py - y1) * dy) / length2
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
            ex, ey = px - (x1 + t * dx), py - (y1 + t * dy)
            return ex * ex + ey * ey <= r2

        # The cells hit() picks, without calling it for every cell of the box:
        # each row meets the capsule in one interval (it is convex), found in
        # closed form as in _capsule_rows. Only the cells next to the
        # interval's two ends are put to hit() itself; a cell a whole cell or
        # more inside (outside) the interval is inside (outside) by a margin
        # many orders above the float error of either computation. So the
        # result is the per-cell test's, cell for cell (tests/test_dru_rules.py).
        cx0, cy0, cx1, cy1 = self.cells_in_box(min(x1, x2) - r, min(y1, y2) - r,
                                               max(x1, x2) + r, max(y1, y2) + r)
        out = SpanCells()
        if cx1 < cx0:
            return out
        p, ox, oy = self.pitch, self.ox, self.oy
        base = layer * self.plane
        length = math.sqrt(length2)
        if length > 0:
            ux, uy = dx / length, dy / length
        for cy in range(cy0, cy1 + 1):
            y = oy + cy * p
            lo, hi = math.inf, -math.inf
            for ex, ey in ((x1, y1), (x2, y2)):
                d = abs(y - ey)
                if d <= r:
                    h = math.sqrt(r2 - d * d)
                    if ex - h < lo:
                        lo = ex - h
                    if ex + h > hi:
                        hi = ex + h
            if length > 0:
                a_lo, a_hi = -math.inf, math.inf
                for k, c, lim_lo, lim_hi in ((ux, (y - y1) * uy, 0.0, length),
                                             (-uy, (y - y1) * ux, -r, r)):
                    if abs(k) < 1e-12:
                        if not (lim_lo <= c <= lim_hi):
                            a_lo, a_hi = 1, 0
                        continue
                    q1, q2 = (lim_lo - c) / k + x1, (lim_hi - c) / k + x1
                    if q1 > q2:
                        q1, q2 = q2, q1
                    if q1 > a_lo:
                        a_lo = q1
                    if q2 < a_hi:
                        a_hi = q2
                if a_lo <= a_hi:
                    if a_lo < lo:
                        lo = a_lo
                    if a_hi > hi:
                        hi = a_hi
            if lo > hi:
                continue
            a = int(math.ceil((lo - ox) / p))
            b = int(math.floor((hi - ox) / p))
            s0, s1 = max(cx0, a - 1), min(cx1, b + 1)
            if s1 < s0:
                continue
            row = base + cy * self.w
            if b - a < 3:
                # a short row (a disc's top, a thin sliver): every cell tested
                start = -1
                for cx in range(s0, s1 + 1):
                    if hit(ox + cx * p, y):
                        if start < 0:
                            start = cx
                    elif start >= 0:
                        out.add_span(row + start, row + cx)
                        start = -1
                if start >= 0:
                    out.add_span(row + start, row + s1 + 1)
                continue
            # a cell centre within _TIE_NM of an end is a near tie: hit()
            # decides it; the others are decided by the margin
            xa = ox + a * p
            if lo - (xa - p) < _TIE_NM:
                if hit(xa - p, y):
                    a -= 1
            elif xa - lo < _TIE_NM and not hit(xa, y):
                a += 1
            xb = ox + b * p
            if (xb + p) - hi < _TIE_NM:
                if hit(xb + p, y):
                    b += 1
            elif hi - xb < _TIE_NM and not hit(xb, y):
                b -= 1
            a, b = max(a, cx0), min(b, cx1)
            if b >= a:
                out.add_span(row + a, row + b + 1)
        return out

    def _capsule_rows(self, layer: int, x1: int, y1: int, x2: int, y2: int, r: int) -> List[int]:
        """segment_cells by row spans: the capsule is convex, so each row
        meets it in one interval, the hull of the end discs' and the middle
        band's intervals."""
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        cy0 = max(0, int(math.floor((min(y1, y2) - r - self.oy) / self.pitch)))
        cy1 = min(self.h - 1, int(math.ceil((max(y1, y2) + r - self.oy) / self.pitch)))
        base = layer * self.plane
        out = SpanCells()
        for cy in range(cy0, cy1 + 1):
            y = self.oy + cy * self.pitch
            lo, hi = math.inf, -math.inf
            for ex, ey in ((x1, y1), (x2, y2)):
                d = abs(y - ey)
                if d <= r:
                    h = math.sqrt(r * r - d * d)
                    if ex - h < lo:
                        lo = ex - h
                    if ex + h > hi:
                        hi = ex + h
            if length > 0:
                ux, uy = dx / length, dy / length
                # band: 0 <= (p - p1).u <= length and |(p - p1) x u| <= r, both linear in x
                a_lo, a_hi = -math.inf, math.inf
                for k, c, lim_lo, lim_hi in ((ux, (y - y1) * uy, 0.0, length),
                                             (-uy, (y - y1) * ux, -r, r)):
                    # k * (x - x1) + c in [lim_lo, lim_hi]
                    if abs(k) < 1e-12:
                        if not (lim_lo <= c <= lim_hi):
                            a_lo, a_hi = 1, 0
                        continue
                    p, q = (lim_lo - c) / k + x1, (lim_hi - c) / k + x1
                    if q < p:
                        p, q = q, p
                    if p > a_lo:
                        a_lo = p
                    if q < a_hi:
                        a_hi = q
                if a_lo <= a_hi:
                    if a_lo < lo:
                        lo = a_lo
                    if a_hi > hi:
                        hi = a_hi
            if lo > hi:
                continue
            cx0 = max(0, int(math.ceil((lo - self.ox) / self.pitch)))
            cx1 = min(self.w - 1, int(math.floor((hi - self.ox) / self.pitch)))
            if cx1 >= cx0:
                row = base + cy * self.w
                out.add_span(row + cx0, row + cx1 + 1)
        return out

    def polygon_cells(self, layer: int, outline: Sequence[Tuple[int, int]],
                      holes: Sequence[Sequence[Tuple[int, int]]] = (), r: int = 0) -> List[int]:
        """Cells inside a polygon (even-odd with holes), grown by r via its edges
        (row spans, each cell once)."""
        rows = self._polygon_rows(layer, outline, holes, r)
        base = layer * self.plane
        out = SpanCells()
        for cy in sorted(rows):
            row = base + cy * self.w
            for a, b in _merged(rows[cy]):
                out.add_span(row + a, row + b + 1)
        return out

    def _polygon_rows(self, layer: int, outline, holes, r: int) -> dict:
        """cy -> [(cx0, cx1) inclusive runs] of polygon_cells, unmerged."""
        rings = [list(outline)] + [list(h) for h in holes]
        xs = [p[0] for ring in rings for p in ring]
        ys = [p[1] for ring in rings for p in ring]
        rows: dict = {}
        if not xs:
            return rows
        cx0, cy0, cx1, cy1 = self.cells_in_box(min(xs), min(ys), max(xs), max(ys))
        edges = []
        for ring in rings:
            n = len(ring)
            for i in range(n):
                (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
                if ay != by:
                    edges.append((ax, ay, bx, by))
        for cy in range(cy0, cy1 + 1):
            y = self.oy + cy * self.pitch
            crossings = []
            for ax, ay, bx, by in edges:
                if (ay <= y) != (by <= y):
                    crossings.append(ax + (y - ay) * (bx - ax) / (by - ay))
            crossings.sort()
            for i in range(0, len(crossings) - 1, 2):
                a = max(cx0, int(math.ceil((crossings[i] - self.ox) / self.pitch)))
                b = min(cx1, int(math.floor((crossings[i + 1] - self.ox) / self.pitch)))
                if b >= a:
                    rows.setdefault(cy, []).append((a, b))
        if r > 0:
            self._edge_rows(rows, layer, rings, r)
        return rows

    def _edge_rows(self, rows: dict, layer: int, rings, r: int) -> None:
        """Add the capsules of r around every ring edge to rows (cy -> runs)."""
        w, plane = self.w, self.plane
        for ring in rings:
            n = len(ring)
            for i in range(n):
                (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
                for a, b in self.segment_cells(layer, ax, ay, bx, by, r).spans:
                    cy, c0 = divmod(a % plane, w)
                    rows.setdefault(cy, []).append((c0, c0 + (b - a) - 1))

    def outside_polygon_cells(self, layer: int, outline: Sequence[Tuple[int, int]],
                              holes: Sequence[Sequence[Tuple[int, int]]], r: int) -> List[int]:
        """Everything not inside the polygon, plus a band of r inside its edges
        (row spans, each cell once)."""
        inside = self._polygon_rows(layer, outline, holes, 0)
        rings = [list(outline)] + [list(h) for h in holes]
        band: dict = {}
        self._edge_rows(band, layer, rings, r)
        base = layer * self.plane
        last = self.w - 1
        out = SpanCells()
        for cy in range(self.h):
            runs = []
            x = 0
            for a, b in _merged(inside.get(cy, ())):
                if a > x:
                    runs.append((x, a - 1))
                x = b + 1
            if x <= last:
                runs.append((x, last))
            runs.extend(band.get(cy, ()))
            row = base + cy * self.w
            for a, b in _merged(runs):
                out.add_span(row + a, row + b + 1)
        return out


def _merged(runs) -> List[Tuple[int, int]]:
    """Inclusive (a, b) runs of one row, sorted and joined where they touch or overlap."""
    out: List[List[int]] = []
    for a, b in sorted(runs):
        if out and a <= out[-1][1] + 1:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def jog(ax: int, ay: int, bx: int, by: int, at_end: bool = False):
    """Points of an octilinear run from A to B: [A, B] when the straight line
    is already at a multiple of 45 degrees, else [A, J, B] with one 45-degree
    jog that takes up the smaller offset at the start (or at the end when
    at_end is set) and an axis-parallel run for the rest."""
    dx, dy = bx - ax, by - ay
    if dx == 0 or dy == 0 or abs(dx) == abs(dy):
        return [(ax, ay), (bx, by)]
    sx = (dx > 0) - (dx < 0)
    sy = (dy > 0) - (dy < 0)
    m = min(abs(dx), abs(dy))
    if at_end:
        return [(ax, ay), (bx - sx * m, by - sy * m), (bx, by)]
    return [(ax, ay), (ax + sx * m, ay + sy * m), (bx, by)]
