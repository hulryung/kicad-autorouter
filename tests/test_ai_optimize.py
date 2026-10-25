"""Score-only optimism, electrical regressions and failed candidates cannot win."""
import copy
import hashlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.ai import context, headless, optimize, review


def sample(score=50, opens=0, errors=(), length=100, vias=3):
    return {'score': score, 'drc': {'available': True, 'unconnected_total': opens,
            'violation_identities': list(errors)},
            'metrics': {'length_mm': length, 'vias': vias, 'angles': {'other': 0}}}


def offline():
    route = {'type': 'route', 'scope': 'board'}
    assert optimize.proposal_key('best', [route, {'type': 'note', 'text': 'new claim'}]) == optimize.proposal_key('best', [route])
    assert optimize.proposal_key('best', [route]) != optimize.proposal_key('better', [route])
    best = sample()
    assert optimize.decision(best, sample(score=90))[0] is False  # scoring noise
    assert optimize.decision(best, sample(score=90, opens=1, length=50))[0] is False
    assert optimize.decision(best, sample(score=90, errors=['clearance'], length=50))[0] is False
    assert optimize.decision(sample(errors=['old']), sample(score=90, errors=['new'], length=50))[0] is False
    assert optimize.decision(best, sample(score=51, length=98))[0] is True
    assert optimize.decision(sample(opens=2), sample(score=51, length=200))[0] is True
    assert optimize.decision(best, sample(score=49, length=50))[0] is False
    missing = sample(score=99, length=50)
    missing['drc']['available'] = False
    assert not optimize.decision(best, missing)[0]
    from autorouter.engine.native.problem import NativeOptions
    base = NativeOptions()
    original = copy.deepcopy(base)
    first = headless.native_options_from({'via_cost': 99}, base)
    first.skip_nets.append('GND')
    assert base == original, 'candidate options leaked into subsequent runs'
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        source = root / 'a.kicad_pcb'
        source.write_text('board')
        source.with_suffix('.kicad_dru').write_text('rules')
        dest = root / 'copy' / 'b.kicad_pcb'
        optimize.copy_project(source, dest)
        assert dest.read_text() == 'board'
        assert dest.with_suffix('.kicad_dru').read_text() == 'rules'
    print('test_ai_optimize: offline acceptance and isolation ok')


def with_kicad():
    try:
        import pcbnew
    except ImportError:
        print('test_ai_optimize: native integration skipped (requires KiCad Python)')
        return
    if not context.kicad_cli():
        print('test_ai_optimize: native integration skipped (requires kicad-cli)')
        return
    source = Path(__file__).parent / 'fixtures' / 'obd2-adapter.kicad_pcb'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        board = pcbnew.LoadBoard(str(source))
        report = context.drc_summary(board, root / 'nested' / 'verify')
        assert report['available'], report
        assert report['unconnected_total'] == 5, report
        # A stale successful report must not mask a later CLI failure.
        with patch.object(context.subprocess, 'run') as run:
            run.return_value.returncode = 2
            run.return_value.stderr = 'simulated failure'
            failed = context.drc_summary(board, root / 'nested' / 'verify')
        assert not failed['available'], failed

        class ScriptedOptimizer(optimize.Optimizer):
            calls = 0

            def _assess(self, board, folder, request, options):
                self.calls += 1
                drc = context.drc_summary(board, folder)
                assert drc['available'], drc
                acts = ([{'type': 'route', 'scope': 'board'}] if self.calls == 1 else
                        [{'type': 'remove_tracks', 'nets': ['/CANH', '/CANL', '/VBAT_SW', '/TERM_H', 'GND']}])
                return {'score': [10, 80, 80, 99][self.calls - 1], 'drc': drc,
                        'metrics': review.metrics(board), 'actions': acts, 'done': False}

        runner = ScriptedOptimizer(source, root / 'run', attempts=2, log=lambda s: None)
        result = runner.run()
        assert runner.calls == 4, 'the last edited candidate was not assessed'
        assert result['initial_score'] == 10 and result['best_score'] == 80, result
        assert [r['accepted'] for r in result['attempts']] == [True, False], result['attempts']
        assert result['best_drc']['unconnected_total'] == 0, result['best_drc']
        final = pcbnew.LoadBoard(result['best_board'])
        final_drc = context.drc_summary(final, root / 'final-check')
        assert final_drc['unconnected_total'] == 0, final_drc
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        assert Path(result['best_board']).parent.name == 'attempt-001'
        # Renaming the DRC copy must not silently drop the project's rules.
        best_path = Path(result['best_board'])
        best_path.with_suffix('.kicad_dru').write_text(
            '(version 1)\n(rule "wide tracks" (constraint track_width (min 1mm)))\n')
        constrained = context.drc_summary(final, root / 'rules-check')
        assert constrained['available'], constrained
        assert constrained['violations_by_type'].get('track_width', 0) > 0, constrained
    print('test_ai_optimize: final candidate assessed, destructive high-score candidate rejected, best board retained')


if __name__ == '__main__':
    offline()
    with_kicad()
