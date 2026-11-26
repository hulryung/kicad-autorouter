"""Stage timing, stall reports and a hard stop for long jobs.

A job's time limit counts search work (benchmarks: A* expansions), so board
work outside the searches (reading a routed board back with its isolation
rings, rip-up bookkeeping) can run on unseen for an hour. This module makes
that visible early and bounds it:

  * every status line starts a stage; the stages' times are kept, and the
    slow ones are reported in the job's stats (``slow_stages``);
  * a background thread logs the main thread's stack once a stage has run
    ``STALL_S`` seconds, and again at each doubling, so a stall says where
    it is while it is still going on;
  * ``check()`` raises ``Overrun`` past the job's hard deadline (the wall
    cap plus a grace for the pour passes): loops that may run long call it.

One watch is active at a time (``start``/``stop`` in run_job); the stack it
logs is that of the thread that started the current stage (the dialog reads
the board on its UI thread, routes on a worker). With none active every
function here is a cheap no-op.
"""
import logging
import sys
import threading
import time
import traceback
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

STALL_S = 120.0        # a stage this long gets its stack logged (then at 2x, 4x, ...)
SLOW_S = 60.0          # stages from this long are listed in the job's stats
POLL_S = 5.0


class Overrun(RuntimeError):
    """The job's hard deadline passed in the middle of a stage."""


class Watch:
    def __init__(self, status: Optional[Callable[[str], None]] = None, stall_s: float = STALL_S,
                 slow_s: float = SLOW_S, hard_deadline: float = 0.0):
        self.status = status
        self.stall_s = stall_s
        self.slow_s = slow_s
        self.hard_deadline = hard_deadline
        self._wall_origin = time.time()
        self._awake_origin = time.monotonic()
        self.thread_id = threading.get_ident()
        self.totals: Dict[str, float] = {}
        self.stalls: List[Tuple[str, float, str]] = []     # (stage, seconds, where)
        self.overrun = ""                                   # the stage the hard deadline cut, if any
        self._told = 0                                      # stalls already passed to status
        self._lock = threading.Lock()
        self._name = ""
        self._started = time.time()
        self._cpu_started = time.process_time()
        self._awake_started = time.monotonic()
        self._next_report = stall_s
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="autorouter-watchdog", daemon=True)
        self._thread.start()

    # ---- the job's thread ---------------------------------------------------

    def stage(self, name: str) -> None:
        now = time.time()
        with self._lock:
            self._close(now)
            self._name, self._started, self._next_report = name, now, self.stall_s
            self._cpu_started = time.process_time()
            self._awake_started = time.monotonic()
            self.thread_id = threading.get_ident()
            fresh = self.stalls[self._told:]
            self._told = len(self.stalls)
        if self.status:
            for stage, secs, where in fresh:
                self.status(f"Slow step: '{stage}' has taken {secs:.0f} s so far (in {where})")

    def wall_time(self) -> float:
        """Epoch-based job clock advanced only by monotonic awake time.

        This uses the same clock difference as our existing sleep diagnostic.
        It excludes suspend on macOS without extending caps for CPU starvation.
        """
        return self._wall_origin + max(0.0, time.monotonic() - self._awake_origin)

    def overdue(self) -> bool:
        if self.hard_deadline and self.wall_time() > self.hard_deadline:
            if not self.overrun:
                self.overrun = self._name or "?"
                logger.warning("hard deadline passed during '%s'", self.overrun)
            return True
        return False

    def check(self) -> None:
        if self.overdue():
            raise Overrun(f"during '{self._name}'")

    def close(self) -> None:
        with self._lock:
            self._close(time.time())
            self._name = ""
        self._done.set()

    def slow(self) -> List[Tuple[str, float]]:
        """Stages (by name) that took SLOW_S or more in all, slowest first."""
        return sorted(((n, round(s, 1)) for n, s in self.totals.items() if s >= self.slow_s),
                      key=lambda t: -t[1])

    def _close(self, now: float) -> None:
        if self._name:
            self.totals[self._name] = self.totals.get(self._name, 0.0) + (now - self._started)

    # ---- the watchdog thread -------------------------------------------------

    def _watch(self) -> None:
        while not self._done.wait(min(POLL_S, max(0.05, self.stall_s / 4))):
            with self._lock:
                name, started, due = self._name, self._started, self._next_report
                cpu0, awake0 = self._cpu_started, self._awake_started
                if not name or time.time() - started < due:
                    continue
                self._next_report = due * 2
            secs = time.time() - started
            frame = sys._current_frames().get(self.thread_id)
            stack = traceback.extract_stack(frame) if frame is not None else []
            ours = [f for f in stack if "autorouter" in f.filename] or stack
            where = " <- ".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}"
                                for f in reversed(ours[-4:]))
            # CPU time well below the wall time: the process was starved
            # (another job, efficiency cores) or paging, not computing; or
            # the machine slept (a closed lid on battery): the monotonic
            # clock (mach_absolute_time on macOS) stops while the wall clock runs on
            where += f"; CPU {time.process_time() - cpu0:.0f} s of {secs:.0f} s" + _peak_memory()
            asleep = secs - (time.monotonic() - awake0)
            if asleep > max(5.0, 0.05 * secs):
                where += f", system asleep {asleep:.0f} s"
            with self._lock:
                self.stalls.append((name, secs, where))
            logger.warning("slow step '%s': %.0f s so far\n%s", name, secs,
                           "".join(traceback.format_list(ours[-8:])))


def _peak_memory() -> str:
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak *= 1 if sys.platform == "darwin" else 1024            # bytes on macOS, KiB elsewhere
        return f", peak memory {peak / 1e9:.1f} GB"
    except Exception:
        return ""


_active: Optional[Watch] = None


def start(status=None, hard_deadline: float = 0.0, **kw) -> Watch:
    global _active
    stop(_active)
    _active = Watch(status, hard_deadline=hard_deadline, **kw)
    return _active


def stop(w: Optional[Watch]) -> None:
    global _active
    if w is None:
        return
    w.close()
    if _active is w:
        _active = None


def current() -> Optional[Watch]:
    return _active


def stage(name: str) -> None:
    if _active is not None:
        _active.stage(name)


def check() -> None:
    if _active is not None:
        _active.check()


_ticks = [0]


def tick(every: int = 200) -> None:
    """check() on every 200th call: for loops over pads or items, where one
    step at a fine pitch can run for many minutes."""
    _ticks[0] += 1
    if _ticks[0] % every == 0 and _active is not None:
        _active.check()


def overdue() -> bool:
    """Past the job's hard deadline? (Loops that can stop cleanly ask this.)"""
    return _active is not None and _active.overdue()


def set_hard_deadline(t: float) -> None:
    if _active is not None:
        _active.hard_deadline = t


def wall_time() -> float:
    """Current job's sleep-adjusted wall clock; ordinary epoch time if idle."""
    return _active.wall_time() if _active is not None else time.time()
