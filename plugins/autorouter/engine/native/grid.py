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
# lane_guard value of a cell under an IC body on the part's own layer (inside
# its pin ring): passable, but a track there pays astar.BODY_PENALTY per cell
BODY_LANE = -2


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
    def mark(owners: array, cells: Iterable[int], owner: int) -> None:
        """Claim cells for owner; a cell two nets both need becomes BLOCKED."""
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

    def disc_cells(self, layer: int, x: int, y: int, r: int) -> List[int]:
        r2 = r * r
        return self.cells_where(layer, (x - r, y - r, x + r, y + r),
                                lambda px, py: (px - x) ** 2 + (py - y) ** 2 <= r2)

    def segment_cells(self, layer: int, x1: int, y1: int, x2: int, y2: int, r: int) -> List[int]:
        """Cells within r of the segment (a track of width 2r around its centreline)."""
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        r2 = r * r

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
