"""Cancellation/timeouts reap the CLI, and failed runs cannot supply a reply."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.ai import codex


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        helper = root / 'helper.py'
        helper.write_text('import sys,time\nfrom pathlib import Path\n'
                          'sys.stdin.read()\n'
                          'Path(sys.argv[2]).write_text(\'{"actions": [], "done": true}\')\n'
                          'print("fixture output", flush=True)\n'
                          'if sys.argv[1] == "wait": time.sleep(30)\n'
                          'sys.exit(3)\n')
        original = subprocess.Popen
        for mode in ('cancel', 'timeout', 'failure'):
            work = root / mode
            captured = []
            def spawn(cmd, **kwargs):
                proc = original([sys.executable, str(helper), 'fail' if mode == 'failure' else 'wait',
                                 str(work / 'codex-last-message.json')], **kwargs)
                captured.append(proc)
                return proc
            with patch.object(codex.subprocess, 'Popen', side_effect=spawn):
                try:
                    codex.run('ignored', 'test', work, timeout=0.3 if mode == 'timeout' else 10,
                              cancelled=(lambda: (work / 'codex-last-message.json').exists()) if mode == 'cancel' else None)
                except codex.CodexError:
                    pass
                else:
                    raise AssertionError(f'{mode} unexpectedly succeeded')
            assert captured[0].poll() is not None, f'{mode} left the CLI alive'
            assert captured[0].stdout.closed
            assert 'fixture output' in (work / 'codex-exec.log').read_text()
    print('test_ai_codex_process: cancelled/timed-out CLI reaped; failed exit rejected; logs retained')


if __name__ == '__main__':
    main()
