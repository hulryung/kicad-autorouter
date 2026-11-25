"""Reject failed CLI replies and reap cancelled/timed-out processes."""
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.ai import llm


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        helper = root / 'helper.py'
        helper.write_text('import sys,time\nfrom pathlib import Path\n'
                          'sys.stdin.read()\n'
                          'print(\'{"structured_output":{"actions":[]},"is_error":false}\', flush=True)\n'
                          'print("fixture stderr", file=sys.stderr, flush=True)\n'
                          'Path("ready").touch()\n'
                          'if sys.argv[1] == "wait": time.sleep(30)\n'
                          'sys.exit(3)\n')
        original = subprocess.Popen
        for mode in ('failure', 'cancel', 'timeout', 'callback'):
            work = root / mode
            captured = []
            def spawn(cmd, **kwargs):
                proc = original([sys.executable, str(helper), 'fail' if mode == 'failure' else 'wait'], **kwargs)
                captured.append(proc)
                return proc
            def callback(line):
                raise RuntimeError('UI callback failed')
            with patch.object(llm, 'find_claude', return_value='fake'), patch.object(llm.subprocess, 'Popen', side_effect=spawn):
                try:
                    llm.run(llm.Provider(llm.CLAUDE_CLI), 'prompt', work,
                            timeout=0.3 if mode == 'timeout' else 10,
                            cancelled=(lambda: (work / 'ready').exists()) if mode == 'cancel' else None,
                            on_output=callback if mode == 'callback' else None)
                except (llm.LLMError, RuntimeError):
                    pass
                else:
                    raise AssertionError(f'{mode} unexpectedly returned actions')
            assert captured[0].poll() is not None
            assert captured[0].stdout.closed and captured[0].stderr.closed
            if mode != 'callback':
                assert 'fixture stderr' in (work / 'claude-stderr.log').read_text()
    print('test_ai_claude_process: failed replies rejected; processes reaped; logs retained')


if __name__ == '__main__':
    main()
