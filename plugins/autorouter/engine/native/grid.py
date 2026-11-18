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


class SpanCells(list):
    """A cell list that also knows its row spans [a, b), for Grid.mark's slice path."""

    def __init__(self):
        super().__init__()
        self.spans: List[Tuple[int, int]] = []

    def add_span(self, a: int, b: int) -> None:
        self.spans.append((a, b))
        self.extend(range(a, b))

    def __add__(self, other):
        """Two span lists joined stay a span list (a stub's two pieces)."""
        other_spans = getattr(other, "spans", None)
        if other_spans is None:
            return list(self) + list(other)
        out = SpanCells()
        out.spans = self.spans + other_spans
        out.extend(self)
        out.extend(other)
        return out


_ZEROS = array("i")


_BLOCKED_ROW = memoryview(array("i"))


def _blocked_row(n: int) -> memoryview:
    global _BLOCKED_ROW
    if len(_BLOCKED_ROW) < n:
        _BLOCKED_ROW = memoryview(array("i", [BLOCKED]) * max(n, 8192))
    return _BLOCKED_ROW


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
            longest = max((b - a for a, b in spans), default=0)
            row = _blocked_row(longest)
            for a, b in spans:
                view[a:b] = row[:b - a]           # a view into one shared row: no copy per row
            return
        for idx in cells:
            owners[idx] = BLOCKED

    @staticmethod
    def mark(owners: array, cells: Iterable[int], owner: int) -> None:
        """Claim cells for owner; a cell two nets both need becomes BLOCKED."""
        spans = getattr(cells, "spans", None)
        if spans is not None:
            # row spans (the wide rings of isolation rules): a span still empty
            # or wholly this owner's is written in one slice
            for a, b in spans:
                n = b - a
                part = owners[a:b]
                first = part[0]
                if part.count(first) == n:            # one owner along the whole row
                    if first == 0:
                        owners[a:b] = array("i", [owner]) * n
                    elif first != owner and first != BLOCKED:
                        owners[a:b] = array("i", [BLOCKED]) * n
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
                    hit: Callable[[int, int], bool]) -> List[int]:
        """Cells of a layer inside a nm box whose centre satisfies hit(x_nm, y_nm)."""
        cx0, cy0, cx1, cy1 = self.cells_in_box(*box)
        base = layer * self.plane
        out = []
        for cy in range(cy0, cy1 + 1):
            y = self.oy + cy * self.pitch
            row = base + cy * self.w
            for cx in range(cx0, cx1 + 1):
                if hit(self.ox + cx * self.pitch, y):
                    out.append(row + cx)
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

        return self.cells_where(layer, (min(x1, x2) - r, min(y1, y2) - r,
                                        max(x1, x2) + r, max(y1, y2) + r), hit)

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
                    lo, hi = min(lo, ex - h), max(hi, ex + h)
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
                    a_lo, a_hi = max(a_lo, min(p, q)), min(a_hi, max(p, q))
                if a_lo <= a_hi:
                    lo, hi = min(lo, a_lo), max(hi, a_hi)
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
        """Cells inside a polygon (even-odd with holes), grown by r via its edges."""
        rings = [list(outline)] + [list(h) for h in holes]
        xs = [p[0] for ring in rings for p in ring]
        ys = [p[1] for ring in rings for p in ring]
        if not xs:
            return []
        cx0, cy0, cx1, cy1 = self.cells_in_box(min(xs), min(ys), max(xs), max(ys))
        edges = []
        for ring in rings:
            n = len(ring)
            for i in range(n):
                (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
                if ay != by:
                    edges.append((ax, ay, bx, by))
        inside = set()
        base = layer * self.plane
        for cy in range(cy0, cy1 + 1):
            y = self.oy + cy * self.pitch
            crossings = []
            for ax, ay, bx, by in edges:
                if (ay <= y) != (by <= y):
                    crossings.append(ax + (y - ay) * (bx - ax) / (by - ay))
            crossings.sort()
            row = base + cy * self.w
            for i in range(0, len(crossings) - 1, 2):
                a = max(cx0, int(math.ceil((crossings[i] - self.ox) / self.pitch)))
                b = min(cx1, int(math.floor((crossings[i + 1] - self.ox) / self.pitch)))
                if b >= a:
                    inside.update(range(row + a, row + b + 1))
        if r > 0:
            for ring in rings:
                n = len(ring)
                for i in range(n):
                    (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
                    inside.update(self.segment_cells(layer, ax, ay, bx, by, r))
        return list(inside)

    def outside_polygon_cells(self, layer: int, outline: Sequence[Tuple[int, int]],
                              holes: Sequence[Sequence[Tuple[int, int]]], r: int) -> List[int]:
        """Everything not inside the polygon, plus a band of r inside its edges."""
        inside = set(self.polygon_cells(layer, outline, holes, 0))
        base = layer * self.plane
        out = [idx for idx in range(base, base + self.plane) if idx not in inside]
        for ring in [list(outline)] + [list(h) for h in holes]:
            n = len(ring)
            for i in range(n):
                (ax, ay), (bx, by) = ring[i], ring[(i + 1) % n]
                out.extend(self.segment_cells(layer, ax, ay, bx, by, r))
        return out


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
