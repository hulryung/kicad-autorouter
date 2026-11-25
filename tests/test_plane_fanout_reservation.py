"""Early plane vias and stubs retain their sites through later signal passes."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
try:
    import pcbnew
except ImportError:
    print('test_plane_fanout_reservation: skipped (requires KiCad Python)')
    raise SystemExit(0)
from test_fanout_stub_clearance import make_board
from autorouter.engine.native import extract, pipeline
from autorouter.engine.native.problem import NativeOptions

b, nets = make_board()
pipeline.run_job(b, NativeOptions(pours_only=True, stitch_vias=False), escalate=False)
plane_items = [t for t in b.GetTracks() if t.GetNetCode() == nets['+3V3'].GetNetCode()]
assert plane_items
ids = [t.m_Uuid.AsString() for t in plane_items]
for t in plane_items: t.SetLocked(True)
p = extract.extract(b, NativeOptions(fanout_vias=False, stitch_vias=False, movable_uuids=ids))
movable = {t.m_Uuid.AsString() for piece in p.existing for t in piece.items}
assert not movable.intersection(ids), 'locked plane copper became movable through job UUIDs'
print('test_plane_fanout_reservation: locked plane copper remains fixed')

for reserve in (False, True):
    b, nets = make_board()
    early = {}
    signal_reads = []
    def read(board, options, status):
        problem = extract.extract(board, options, status=status)
        if not options.pours_only:
            items = [t for t in board.GetTracks() if t.GetNetCode() == nets['+3V3'].GetNetCode()]
            assert items, 'early fanout did not place copper'
            if not early:
                early.update({t.m_Uuid.AsString(): (t.GetClass(), t.GetStart().x, t.GetStart().y,
                                                   t.GetEnd().x, t.GetEnd().y) for t in items})
            movable = {t.m_Uuid.AsString() for piece in problem.existing for t in piece.items}
            signal_reads.append(movable.intersection(early))
            if reserve:
                assert not movable.intersection(early), 'early plane outlet can be ripped up'
                assert set(early) <= set(options.locked_uuids)
        return problem
    pipeline.run_job(b, NativeOptions(preserve_plane_fanout=reserve, stitch_vias=False),
                     extract_fn=read, escalate=False)
    assert signal_reads
    if reserve:
        after = {t.m_Uuid.AsString(): (t.GetClass(), t.GetStart().x, t.GetStart().y,
                                      t.GetEnd().x, t.GetEnd().y) for t in b.GetTracks()}
        assert all(after.get(key) == value for key, value in early.items())
        assert not any(t.IsLocked() for t in b.GetTracks()), 'per-job reservation changed editor lock flags'
    else:
        assert any(signal_reads), 'legacy rip-up mode no longer exposes early copper'
    assert b.GetConnectivity().GetUnconnectedCount(True) == 0
print('test_plane_fanout_reservation: early sites preserved through the real signal pass; opt-out retained')
