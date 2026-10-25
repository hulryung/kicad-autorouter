"""Barrel-sized maps avoid inflating an already rasterized track map twice."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.native import astar
from autorouter.engine.native.grid import Grid, BLOCKED


def crossing(exact=True, inner_block=False, hole=False):
    grid = Grid(0, 0, 100_000, 9, 9, 3)
    owners, via_owners = grid.new_owner_map(), grid.new_owner_map()
    # The track map alone cannot describe the exact via clearance boundary.
    owners[grid.index(1, 5, 4)] = BLOCKED
    if inner_block:
        via_owners[grid.index(1, 4, 4)] = BLOCKED
    via_block = bytearray(grid.plane)
    if hole:
        via_block[4 * grid.w + 4] = 1
    args = dict(via_goals_only=True, routable=[True, False, True])
    if exact:
        args['via_owners'] = via_owners
    return astar.route(grid, owners, via_block, 1,
                       {grid.index(0, 4, 4)}, {grid.index(2, 4, 4)},
                       [(4, 4)], [True] * 3, 10, True, 1, 5000, **args)


class SignalViaMapTests(unittest.TestCase):
    def test_barrel_map_can_admit_a_site_rejected_by_second_inflation(self):
        self.assertIsNone(crossing(exact=False))
        self.assertIsNotNone(crossing())

    def test_unrouted_inner_layer_copper_blocks_through_via(self):
        self.assertIsNone(crossing(inner_block=True))

    def test_drill_clearance_still_blocks_exact_copper_site(self):
        self.assertIsNone(crossing(hole=True))


if __name__ == '__main__':
    unittest.main()
