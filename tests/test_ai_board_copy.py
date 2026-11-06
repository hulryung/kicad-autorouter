"""Editor copies preserve the project, locked copper and group membership."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))


def main():
    try:
        import pcbnew
    except ImportError:
        print('test_ai_board_copy: skipped (needs KiCad Python)')
        return
    from autorouter.engine.ai import board_copy, context, headless
    from autorouter.engine.native import kicad_tuning
    source = Path(__file__).parent / 'fixtures' / 'obd2-adapter.kicad_pcb'
    board = pcbnew.LoadBoard(str(source))
    project = int(board.GetProject())
    filename = board.GetFileName()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        initial = board_copy.snapshot(board, root / 'input.kicad_pcb')
        candidate = board_copy.load_copy(initial, board)
        assert int(board.GetProject()) == project
        assert board.GetFileName() == filename
        assert headless.make_route_fn(candidate)({'scope': 'board'})['unconnected_after'] == 0
        assert len(list(board.GetTracks())) == 0, 'copy routing changed the editor board'
        tracks = list(candidate.GetTracks())
        group = pcbnew.PCB_GROUP(candidate)
        group.SetName('Routing test group')
        candidate.Add(group)
        group.AddItem(tracks[0])
        group.AddItem(list(candidate.GetFootprints())[0])
        # Exercise native tuning-pattern transfer as well as ordinary groups.
        track = next(t for t in tracks if t.GetParentGroup() is None)
        start, end = track.GetStart(), track.GetEnd()
        spec = {'base': (track.GetLayer(), start.x, start.y, end.x, end.y, track.GetWidth()),
                'net': track.GetNetname(), 'target': track.GetLength(), 'tolerance': 100000,
                'length': track.GetLength(), 'spacing': 200000, 'amin': 100000, 'amax': 1000000,
                'members': [board_copy.uid(track)]}
        assert kicad_tuning.attach(candidate, [spec]) == 1
        candidate_file = root / 'candidate.kicad_pcb'
        assert pcbnew.SaveBoard(str(candidate_file), candidate)
        expected = sorted(board_copy.track_signature(t) for t in candidate.GetTracks())
        assert board_copy.apply_copper(board, candidate_file) == len(tracks)
        assert sorted(board_copy.track_signature(t) for t in board.GetTracks()) == expected
        assert int(board.GetProject()) == project and board.GetFileName() == filename
        assert len(list(board.Generators())) == 1
        assert len(list(board.Groups())) == 1
        for item in board_copy._groups(board):
            group = pcbnew.Cast_to_PCB_GROUP(item)
            for member in group.GetItems():
                assert member.GetBoard() == board
                assert board_copy.uid(pcbnew.Cast_to_PCB_GROUP(pcbnew.Cast_to_BOARD_ITEM(member.GetParentGroup().AsEdaItem()))) == board_copy.uid(group)
        # Transfer again with pre-existing groups to catch dangling memberships.
        board_copy.apply_copper(board, candidate_file)
        assert board.GetConnectivity().GetUnconnectedCount(True) == 0
        assert not board.GroupsSanityCheck(), board.GroupsSanityCheck()
        drc = context.drc_summary(board, root / 'drc')
        assert drc['available'] and drc['unconnected_total'] == 0, drc
        assert drc['violations_by_type'] == {'invalid_outline': 1}, drc
        # A footprint move in an otherwise valid file must not be imported.
        unsafe = board_copy.load_copy(candidate_file, board)
        fp = list(unsafe.GetFootprints())[0]
        fp.Move(pcbnew.VECTOR2I(1000000, 0))
        unsafe_file = root / 'unsafe.kicad_pcb'
        pcbnew.SaveBoard(str(unsafe_file), unsafe)
        before = board_copy.protected_state(board)
        try:
            board_copy.apply_copper(board, unsafe_file)
        except ValueError:
            pass
        else:
            raise AssertionError('unsafe placement change was imported')
        assert board_copy.protected_state(board) == before
        assert sorted(board_copy.track_signature(t) for t in board.GetTracks()) == expected
        # Unsaved board minimums must participate in the candidate's DRC.
        board.GetDesignSettings().m_TrackMinWidth = 1000000
        strict = board_copy.snapshot(board, root / 'strict.kicad_pcb')
        strict_copy = board_copy.load_copy(strict, board)
        strict_drc = context.drc_summary(strict_copy, root / 'strict-drc')
        assert strict_drc['available'], strict_drc
        assert strict_drc['violations_by_type'].get('track_width', 0) > 0, strict_drc
    print('test_ai_board_copy: isolated routing, copper transfer, groups, tuning patterns and placement guard ok')


if __name__ == '__main__':
    main()
