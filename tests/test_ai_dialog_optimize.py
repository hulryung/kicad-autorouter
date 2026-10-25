"""Exercise the actual Score and improve handler with real routing/DRC.

Only the model replies are scripted. Requires a desktop session and opt-in.
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))


def main():
    if os.environ.get('KICAD_AUTOROUTER_GUI_TESTS') != '1':
        print('test_ai_dialog_optimize: skipped (requires GUI opt-in)')
        return
    import pcbnew
    import wx
    from autorouter.dialog import AutorouterDialog
    from autorouter.engine.ai import codex
    from autorouter.engine.options import RouteOptions
    fixture = Path(__file__).parent / 'fixtures' / 'obd2-adapter.kicad_pcb'
    app = wx.App(False)
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'KICAD_AUTOROUTER_HOME': tmp}):
        for cancel in (False, True):
            board = pcbnew.LoadBoard(str(fixture))
            project = int(board.GetProject())
            dialog = AutorouterDialog(None, board)
            dialog._load_options(RouteOptions())
            dialog.ai_rounds.SetValue(2)
            calls = []

            def answer(*args, **kwargs):
                calls.append(1)
                assert len(list(board.GetTracks())) == 0, 'candidate copper leaked into the open board'
                assert int(board.GetProject()) == project
                if cancel and len(calls) == 3:
                    dialog._cancel = True
                    raise codex.CodexError('Cancelled by test')
                acts = ([{'type': 'route', 'scope': 'board'}] if len(calls) == 1 else
                        [{'type': 'remove_tracks', 'nets': ['/CANH', '/CANL', '/TERM_H', '/VBAT_SW', 'GND']}])
                return {'score': [10, 80, 80, 99][len(calls) - 1], 'criteria': [], 'issues': [],
                        'summary': 'scripted model, real routing', 'actions': acts, 'done': False}

            with patch.object(codex, 'find_codex', return_value='fake'), patch.object(codex, 'run', side_effect=answer), \
                 patch.object(pcbnew, 'LoadBoard', side_effect=AssertionError('Do not replace the active project')):
                dialog._on_ai_score(None)
            assert not dialog.busy
            assert int(board.GetProject()) == project and board.GetFileName() == str(fixture)
            if cancel:
                assert not dialog.applied and len(list(board.GetTracks())) == 0
                assert 'stopped' in dialog.status.GetLabel().lower()
            else:
                assert len(calls) == 4
                assert dialog.applied and board.GetConnectivity().GetUnconnectedCount(True) == 0
                assert '10 -> 80' in dialog.status.GetLabel(), dialog.status.GetLabel()
            dialog.Destroy()
    app.Destroy()
    print('test_ai_dialog_optimize: best result applied; rejection, cancellation and active project preserved')


if __name__ == '__main__':
    main()
