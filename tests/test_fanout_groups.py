"""One successful bridge is enough for pads already on the same island."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.native.grid import Grid
from autorouter.engine.native.problem import Cluster, LayerInfo, NativeOptions, NetInfo, NetRules, RoutingProblem
from autorouter.engine.native.router import Router


def run(joined):
    grid = Grid(0, 0, 100_000, 60, 40, 2)
    rules = NetRules(100_000, 100_000, 400_000, 200_000)
    source1, source2, goal = grid.index(0, 10, 10), grid.index(0, 10, 30), grid.index(1, 40, 20)
    net = NetInfo(1, 'power', rules, route=False)
    net.clusters = ([Cluster(cells={source1, source2})] if joined else
                    [Cluster(cells={source1}), Cluster(cells={source2})])
    opts = NativeOptions(stitch_vias=False, pours_only=True)
    problem = RoutingProblem(grid, [LayerInfo(0, 0, 'F.Cu', False), LayerInfo(1, 2, 'In2.Cu', True)],
                             {rules.key: grid.new_owner_map()}, bytearray(grid.plane), {1: net}, [], opts,
                             fanout=[(1, source1), (1, source2)], fanout_strict={source1, source2},
                             fanout_goals={source1: {goal}, source2: {goal}})
    return Router(problem).run()


class FanoutGroupTests(unittest.TestCase):
    def test_connected_pads_need_one_bridge(self):
        self.assertEqual(run(True).fanout, 1)

    def test_separate_islands_keep_separate_bridges(self):
        self.assertEqual(run(False).fanout, 2)


if __name__ == '__main__':
    unittest.main()
