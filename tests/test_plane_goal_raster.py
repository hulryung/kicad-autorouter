"""Rasterized landing margins agree with KiCad polygon containment."""
import sys
import unittest
from pathlib import Path
try:
    import pcbnew
except ImportError:
    print('test_plane_goal_raster: skipped (KiCad Python required)')
    sys.exit(0)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.native.extract import _filled_goal_cells
from autorouter.engine.native.grid import Grid


def polygon(aligned=False):
    poly = pcbnew.SHAPE_POLY_SET()
    shift = 0 if aligned else 13_000
    poly.NewOutline()
    for x, y in [(100_000, 100_000), (2_800_000, 100_000), (2_800_000, 2_800_000),
                 (2_000_000, 2_800_000), (2_000_000, 2_000_000), (100_000, 2_000_000)]:
        poly.Append(x + shift, y + shift)
    hole = poly.NewHole(0)
    for x, y in [(600_000, 600_000), (1_100_000, 600_000), (1_100_000, 1_100_000), (600_000, 1_100_000)]:
        poly.Append(x + shift, y + shift, 0, hole)
    poly.NewOutline()
    for x, y in [(3_200_000, 200_000), (3_800_000, 200_000), (3_800_000, 1_800_000), (3_200_000, 1_800_000)]:
        poly.Append(x + shift, y + shift)
    return poly


class PlaneGoalRasterTests(unittest.TestCase):
    def test_concave_fill_hole_and_separate_island(self):
        grid = Grid(0, 0, 100_000, 40, 30, 2)
        inset = 150_000
        poly = polygon()
        expected = {cy * grid.w + cx for cy in range(grid.h) for cx in range(grid.w)
                    if all(poly.Contains(pcbnew.VECTOR2I(grid.nm_x(cx) + dx, grid.nm_y(cy) + dy), 0)
                           for dx, dy in [(0, 0), (inset, 0), (-inset, 0), (0, inset), (0, -inset)])}
        self.assertTrue(expected)
        self.assertEqual(_filled_goal_cells(grid, poly, [0], inset), expected)
        self.assertNotIn(10 * grid.w + 35, expected)

    def test_diagonal_edge_is_conservative(self):
        grid = Grid(0, 0, 100_000, 40, 30, 2)
        poly = pcbnew.SHAPE_POLY_SET()
        poly.NewOutline()
        for x, y in [(200_000, 200_000), (3_200_000, 200_000), (200_000, 2_700_000)]:
            poly.Append(x, y)
        inset = 150_000
        goals = _filled_goal_cells(grid, poly, [0], inset)
        self.assertTrue(goals)
        for cell in goals:
            y, x = divmod(cell, grid.w)
            self.assertTrue(all(poly.Contains(pcbnew.VECTOR2I(grid.nm_x(x) + dx, grid.nm_y(y) + dy), 0)
                                for dx, dy in [(0, 0), (inset, 0), (-inset, 0), (0, inset), (0, -inset)]))

    def test_aligned_edges_do_not_admit_points_outside_native_fill(self):
        grid = Grid(0, 0, 100_000, 40, 30, 2)
        inset = 100_000
        poly = polygon(aligned=True)
        goals = _filled_goal_cells(grid, poly, [0], inset)
        for cell in goals:
            y, x = divmod(cell, grid.w)
            self.assertTrue(all(poly.Contains(pcbnew.VECTOR2I(grid.nm_x(x) + dx, grid.nm_y(y) + dy), 0)
                                for dx, dy in [(0, 0), (inset, 0), (-inset, 0), (0, inset), (0, -inset)]))


if __name__ == '__main__':
    unittest.main()
