"""Invalid saved input cannot reach the router; failed rule writes preserve files."""
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine import guides, si, fab
from autorouter.engine.options import RouteOptions


def main():
    options = RouteOptions.from_dict({'native_pitch_mm': 0, 'native_via_cost': -1,
                                     'native_time_limit_min': 'forever', 'native_allow_vias': 'false',
                                     'routable_layers': [1, None], 'native_track_mode': [],
                                     'to_dict': 4, 'native_stitch_pitch_mm': float('nan')})
    assert options == RouteOptions()
    assert RouteOptions.from_dict({'native_pitch_mm': 0.05, 'native_allow_vias': False}).native_pitch_mm == 0.05
    for invalid in ([], {'pair_skew_mm': float('nan')}, {'impedance': [{'name': 'bad', 'gap_mm': -1}]},
                    {'length_groups': [{'name': 'bad', 'nets': 'CLK'}]}, {'rounded': 'false'}):
        try:
            si.SiConfig.from_dict(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(invalid)
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KICAD_AUTOROUTER_HOME=tmp):
        folder = guides.user_dir()
        folder.mkdir()
        (folder / 'invalid.json').write_text('[]')
        (folder / 'bad-options.json').write_text(json.dumps({'options': {'pitch_mm': 0}}))
        assert all(g['id'] not in ('invalid', 'bad-options') for g in guides.load_all())
        for g in ({'id': '../escape'}, {'options': {'allow_vias': 'yes'}}, {'style': {'class_via_mm': {'PWR': [0.3, 0.6]}}}):
            try:
                guides.save(g)
            except ValueError:
                pass
            else:
                raise AssertionError(g)
        boardpath = Path(tmp) / 'board.kicad_pcb'
        board = type('Board', (), {'GetFileName': lambda self: str(boardpath)})()
        rules = boardpath.with_suffix('.kicad_dru')
        rules.write_text('(version 1)\n# user rules\n')
        intent = boardpath.with_suffix('.autorouter.json')
        intent.write_text('{"old": true}')
        guide = guides.save({'id': 'mine', 'name': 'Mine'})
        before = {p: p.read_bytes() for p in (rules, intent, guide)}
        writes = [lambda: si.save(board, si.SiConfig()),
                  lambda: si.write_drc_rules(board, '# new rules\n'),
                  lambda: fab.write_drc_rules(board, next(iter(fab.PRESETS)) if hasattr(fab, 'PRESETS') else 'JLCPCB 4-layer'),
                  lambda: guides.save({'id': 'mine', 'name': 'Changed'})]
        # The fab block is removed when no preset is selected; seed one to exercise the write.
        rules.write_text('(version 1)\n' + fab.BEGIN + '\nold\n' + fab.END + '\n')
        before[rules] = rules.read_bytes()
        writes[2] = lambda: fab.write_drc_rules(board, '')
        for write in writes:
            with patch('autorouter.engine.persistence.os.replace', side_effect=OSError('disk failure')):
                try:
                    write()
                except OSError:
                    pass
                else:
                    raise AssertionError('write unexpectedly succeeded')
            assert all(p.read_bytes() == data for p, data in before.items())
        assert not list(Path(tmp).rglob('.*-?*')), 'temporary files leaked'
    print('test_config_validation: malformed values rejected; failed sidecar writes preserve prior files')


if __name__ == '__main__':
    main()
