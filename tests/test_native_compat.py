"""KiCad 9 component traversal agrees with KiCad 10, including split fills."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_native_compat: skipped (needs KiCad Python)')
    sys.exit(0)
from autorouter.engine.compat import _legacy_connected_items, connected_items
from test_wired_pieces import new_board, add_pad, add_track
from test_pour_joined import make_board as pour_board
from test_repair_islands import make_board as island_board


def ids(items):
    return {str(item.m_Uuid.AsString()) for item in items}


def main():
    board, nets = new_board(['SIG'])
    fp = pcbnew.FOOTPRINT(board)
    board.Add(fp)
    for number, x in enumerate((5, 15, 25), 1):
        add_pad(fp, number, x, 10, nets['SIG'])
    add_track(board, nets['SIG'], (5, 10), (10, 10))
    add_track(board, nets['SIG'], (10, 10), (15, 10))
    board.BuildConnectivity()
    pads = sorted(board.GetPads(), key=lambda p: p.GetNumber())
    component = _legacy_connected_items(board.GetConnectivity(), pads[0])
    assert ids(pads[:2]).issubset(ids(component)) and not ids(pads[2:]) & ids(component)
    # Exercise after an edit as well: no persistent cache can hide removed copper.
    board.Remove(next(iter(board.GetTracks())))
    board.BuildConnectivity()
    assert not ids(pads[1:]) & ids(_legacy_connected_items(board.GetConnectivity(), pads[0]))
    source = pcbnew.LoadBoard(str(Path(__file__).parent / 'fixtures/obd2-adapter.kicad_pcb'))
    project = int(source.GetProject())
    active_project = int(pcbnew.GetSettingsManager().Prj())
    boards = [pour_board(True)[0], island_board()]
    assert int(source.GetProject()) == project, 'refill replaced the source project'
    assert int(pcbnew.GetSettingsManager().Prj()) == active_project, 'refill changed the active project'
    for board in boards:
        if isinstance(board, tuple):
            board = board[0]
        board.BuildConnectivity()
        for pad in board.GetPads():
            actual = _legacy_connected_items(board.GetConnectivity(), pad)
            if pcbnew.Version().startswith('10.'):
                expected = board.GetConnectivity().GetConnectedItems(pad)
                assert ids(actual) == ids(expected), (pad.GetNumber(), ids(actual) ^ ids(expected))
            else:
                assert ids(actual) == ids(connected_items(board.GetConnectivity(), pad))
    print('test_native_compat: track chains, edits and filled islands passed')


if __name__ == '__main__':
    main()
