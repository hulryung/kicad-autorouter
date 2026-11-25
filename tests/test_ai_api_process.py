"""Fake API transport in a real child: bounded request, cancellation and failures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.ai import llm
from autorouter.engine import isolated


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'openai.py').write_text('''import json,os,time
from pathlib import Path
class AuthenticationError(Exception): pass
class APIStatusError(Exception): pass
class APIConnectionError(Exception): pass
class OpenAI:
    def __init__(self, **kwargs):
        Path('sdk-options.json').write_text(json.dumps(kwargs))
        self.responses=self
    def create(self, **kwargs):
        Path('ready').touch()
        mode=os.environ['FAKE_API_MODE']
        if mode == 'failure': raise APIConnectionError('offline')
        if mode != 'success': time.sleep(30)
        return type('Response', (), {'output_text': '{"actions":[],"done":true}'})()
''')
        original = subprocess.Popen
        for mode in ('success', 'failure', 'cancel', 'timeout', 'callback'):
            captured = []
            work = root / mode
            def spawn(cmd, **kwargs):
                proc = original(cmd, **kwargs)
                captured.append(proc)
                return proc
            def poll():
                if (work / 'ready').exists():
                    raise RuntimeError('UI stopped')
            with patch.dict(os.environ, PYTHONPATH=str(root), FAKE_API_MODE=mode), \
                    patch.object(llm, 'status', return_value=(True, 'test SDK')), \
                    patch.object(isolated.subprocess, 'Popen', side_effect=spawn):
                try:
                    reply = llm.run(llm.Provider(llm.OPENAI_API), 'route', work,
                                    timeout=2.0 if mode == 'timeout' else 10,
                                    cancelled=(lambda: (work / 'ready').exists()) if mode == 'cancel' else None,
                                    poll=poll if mode == 'callback' else None)
                    assert mode == 'success' and reply['done']
                except llm.LLMError:
                    assert mode != 'success'
            assert captured[0].poll() is not None, 'API request still running'
            opts = json.loads((work / 'sdk-options.json').read_text())
            assert opts['max_retries'] == 0 and 0 < opts['timeout'] <= 60
    print('test_ai_api_process: successful reply; failure, cancel, timeout, UI failure reaped API worker')


if __name__ == '__main__':
    main()
