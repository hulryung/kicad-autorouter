"""Fanout must reach its plane with the via, not with an omitted inner track."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from autorouter.engine.native import router
from autorouter.engine.native.grid import Grid, BLOCKED
from autorouter.engine.native.problem import (Cluster, LayerInfo, NativeOptions,
                                              NetInfo, NetRules, RoutingProblem)


def fanout(blocked=False):
    grid = Grid(0, 0, 100_000, 50, 40, 2)
    rules = NetRules(100_000, 100_000, 500_000, 300_000)
    problem = RoutingProblem(
        grid, [LayerInfo(0, 0, "F.Cu", False), LayerInfo(1, 2, "In2.Cu", True)],
        {rules.key: grid.new_owner_map()}, bytearray(grid.plane), {}, [], NativeOptions())
    source, goal = grid.index(0, 10, 20), grid.index(1, 35, 20)
    net = NetInfo(1, "plane", rules, route=False)
    net.endpoints[source] = (1_000_000, 2_000_000, 1_000_000, 2_000_000, rules.width)
    net.clusters = [Cluster(cells={source}, anchors=[(10, 20)])]
    problem.nets[1] = net
    problem.fanout_goals[source] = {goal}
    if blocked:
        for y in range(grid.h):
            problem.owners[rules.key][grid.index(0, 22, y)] = BLOCKED
    return router.Router(problem)._fanout_search(1, source, None)


class PlaneLandingTests(unittest.TestCase):
    def test_via_lands_in_plane(self):
        path = fanout()
        self.assertIsNotNone(path)
        self.assertEqual(path.vias, [(3_500_000, 2_000_000)])
        self.assertTrue(path.segments)
        self.assertTrue(all(segment[0] == 0 for segment in path.segments))

    def test_does_not_claim_connection_through_an_omitted_track(self):
        # A source-layer wall forces the old search to change layers before
        # the target, then walk along the plane layer. That walk is never
        # emitted, so the resulting via cannot connect to the target fill.
        self.assertIsNone(fanout(blocked=True))


if __name__ == "__main__":
    unittest.main()
