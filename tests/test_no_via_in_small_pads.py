"""No via is drilled into a small SMD pad (its own net's included); exposed pads keep their vias."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    pcbnew = None
from autorouter.engine.native.problem import NativeOptions


@unittest.skipIf(pcbnew is None, 'requires pcbnew')
class NoViaInSmallPadsTests(unittest.TestCase):
    def setUp(self):
        self.board = pcbnew.LoadBoard(str(Path(__file__).parent / 'fixtures/obd2-adapter.kicad_pcb'))
        smd = [p for p in self.board.GetPads() if p.GetAttribute() == pcbnew.PAD_ATTRIB_SMD and p.GetNetCode() > 0]
        self.small = min(smd, key=lambda p: min(p.GetSize().x, p.GetSize().y))
        big = [p for p in smd if min(p.GetSize().x, p.GetSize().y) >= 1_200_000]
        self.big = big[0] if big else None

    def centre(self, problem, pad):
        g = problem.grid
        return g.index(0, g.cell_x(pad.GetPosition().x), g.cell_y(pad.GetPosition().y)) % g.plane

    def test_small_pad_blocks_vias(self):
        from autorouter.engine.native import extract
        p = extract.extract(self.board, NativeOptions(fanout_vias=False, stitch_vias=False))
        self.assertEqual(p.via_block[self.centre(p, self.small)], 1)

    def test_option_allows_vias_in_small_pads(self):
        from autorouter.engine.native import extract
        p = extract.extract(self.board, NativeOptions(fanout_vias=False, stitch_vias=False, via_in_small_pads=True))
        self.assertEqual(p.via_block[self.centre(p, self.small)], 0)

    def test_exposed_pad_keeps_vias(self):
        if self.big is None:
            self.skipTest('fixture has no large SMD pad')
        from autorouter.engine.native import extract
        p = extract.extract(self.board, NativeOptions(fanout_vias=False, stitch_vias=False))
        self.assertEqual(p.via_block[self.centre(p, self.big)], 0)


if __name__ == '__main__':
    unittest.main()
