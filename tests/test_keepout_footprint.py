"""A via centre outside an avoid rectangle must still keep its copper out."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    pcbnew = None
from autorouter.engine.native.grid import BLOCKED
from autorouter.engine.native.problem import NativeOptions


@unittest.skipIf(pcbnew is None, 'requires pcbnew')
class KeepoutFootprintTests(unittest.TestCase):
    def test_barrel_margin_is_wider_than_track_margin(self):
        from autorouter.engine.native import extract
        source = Path(__file__).parent / 'fixtures/obd2-adapter.kicad_pcb'
        def read(keepouts):
            return extract.extract(pcbnew.LoadBoard(str(source)),
                                   NativeOptions(fanout_vias=False, stitch_vias=False, keepouts=keepouts))
        before = read([])
        after = read([(105_200_000, 154_500_000, 106_000_000, 155_500_000)])
        point = after.grid.index(0, after.grid.cell_x(105_000_000), after.grid.cell_y(155_000_000))
        far = after.grid.index(0, after.grid.cell_x(104_500_000), after.grid.cell_y(155_000_000))
        track, via = (200_000, 200_000), (600_000, 200_000)
        self.assertEqual(before.owners[via][point], 0)
        self.assertEqual(after.owners[track][point], 0)
        self.assertEqual(after.owners[via][point], BLOCKED)
        self.assertEqual(after.owners[via][far], 0)
        self.assertEqual(after.via_block[point % after.grid.plane], 0)


if __name__ == '__main__':
    unittest.main()
