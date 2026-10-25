"""Reserved pin via sites remain available even during a soft rip-up search."""
import sys
import unittest
from array import array
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.native import astar
from autorouter.engine.native.grid import Grid


def corridor(net=1, reservation=2, copper=0, soft=False, guarded=True):
    grid = Grid(0, 0, 100_000, 12, 3, 1)
    owners, guard = grid.new_owner_map(), array('i', [0]) * grid.size
    routed = array('i', [0]) * grid.size
    for y in range(grid.h):
        cell = grid.index(0, 5, y)
        guard[cell], owners[cell], routed[cell] = reservation, copper, 7
    kw = {'site_guard': guard} if guarded else {}
    if soft:
        kw.update(routed=routed, rippable=lambda pid: True, rip_penalty=10)
    return astar.route(grid, owners, bytearray(grid.plane), net,
                       {grid.index(0, 1, 1)}, {grid.index(0, 10, 1)}, [(10, 1)], [True],
                       10, False, 0, 1000, **kw)


class PinSiteGuardTests(unittest.TestCase):
    def test_other_net_cannot_take_reserved_exit(self):
        self.assertIsNone(corridor())
        self.assertIsNotNone(corridor(net=2))

    def test_soft_rip_does_not_override_reservation(self):
        self.assertIsNotNone(corridor(copper=2, soft=True, guarded=False))
        self.assertIsNone(corridor(copper=2, soft=True))

    def test_shared_site_and_existing_own_copper_remain_passable(self):
        self.assertIsNotNone(corridor(reservation=-1))
        self.assertIsNotNone(corridor(copper=1))


if __name__ == '__main__':
    unittest.main()
