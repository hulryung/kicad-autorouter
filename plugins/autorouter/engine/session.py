"""The board side of a run: job folders and what the dialog needs to know
about the board. Everything that touches pcbnew lives here, imported lazily so
the rest of the engine stays importable without KiCad."""
from .compat import zone_filler

import re
import shutil
import time
import tempfile
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from .paths import data_dir

KEEP_JOBS = 10


@dataclass
class JobFiles:
    directory: Path
    _lease: object = field(default=None, repr=False)

    def close(self):
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def __del__(self):
        self.close()


def _lock(stream):
    """Nonblocking process lease, released by the OS even after a crash."""
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def new_job(board_name: str) -> JobFiles:
    """A fresh working directory for one run (its log, the AI context and
    replies stay there for inspection); old jobs are pruned."""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", board_name or "board").strip("_") or "board"
    jobs = data_dir() / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix=f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}-", dir=jobs))
    lease = (directory / '.active.lock').open('w+b')
    lease.write(b'1')
    lease.flush()
    _lock(lease)
    job = JobFiles(directory, lease)
    _prune(jobs)
    return job


def _prune(jobs: Path) -> None:
    old = sorted((p for p in jobs.iterdir() if p.is_dir()), key=lambda p: p.name)
    for path in old[:-KEEP_JOBS]:
        # Unmarked folders may belong to older plugin instances or a job
        # being created. Never infer that they are safe to remove.
        try:
            with (path / '.active.lock').open('r+b') as lease:
                _lock(lease)
            # No job reopens a lease after releasing it. Close before deleting
            # so Windows can remove the file as well.
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def copper_layers(board) -> List[Tuple[int, str]]:
    """(layer id, name) for the enabled copper layers, top to bottom."""
    return [(layer, board.GetLayerName(layer)) for layer in board.GetEnabledLayers().CuStack()]


def net_class_names(board) -> List[str]:
    """Net class names in use on the board, Default first."""
    names = set()
    for net in board.GetNetsByNetcode().values():
        if net.GetNetCode() > 0:
            names.add(net.GetNetClassName())
    ordered = sorted(names)
    if "Default" in ordered:
        ordered.remove("Default")
        ordered.insert(0, "Default")
    return ordered


def unrouted_count(board) -> int:
    """Connections the ratsnest still shows, if KiCad can tell us."""
    try:
        return board.GetConnectivity().GetUnconnectedCount(True)
    except Exception:
        return -1


_REMOVED = []        # proxies of removed items, kept alive (see BoardActions._forget)


def remove_tracks(board) -> int:
    """Remove every track, arc and via. Inside KiCad the removed items go to
    the undo list; headless they become Python-owned and are kept alive here."""
    items = list(board.GetTracks())
    for item in items:
        board.Remove(item)
        try:
            item.thisown = 0          # Python must never delete board items
        except Exception:
            pass
    _REMOVED.extend(items)
    try:
        board.BuildConnectivity()
    except Exception:
        pass
    return len(items)


def refill_zones(board) -> int:
    """Refill every copper zone so pours clear the new tracks and vias, as
    Edit > Fill All Zones does. Returns the number of zones filled."""
    import pcbnew
    zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
    if zones:
        filler = zone_filler(board)
        filler.Fill(board.Zones())
    return len(zones)
