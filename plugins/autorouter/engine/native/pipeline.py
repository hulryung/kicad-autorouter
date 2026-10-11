"""One routing job from the board's point of view: read, route, apply, and
escalate on what failed. Shared by the dialog, the AI assistant and the
headless tests so they all behave the same.

Escalation: connections the first pass could not route are tried again on a
finer grid (half the pitch), at the neck width, with cheaper vias, in a run
scoped to just those nets. That is what a designer does by hand for the last
few stubborn connections, and what the AI assistant found by itself.
"""
import copy
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import apply as native_apply, extract as native_extract, router as native_router
from .problem import NativeOptions, SCOPE_SELECTION


@dataclass
class JobResult:
    routed: int = 0
    failed: List[str] = field(default_factory=list)       # net names still open
    failed_detail: List[dict] = field(default_factory=list)
    ripped: int = 0
    fanout: int = 0
    pairs: int = 0
    stitched: int = 0
    pair_log: List[tuple] = field(default_factory=list)
    escalated: int = 0                                     # connections saved by the second pass
    passes: int = 1
    seconds: float = 0.0
    stats: dict = field(default_factory=dict)
    layers: list = field(default_factory=list)
    cancelled: bool = False


def fine_options(options: NativeOptions, failed_nets: List[str], board) -> NativeOptions:
    """The second pass's options: half the pitch, the neck width, cheaper
    vias, scoped to the open nets, no more pour vias."""
    fine = copy.deepcopy(options)
    fine.pitch_mm = max(0.025, options.pitch_mm / 2)
    fine.via_cost = max(5, options.via_cost // 3)
    fine.max_expansions = options.max_expansions * 3
    fine.scope, fine.scope_nets, fine.scope_refs = SCOPE_SELECTION, list(failed_nets), []
    fine.fanout_vias = False
    fine.stitch_vias = False
    fine.rip_rounds = max(fine.rip_rounds, 12)
    fine.rip_limit = max(fine.rip_limit, 8)
    # the stubborn ones go at the neck width for their whole length
    sizes = native_extract.board_sizes(board)
    narrow = [w for w in sizes["track_widths"] if w >= sizes["min_track_width"]]
    if narrow:
        fine.track_mode, fine.track_width = "fixed", min(narrow)
    return fine


def run_job(board, options: NativeOptions, status=None, progress=None, on_path=None, on_remove=None,
            cancelled: Optional[Callable[[], bool]] = None, escalate: bool = True,
            apply_paths: bool = True) -> JobResult:
    """Route with the options, then escalate on failures. When apply_paths
    is False the caller adds paths itself through on_path (the dialog does,
    for live display)."""
    out = JobResult()
    problem = native_extract.extract(board, options, status=status)
    out.layers = problem.layers
    out.stats = dict(problem.stats)
    result = native_router.run(problem, progress=progress, on_path=on_path, on_remove=on_remove, cancelled=cancelled)
    if apply_paths:
        native_apply.add_paths(board, problem.layers, result.paths)
    out.routed, out.ripped, out.fanout, out.pairs, out.stitched = (result.routed, result.ripped, result.fanout,
                                                                 result.pairs, result.stitched)
    out.pair_log = list(getattr(result, "pair_log", []))
    out.failed_detail = list(getattr(result, "failed_detail", []))
    out.seconds = result.seconds
    out.cancelled = result.cancelled
    failed_nets = sorted({problem.nets[c.net].name for c in result.failed})
    out.failed = failed_nets

    if escalate and failed_nets and not out.cancelled and options.pitch_mm > 0.04:
        if status:
            status(f"Second pass on {len(failed_nets)} open connection(s): finer grid, neck width, cheaper vias...")
        fine = fine_options(options, failed_nets, board)
        try:
            problem2 = native_extract.extract(board, fine, status=status)
        except Exception as e:
            out.stats["escalation_error"] = str(e)
            return out
        if problem2.connections:
            result2 = native_router.run(problem2, progress=progress, on_path=on_path, on_remove=on_remove,
                                        cancelled=cancelled)
            if apply_paths:
                native_apply.add_paths(board, problem2.layers, result2.paths)
            out.passes = 2
            out.escalated = result2.routed
            out.routed += result2.routed
            out.ripped += result2.ripped
            out.seconds += result2.seconds
            out.failed = sorted({problem2.nets[c.net].name for c in result2.failed})
            out.failed_detail = list(getattr(result2, "failed_detail", []))
            out.layers = problem2.layers if on_path is None else out.layers
    return out
