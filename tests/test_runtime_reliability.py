"""Persistence, job isolation and test selection regressions; no KiCad needed."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
from autorouter.settings import Settings
from autorouter.engine import session


def main():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KICAD_AUTOROUTER_HOME=tmp):
        path = Settings.path()
        for value in ([], None, 1, {'options': []}, {'options': 'broken'}, {'ai_model': []}):
            path.write_text(json.dumps(value))
            loaded = Settings.load()
            assert loaded.options.native_pitch_mm == 0.1
            assert isinstance(loaded.ai_model, str)
        settings = Settings(ai_model='retained')
        settings.save()
        original = path.read_bytes()
        settings.ai_model = 'new'
        with patch('autorouter.settings.os.replace', side_effect=OSError('disk failure')):
            try:
                settings.save()
            except OSError:
                pass
            else:
                raise AssertionError('save unexpectedly succeeded')
        assert path.read_bytes() == original
        assert not list(path.parent.glob('.settings-*'))
        settings.save()
        assert Settings.load().ai_model == 'new'
        with patch.object(session.time, 'strftime', return_value='20261125-000000'):
            first = session.new_job('same board').directory
            (first / 'evidence').write_text('keep')
            second = session.new_job('same board').directory
        assert first != second and second.is_dir()
        assert (first / 'evidence').read_text() == 'keep'
        spec = importlib.util.spec_from_file_location('test_runner', ROOT / 'scripts/run-tests.py')
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        with patch.object(runner, 'TESTS', Path(tmp) / 'empty'), patch.object(sys, 'argv', ['run-tests.py', 'typo']):
            assert runner.main() == 2
    print('test_runtime_reliability: settings recovery, atomic save, unique jobs, empty selection passed')


if __name__ == '__main__':
    main()
