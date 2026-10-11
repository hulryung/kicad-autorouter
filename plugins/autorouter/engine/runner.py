"""Running the engine process in the background, streaming its log."""
import os
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional


class EngineRun:
    """One Freerouting process. Lines arrive on a worker thread through
    on_line; on_done gets the exit code (None when cancelled)."""

    def __init__(self, command: List[str], on_line: Callable[[str], None],
                 on_done: Callable[[Optional[int]], None], cwd: Optional[str] = None):
        self.command = command
        self.on_line = on_line
        self.on_done = on_done
        self.cwd = cwd
        self.process: Optional[subprocess.Popen] = None
        self.cancelled = False
        self.returncode: Optional[int] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        env = dict(os.environ)
        env.setdefault("JAVA_TOOL_OPTIONS", "-Dfile.encoding=UTF-8")
        self.process = subprocess.Popen(
            self.command, cwd=self.cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        self._thread = threading.Thread(target=self._pump, name="autorouter-engine", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            assert self.process and self.process.stdout
            for line in self.process.stdout:
                self.on_line(line.rstrip("\r\n"))
            self.returncode = self.process.wait()
        except Exception as e:                 # the UI must still be told
            self.on_line(f"[plugin] engine output stopped: {e!r}")
            self.returncode = self.process.poll() if self.process else -1
        finally:
            self.on_done(None if self.cancelled else self.returncode)

    def cancel(self, grace: float = 3.0) -> None:
        """Stop the engine: terminate, then kill if it lingers."""
        self.cancelled = True
        proc = self.process
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            deadline = time.time() + grace
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None
