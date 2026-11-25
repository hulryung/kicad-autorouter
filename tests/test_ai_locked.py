"""AI edits must not change locked copper/footprints, even with force."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))


def main():
    try:
        import pcbnew
    except ImportError:
        print('test_ai_locked: skipped (requires KiCad Python)')
        return
    from autorouter.engine.ai.actions import BoardActions
    from autorouter.engine.ai.board_copy import track_signature, protected_state
    board = pcbnew.LoadBoard(str(Path(__file__).parent / 'fixtures/obd2-adapter.kicad_pcb'))
    routed = []
    editor = BoardActions(board, {}, lambda spec: routed.append(spec) or {}, lambda: 0, lambda line: None)
    acts = [dict(type='add_track', net='/CANH', layer='F.Cu', width_mm=0.3,
                 points_mm=[[150, 100], [152, 100]]),
            dict(type='add_via', net='/CANH', x_mm=151, y_mm=100)]
    assert all(r.ok for r in editor.apply(acts))
    for item in board.GetTracks():
        item.SetLocked(True)
    fp = next(iter(board.GetFootprints()))
    fp.SetLocked(True)
    original = protected_state(board)
    near = dict(x_mm=151, y_mm=100, radius_mm=2)
    edits = [dict(type='remove_tracks', nets=['/CANH']),
             dict(type='remove_items_near', **near),
             dict(type='move_items_near', dx_mm=1, **near),
             dict(type='set_track_width', net='/CANH', width_mm=0.8),
             dict(type='set_track_width', width_mm=0.8, **near),
             dict(type='set_layer_near', to_layer='B.Cu', **near),
             dict(type='set_via_near', diameter_mm=1, **near),
             dict(type='move_footprint', ref=fp.GetReference(), x_mm=0, y_mm=0)]
    for edit in edits:
        result = editor.apply([{**edit, 'force': True}])[0]
        assert not result.ok and 'locked' in result.detail, result
        assert protected_state(board) == original
    editor.options['remove_existing'] = True
    assert not editor.apply([{'type': 'route', 'scope': 'board'}])[0].ok
    assert not routed
    assert editor.apply([dict(type='inspect_near', **near)])[0].ok
    for item in board.GetTracks():
        item.SetLocked(False)
    assert editor.apply([dict(type='remove_tracks', nets=['/CANH'])])[0].ok
    assert not list(board.GetTracks())
    print('test_ai_locked: direct edits and remove-and-route respect locks; inspect and unlocked edits work')


if __name__ == '__main__':
    main()
