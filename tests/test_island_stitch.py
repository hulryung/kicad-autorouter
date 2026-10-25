"""An isolated filled polygon needs a legal via to a connected pour layer."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import pcbnew
except ImportError:
    print("test_island_stitch: skipped (KiCad Python required)")
    sys.exit(0)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from autorouter.engine.native.islands import stitch_candidates
from autorouter.engine.native.grid import Grid, BLOCKED
from autorouter.engine.native.problem import LayerInfo, NativeOptions, NetInfo, NetRules, RoutingProblem
from autorouter.engine.native.router import Router


def rectangle(x0, y0, x1, y1):
    poly = pcbnew.SHAPE_POLY_SET()
    poly.NewOutline()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        poly.Append(x, y)
    return poly


def fixture(attached=False, overlap=True):
    grid = Grid(0, 0, 100_000, 80, 80, 2)
    rules = NetRules(100_000, 100_000, 400_000, 200_000)
    nets = {1: NetInfo(1, "GND", rules, route=False)}
    pad = SimpleNamespace(GetNetCode=lambda: 1, GetPosition=lambda: pcbnew.VECTOR2I(500_000, 500_000),
                          GetClass=lambda: "PAD", GetLayer=lambda: 1, GetLayerSet=lambda: SimpleNamespace(CuStack=lambda: [1]))
    via = SimpleNamespace(GetPosition=lambda: pcbnew.VECTOR2I(3_000_000, 3_000_000),
                          GetClass=lambda: "PCB_VIA", GetLayerSet=lambda: SimpleNamespace(CuStack=lambda: [0, 1]))
    fills = {0: rectangle(2_000_000, 2_000_000, 4_000_000, 4_000_000),
             1: rectangle(0, 0, 6_000_000 if overlap else 1_000_000, 6_000_000)}
    zone = SimpleNamespace(GetNetCode=lambda: 1, GetIsRuleArea=lambda: False,
                           GetLayerSet=lambda: SimpleNamespace(CuStack=lambda: [0, 1]),
                           GetFilledPolysList=lambda lid: fills[lid])
    conn = SimpleNamespace(GetConnectedItems=lambda p: [pad, via] if attached else [pad])
    board = SimpleNamespace(GetConnectivity=lambda: conn, GetPads=lambda: [pad], Zones=lambda: [zone])
    return grid, nets, stitch_candidates(board, grid, nets, {1: {(500_000, 500_000, 1)}})


class IslandStitchTests(unittest.TestCase):
    def test_island_has_a_site_over_connected_copper(self):
        grid, nets, groups = fixture()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], 1)
        self.assertTrue(groups[0][1])
        self.assertTrue(all(23 <= x <= 37 and 23 <= y <= 37 for x, y in groups[0][1]))

    def test_no_bridge_when_other_layer_does_not_overlap(self):
        self.assertEqual(fixture(overlap=False)[2], [])

    def test_already_stitched_island_is_left_alone(self):
        self.assertEqual(fixture(attached=True)[2], [])

    def test_router_rejects_blocked_site_and_adds_only_one_via(self):
        grid, nets, groups = fixture()
        rules = nets[1].rules
        owners = grid.new_owner_map()
        owners[grid.index(1, 10, 10)] = BLOCKED
        opts = NativeOptions(fanout_vias=False, stitch_vias=False, repair_islands_only=True, pours_only=True)
        problem = RoutingProblem(grid, [LayerInfo(0, 0, "F.Cu", False), LayerInfo(1, 2, "In1.Cu", True)],
                                 {rules.key: owners}, bytearray(grid.plane), nets, [], opts,
                                 island_stitch=[(1, [(10, 10), (30, 30), (40, 40)])])
        result = Router(problem).run()
        self.assertEqual(result.stitched, 1)
        self.assertEqual([v for path in result.paths for v in path.vias], [(3_000_000, 3_000_000)])


if __name__ == "__main__":
    unittest.main()
