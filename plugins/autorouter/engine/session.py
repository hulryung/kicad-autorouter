"""The board side of a run: DSN out, SES in, and what the dialog needs to know
about the board. Everything that touches pcbnew lives here, imported lazily so
the rest of the engine stays importable without KiCad."""
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from .paths import data_dir

KEEP_JOBS = 10


@dataclass
class JobFiles:
    directory: Path
    dsn: Path
    ses: Path


def new_job(board_name: str) -> JobFiles:
    """A fresh working directory for one run; the DSN and SES stay there
    afterwards for inspection, and old jobs are pruned."""
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", board_name or "board").strip("_") or "board"
    jobs = data_dir() / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    directory = jobs / f"{time.strftime('%Y%m%d-%H%M%S')}-{stem}"
    directory.mkdir()
    _prune(jobs)
    return JobFiles(directory, directory / f"{stem}.dsn", directory / f"{stem}.ses")


def _prune(jobs: Path) -> None:
    old = sorted((p for p in jobs.iterdir() if p.is_dir()), key=lambda p: p.name)
    for path in old[:-KEEP_JOBS]:
        shutil.rmtree(path, ignore_errors=True)


def copper_layers(board) -> List[Tuple[int, str]]:
    """(layer id, name) for the enabled copper layers, top to bottom: the order
    KiCad's DSN export uses, which is what Freerouting's layer settings index."""
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


def export_dsn(board, path: Path) -> None:
    import pcbnew
    if not pcbnew.ExportSpecctraDSN(board, str(path)) or not path.is_file():
        raise RuntimeError("KiCad could not export the board to Specctra DSN. "
                           "Check that the board has a closed outline (Edge.Cuts).")


def import_ses(board, path: Path) -> None:
    import pcbnew
    if not pcbnew.ImportSpecctraSES(board, str(path)):
        raise RuntimeError(f"KiCad could not import the routed session {path.name}")


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
        filler = pcbnew.ZONE_FILLER(board)
        filler.Fill(board.Zones())
    return len(zones)
