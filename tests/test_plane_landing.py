"""Fanout must reach its plane with the via, not with an omitted inner track."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from autorouter.engine.native import router
from autorouter.engine.native.grid import Grid, BLOCKED
from autorouter.engine.native.problem import (Cluster, LayerInfo, NativeOptions,
                                              NetInfo, NetRules, RoutingProblem)


def fanout(blocked=False, strict=True):
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
    if strict:
        problem.fanout_strict.add(source)
    if blocked:
        for y in range(grid.h):
            problem.owners[rules.key][grid.index(0, 22, y)] = BLOCKED
    return router.Router(problem)._fanout_search(1, source, None)


class PlaneLandingTests(unittest.TestCase):
    def assert_connected(self, path):
        self.assertIsNotNone(path)
        graph = {}
        def link(a, b):
            graph.setdefault(a, set()).add(b)
            graph.setdefault(b, set()).add(a)
        for layer, x1, y1, x2, y2, *width in path.segments:
            link((layer, x1, y1), (layer, x2, y2))
        for x, y in path.vias:
            link((0, x, y), (1, x, y))
        source, goal = (0, 1_000_000, 2_000_000), (1, 3_500_000, 2_000_000)
        seen, todo = {source}, [source]
        while todo:
            for node in graph.get(todo.pop(), ()):
                if node not in seen:
                    seen.add(node)
                    todo.append(node)
        self.assertIn(goal, seen, "the emitted copper must reach the actual plane goal")

    def test_ordinary_fanout_keeps_direct_plane_landing(self):
        self.assertIsNone(fanout(blocked=True, strict=False))

    def test_emitted_copper_reaches_plane(self):
        self.assert_connected(fanout())

    def test_other_layer_track_is_kept_when_source_layer_is_blocked(self):
        path = fanout(blocked=True)
        self.assert_connected(path)
        self.assertTrue(any(segment[0] == 1 for segment in path.segments))


if __name__ == "__main__":
    unittest.main()
