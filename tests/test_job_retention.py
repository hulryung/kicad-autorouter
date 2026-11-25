"""An old job in another editor process survives retention; crashes release it."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
from autorouter.engine import session


def main():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, KICAD_AUTOROUTER_HOME=tmp):
        script = ('import sys,time\n'
                  f'sys.path.insert(0, {str(ROOT / "plugins")!r})\n'
                  'from autorouter.engine import session\n'
                  'session.time.strftime=lambda *_: "20000101-000000"\n'
                  'job=session.new_job("long-running")\n'
                  'print(job.directory,flush=True)\n'
                  'sys.stdin.read()\n')
        child = subprocess.Popen([sys.executable, '-c', script], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        try:
            active = Path(child.stdout.readline().strip())
            assert active.is_dir()
            (active / 'candidate.pcb').write_text('retain')
            for i in range(15):
                job = session.new_job(f'completed-{i}')
                job.close()
            assert (active / 'candidate.pcb').read_text() == 'retain'
            child.terminate()
            child.wait(timeout=5)
            session._prune(active.parent)
            assert not active.exists(), 'crashed process left an unprunable lease'
            unmarked = active.parent / '19990101-legacy'
            unmarked.mkdir()
            session._prune(active.parent)
            assert unmarked.is_dir(), 'unknown legacy job was deleted'
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()
    print('test_job_retention: cross-process active lease protected; crash reclaimed; legacy preserved')


if __name__ == '__main__':
    main()
