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
        big = [p for p in smd if min(p.GetSize().x, p.GetSize().y) >= 1_500_000]
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

    def test_fine_pass_preserves_fabrication_constraint(self):
        from autorouter.engine.native import extract, pipeline
        for allowed in (False, True):
            with self.subTest(via_in_small_pads=allowed):
                options = NativeOptions(fanout_vias=False, stitch_vias=False,
                                        via_in_small_pads=allowed, via_in_pad_min_mm=1.6)
                fine = pipeline.fine_options(options, [self.small.GetNetname()], self.board)
                self.assertEqual(fine.via_in_small_pads, allowed)
                self.assertEqual(fine.via_in_pad_min_mm, 1.6)
                self.assertEqual(options.via_in_small_pads, allowed)
                self.assertEqual(fine.pitch_mm, max(0.025, options.pitch_mm / 2))
                problem = extract.extract(self.board, fine)
                self.assertEqual(problem.via_block[self.centre(problem, self.small)], int(not allowed))

    def test_exposed_pad_keeps_vias(self):
        if self.big is None:
            self.skipTest('fixture has no large SMD pad')
        from autorouter.engine.native import extract
        p = extract.extract(self.board, NativeOptions(fanout_vias=False, stitch_vias=False))
        self.assertEqual(p.via_block[self.centre(p, self.big)], 0)


if __name__ == '__main__':
    unittest.main()
