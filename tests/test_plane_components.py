"""A local group of connected power pads can still be isolated from its net."""
import sys
import unittest
from collections import namedtuple
from pathlib import Path

try:
    import pcbnew
except ImportError:
    print("test_plane_components: skipped (KiCad Python required)")
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from autorouter.engine.native.extract import _main_pad_components

Point = namedtuple("Point", "x y")


class Pad:
    def __init__(self, code, x, y=0):
        self.code, self.pos = code, Point(x, y)

    def GetNetCode(self):
        return self.code

    def GetPosition(self):
        return self.pos

    def GetClass(self):
        return "PAD"


class Board:
    def __init__(self, groups, reverse=False):
        self.pads = [pad for group in groups for pad in group]
        if reverse:
            self.pads.reverse()
        self.connected = {pad: group for group in groups for pad in group}

    def GetPads(self):
        return self.pads

    def GetConnectedItems(self, pad):
        return self.connected[pad]


class PlaneComponentTests(unittest.TestCase):
    def test_multiple_pads_do_not_make_an_island_part_of_the_main_net(self):
        main = [Pad(1, x) for x in (0, 1, 2)]
        island = [Pad(1, x) for x in (10, 11)]
        board = Board([island, main])
        self.assertEqual(_main_pad_components(board, board)[1], {(0, 0), (1, 0), (2, 0)})

    def test_equal_islands_choose_the_same_component_regardless_of_pad_order(self):
        groups = [[Pad(1, x) for x in (10, 11)], [Pad(1, x) for x in (0, 1)]]
        for reverse in (False, True):
            board = Board(groups, reverse)
            self.assertEqual(_main_pad_components(board, board)[1], {(0, 0), (1, 0)})

    def test_connected_net_and_overlapping_positions_of_different_nets(self):
        board = Board([[Pad(1, x) for x in (0, 1, 2)], [Pad(2, 0), Pad(2, 5)], [Pad(0, 99)]])
        self.assertEqual(_main_pad_components(board, board),
                         {1: {(0, 0), (1, 0), (2, 0)}, 2: {(0, 0), (5, 0)}})


if __name__ == "__main__":
    unittest.main()
