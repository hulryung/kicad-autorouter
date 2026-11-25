"""Run work that owns project/API state outside the PCB editor process."""
import os
from pathlib import Path
import subprocess
import sys
import time


def python_executable():
    candidates = [os.environ.get('KICAD_AUTOROUTER_PYTHON'),
                  str(Path(sys.prefix) / 'bin' / 'python3'),
                  str(Path(sys.prefix) / 'python.exe'),
                  str(Path(sys.prefix) / 'bin' / 'python.exe')]
    if Path(sys.executable).name.lower().startswith('python'):
        candidates.append(sys.executable)
    binary = next((p for p in candidates if p and Path(p).is_file()), None)
    if binary is None:
        raise RuntimeError('KiCad Python not found; set KICAD_AUTOROUTER_PYTHON to its interpreter')
    return binary


def run(cmd, workdir, timeout, cancelled=None, poll=None):
    """Bounded child lifetime; logs go to files so output cannot block pipes."""
    if cancelled and cancelled():
        raise RuntimeError('Cancelled.')
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    with (workdir / 'worker-stdout.log').open('w', encoding='utf-8') as stdout, \
            (workdir / 'worker-stderr.log').open('w', encoding='utf-8') as stderr:
        kwargs = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {}
        proc = subprocess.Popen(cmd, cwd=str(workdir), stdout=stdout, stderr=stderr, **kwargs)
        started = time.monotonic()
        try:
            while proc.poll() is None:
                if cancelled and cancelled():
                    raise RuntimeError('Cancelled.')
                if time.monotonic() - started >= timeout:
                    raise TimeoutError(f'Worker did not finish within {timeout:g}s.')
                if poll:
                    poll()
                try:
                    proc.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            return proc.returncode
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
