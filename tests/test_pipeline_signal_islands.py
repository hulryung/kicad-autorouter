"""Signal routing gets final island repair even without any power fanout."""
import sys
import unittest
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    pcbnew = None
from autorouter.engine.native.problem import NativeOptions


@unittest.skipIf(pcbnew is None, 'requires pcbnew')
class SignalIslandPassTests(unittest.TestCase):
    def test_signal_only_changes_still_check_for_new_islands(self):
        from autorouter.engine.native import extract, pipeline
        board = pcbnew.LoadBoard(str(Path(__file__).parent / 'fixtures/obd2-adapter.kicad_pcb'))
        calls = []
        def traced(board, options, status):
            calls.append(options.repair_islands_only)
            return extract.extract(board, options, status)
        result = pipeline.run_job(board, NativeOptions(fanout_first=False), extract_fn=traced)
        self.assertGreater(result.routed, 0)
        self.assertEqual(result.fanout, 0)
        self.assertTrue(calls[-1])
        self.assertEqual(calls.count(True), 1)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "routed.kicad_pcb")
            pcbnew.SaveBoard(path, board)
            saved = pcbnew.LoadBoard(path)
            self.assertEqual(saved.GetConnectivity().GetUnconnectedCount(True), 0)


if __name__ == '__main__':
    unittest.main()
