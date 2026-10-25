"""Plane search must approach the goal region rather than the source anchor."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.native import astar
from autorouter.engine.native.grid import Grid


class FanoutBoundsTests(unittest.TestCase):
    def test_remote_plane_is_reached_within_local_search_budget(self):
        grid = Grid(0, 0, 100_000, 400, 80, 2)
        source = grid.index(0, 10, 40)
        goal = grid.index(1, 350, 40)
        args = (grid, grid.new_owner_map(), bytearray(grid.plane), 1,
                {source}, {goal}, [(10, 40)], [True, True], 5, True, 1, 5000, 0)
        self.assertIsNone(astar.route(*args))
        before = astar.LAST['expansions']
        path = astar.route(*args, goal_bounds=(350, 40, 350, 40))
        self.assertIsNotNone(path)
        self.assertEqual((path[0], path[-1]), (source, goal))
        self.assertLess(astar.LAST['expansions'], before)
        print('plane search expansions', before, '->', astar.LAST['expansions'])


if __name__ == '__main__':
    unittest.main()
