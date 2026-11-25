"""Geometry/SI regressions cannot be hidden by a higher reviewer score."""
import copy
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.ai import quality


def main():
    old = {'available': True, 'checks': {'length': {'nets': ['A'], 'available': True, 'error': 0}}}
    changed = copy.deepcopy(old)
    changed['checks']['length']['error'] = 1
    assert quality.regression(old, changed, {})
    assert not quality.regression(old, changed, {'A': 1})
    assert quality.regression(old, {'available': False}, {})
    assert quality.regression(old, {'available': True, 'checks': {}}, {})
    try:
        import pcbnew
    except ImportError:
        print('test_ai_quality: gates passed; board measurement skipped (requires KiCad Python)')
        return
    board = pcbnew.CreateEmptyBoard() if hasattr(pcbnew, 'CreateEmptyBoard') else pcbnew.BOARD()
    tracks = []
    ns = board.GetDesignSettings().m_NetSettings
    ns.GetDefaultNetclass().SetDiffPairGap(250000)
    for name, y in (('USB_D+', 10000000), ('USB_D-', 10450000)):
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        track = pcbnew.PCB_TRACK(board)
        track.SetNetCode(net.GetNetCode())
        track.SetLayer(pcbnew.F_Cu)
        track.SetWidth(200000)
        track.SetStart(pcbnew.VECTOR2I(10000000, y))
        track.SetEnd(pcbnew.VECTOR2I(40000000, y))
        board.Add(track)
        tracks.append(track)
    before = quality.measure(board)
    assert before['checks'], before
    assert all(c['available'] and c['error'] == 0 for c in before['checks'].values()), before
    tracks[1].Move(pcbnew.VECTOR2I(0, 2000000))
    uncoupled = quality.measure(board)
    assert 'uncoupled' in quality.regression(before, uncoupled, {}), uncoupled
    tracks[1].Move(pcbnew.VECTOR2I(0, -2000000))
    tracks[1].SetEnd(pcbnew.VECTOR2I(38000000, 10450000))
    skewed = quality.measure(board)
    assert 'length' in quality.regression(before, skewed, {}), skewed
    # Arc sampling follows the semicircle rather than its chord.
    arc = pcbnew.PCB_ARC(board)
    arc.SetStart(pcbnew.VECTOR2I(0, 0))
    arc.SetMid(pcbnew.VECTOR2I(1000000, 1000000))
    arc.SetEnd(pcbnew.VECTOR2I(2000000, 0))
    segments = quality._segments(arc)
    assert max(max(s[1], s[3]) for s in segments) > 990000
    assert abs(sum(__import__('math').hypot(s[2]-s[0], s[3]-s[1]) for s in segments)-arc.GetLength()) < 5000
    print('test_ai_quality: pair decoupling and skew rejected; arc measurement follows curve')


if __name__ == '__main__':
    main()
