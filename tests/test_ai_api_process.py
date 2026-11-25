"""Fake API transport in a real child: bounded request, cancellation and failures."""
from itertools import product
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
        (root / 'anthropic.py').write_text('''import json,os,time
from pathlib import Path
class BadRequestError(Exception): pass
class AuthenticationError(Exception): pass
class RateLimitError(Exception): pass
class APIStatusError(Exception): pass
class APIConnectionError(Exception): pass
class Anthropic:
    def __init__(self, **kwargs):
        Path('sdk-options.json').write_text(json.dumps(kwargs))
        self.beta=self.messages=self
    def stream(self, **kwargs): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get_final_message(self):
        Path('ready').touch()
        mode=os.environ['FAKE_API_MODE']
        if mode == 'failure': raise APIConnectionError('offline')
        if mode != 'success': time.sleep(30)
        text=type('Text', (), {'type':'text', 'text':'{"actions":[],"done":true}'})()
        return type('Message', (), {'stop_reason':'end_turn', 'content':[text]})()
''')
        original = subprocess.Popen
        for provider, mode in product((llm.OPENAI_API, llm.ANTHROPIC_API),
                                      ('success', 'failure', 'cancel', 'timeout', 'callback')):
            captured = []
            work = root / (provider + "-" + mode)
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
                    reply = llm.run(llm.Provider(provider), 'route', work,
                                    timeout=2.0 if mode == 'timeout' else 10,
                                    cancelled=(lambda: (work / 'ready').exists()) if mode == 'cancel' else None,
                                    poll=poll if mode == 'callback' else None)
                    assert mode == 'success' and reply['done']
                except llm.LLMError:
                    assert mode != 'success'
            assert captured[0].poll() is not None, 'API request still running'
            opts = json.loads((work / 'sdk-options.json').read_text())
            assert opts['max_retries'] == 0 and 0 < opts['timeout'] <= 60
    print('test_ai_api_process: OpenAI and Anthropic successful replies; failure, cancel, timeout, UI failure reaped API worker')


if __name__ == '__main__':
    main()
