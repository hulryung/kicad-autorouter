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

from . import apply as native_apply, extract as native_extract, router as native_router, tuning as native_tuning
from .problem import NativeOptions, SCOPE_SELECTION

_REMOVED: list = []        # board items taken off headless; Python must not free them


@dataclass
class JobResult:
    routed: int = 0
    failed: List[str] = field(default_factory=list)       # net names still open
    failed_detail: List[dict] = field(default_factory=list)
    ripped: int = 0
    shoved: int = 0                 # existing board tracks moved out of the way (shove mode)
    fanout: int = 0
    pairs: int = 0
    pair_swaps: int = 0             # of those, coupled with a via swap
    pair_vias: int = 0              # via pairs where coupled runs change layers
    stitched: int = 0
    pair_log: List[tuple] = field(default_factory=list)
    escalated: int = 0                                     # connections saved by the second pass
    passes: int = 1
    seconds: float = 0.0
    stats: dict = field(default_factory=dict)
    layers: list = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False                                # the time limit cut a pass short
    tuning: List[dict] = field(default_factory=list)       # length groups: target, lengths before/after
    tuning_lines: List[str] = field(default_factory=list)
    pair_exceptions: List[dict] = field(default_factory=list)   # pairs connected directly, and why


FIRST_PASS_SHARE = 0.7          # of the time limit; the finer second pass gets the rest


def fine_options(options: NativeOptions, failed_nets: List[str], board) -> NativeOptions:
    """The second pass's options: half the pitch, the neck width, cheaper
    vias, scoped to the open nets, no more pour vias."""
    fine = copy.deepcopy(options)
    fine.pitch_mm = max(0.025, options.pitch_mm / 2)
    fine.via_cost = max(5, options.via_cost // 3)
    fine.max_expansions = options.max_expansions * 3
    fine.scope, fine.scope_nets, fine.scope_refs = SCOPE_SELECTION, list(failed_nets), []
    fine.fanout_vias = False
    fine.dual_escapes = True       # leave a stubborn pad by either end, whichever the search finds open
    fine.stitch_vias = False
    fine.rip_rounds = max(fine.rip_rounds, 12)
    fine.rip_limit = max(fine.rip_limit, 8)
    # the first pass's tracks are board copper now: let this pass rip up the
    # ones that wall a stubborn connection in and reroute them (shove mode)
    fine.shove = options.fine_shove
    # the stubborn ones go at the neck width for their whole length
    sizes = native_extract.board_sizes(board)
    narrow = [w for w in sizes["track_widths"] if w >= sizes["min_track_width"]]
    if narrow:
        fine.track_mode, fine.track_width = "fixed", min(narrow)
    return fine


def run_job(board, options: NativeOptions, status=None, progress=None, on_path=None, on_remove=None,
            cancelled: Optional[Callable[[], bool]] = None, escalate: bool = True,
            apply_paths: bool = True, extract_fn=None, on_layers=None) -> JobResult:
    """The whole job, in the order a designer works:
      1. signals at the normal grid (pour vias held back);
      2. a finer second pass on what is still open;
      3. the pours: fanout vias, ties and stitching in the room that is left.
    When apply_paths is False the caller adds paths itself through on_path
    (the dialog does, for live display); extract_fn lets the caller run the
    board reads on its UI thread; on_layers is told the layer list of each
    pass before its paths arrive."""
    extract = extract_fn or (lambda b, o, st: native_extract.extract(b, o, status=st))
    out = JobResult()
    added: list = []                       # board items this job created (apply_paths)
    paired_items: list = []                # ...of which the coupled pairs' lines
    import time as _time
    job_started = _time.time()
    limit = options.time_limit_s if options.time_limit_s and options.time_limit_s > 0 else 0.0

    def remaining() -> float:
        """Seconds left of the job's time limit (0 = no limit)."""
        return max(0.0, limit - (_time.time() - job_started)) if limit else 0.0

    def one_pass(opts: NativeOptions):
        problem = extract(board, opts, status)
        if problem is None:
            return None, None
        if on_layers:
            on_layers(problem.layers)
        movable = {ex.path.pid: list(ex.items) for ex in problem.existing}

        def remove(pid):
            # shove mode: a board piece ripped up comes off the board (the
            # caller does it itself when it places paths live)
            if apply_paths:
                for item in movable.pop(pid, []):
                    if native_router._DEBUG:
                        print(f"[remove] pid {pid} {item.GetClass()} {item.GetNetname()} at "
                              f"{item.GetPosition().x / 1e6:.2f},{item.GetPosition().y / 1e6:.2f}", flush=True)
                    try:
                        board.Remove(item)
                        item.thisown = 0
                        _REMOVED.append(item)
                    except Exception:
                        pass
            if on_remove:
                on_remove(pid)

        result = native_router.run(problem, progress=progress, on_path=on_path, on_remove=remove,
                                   cancelled=cancelled)
        if apply_paths:
            paired = getattr(result, "paired_pids", set())
            for rp in result.paths:
                items = native_apply.add_path(board, problem.layers, rp)
                added.extend(items)
                if rp.pid in paired:
                    paired_items.extend(items)       # a coupled pair's lines: no later pass moves them
            if result.shoved:
                try:
                    board.BuildConnectivity()
                except Exception:
                    pass
        return problem, result

    pours_later = options.fanout_vias or options.stitch_vias
    if options.fanout_vias and options.fanout_first and not options.pours_only:
        # Pad vias of the plane nets first, as a designer drops the decoupling
        # vias before routing signals: the pins keep their outlets (via guards)
        # and the signals route around the vias instead of walling them off.
        if status:
            status("Plane nets: pad vias before the signals...")
        early = copy.deepcopy(options)
        early.pours_only = True
        early.stitch_vias = False
        early.time_limit_s = 0
        try:
            problem0, result0 = one_pass(early)
        except Exception as e:
            out.stats["fanout_error"] = str(e)
            problem0 = None
        if problem0 is not None:
            out.fanout += result0.fanout
            out.seconds += result0.seconds
            # the sides the pads were left by, decided before those vias existed
            options = copy.copy(options)
            options.escape_sides = dict(problem0.escape_sides)
            # those vias are the job's own: a signal pass may rip one up that
            # walls in a connection, and the pour pass drops it again if room is left
            options.movable_uuids = [str(item.m_Uuid.AsString()) for item in added]
    first = copy.deepcopy(options)
    if pours_later:
        first.fanout_vias = False
        first.stitch_vias = False
    if limit:
        # the first pass leaves the finer second pass its share of the time:
        # once the first pass is down to rip-up rounds that gain a connection
        # a minute, the finer grid (neck width, cheaper vias, shove) does
        # better with the same minutes on what is still open
        share = FIRST_PASS_SHARE if escalate and options.pitch_mm > 0.04 else 1.0
        first.time_limit_s = max(1.0, remaining() * share)
    problem, result = one_pass(first)
    if problem is None:
        return out
    out.layers = problem.layers
    out.stats = dict(problem.stats)
    out.routed, out.ripped, out.pairs = result.routed, result.ripped, result.pairs
    out.shoved = result.shoved
    out.pair_swaps = getattr(result, "pair_swaps", 0)
    out.pair_vias = getattr(result, "pair_vias", 0)
    out.pair_log = list(getattr(result, "pair_log", []))
    out.failed_detail = list(getattr(result, "failed_detail", []))
    out.seconds = result.seconds
    out.cancelled = result.cancelled
    out.timed_out = result.timed_out
    out.failed = sorted({problem.nets[c.net].name for c in result.failed})
    out.pair_exceptions = list(getattr(result, "pair_exceptions", []))
    out.stats["budget_cuts"] = getattr(result, "budget_cuts", 0)
    if apply_paths and options.movable_uuids:
        # the plane vias as they stand now (ripped ones gone, restored ones in)
        gone = {str(item.m_Uuid.AsString()) for item in _REMOVED}
        options = copy.copy(options)
        options.movable_uuids = [u for u in (str(item.m_Uuid.AsString()) for item in added) if u not in gone]

    # the finer pass needs room in the time limit (its board read alone takes a while)
    time_for_more = not limit or remaining() > 60.0
    if not time_for_more:
        out.timed_out = True
        if status:
            status("Time limit reached: no finer second pass.")
    if escalate and out.failed and not out.cancelled and time_for_more and options.pitch_mm > 0.04 \
            and not (cancelled and cancelled()):
        if status:
            status(f"Second pass on {len(out.failed)} open connection(s): finer grid, neck width, cheaper vias...")
        fine = fine_options(options, out.failed, board)
        fine.locked_uuids = list(options.locked_uuids) + [str(item.m_Uuid.AsString()) for item in paired_items]
        # the finer grid is four times the cells: a stubborn connection could
        # search for minutes there, so the pass gets its own budget
        budget = options.fine_budget_s
        if budget is None:
            budget = 60.0 + 20.0 * len(out.failed)
        if budget and limit:
            fine.time_limit_s = min(budget, max(1.0, remaining()))
        elif limit:
            fine.time_limit_s = max(1.0, remaining())
        else:
            fine.time_limit_s = budget or 0
        try:
            problem2, result2 = one_pass(fine)
        except Exception as e:
            out.stats["escalation_error"] = str(e)
            problem2 = None
        if problem2 is not None and problem2.connections:
            out.passes = 2
            out.escalated = result2.routed
            out.stats["budget_cuts"] += getattr(result2, "budget_cuts", 0)
            out.routed += result2.routed
            out.ripped += result2.ripped
            out.shoved += result2.shoved
            out.seconds += result2.seconds
            out.failed = sorted({problem2.nets[c.net].name for c in result2.failed})
            out.failed_detail = list(getattr(result2, "failed_detail", []))
            # the finer pass running out of its own budget is not the job's
            # time limit: only report that when the job's limit was what ended it
            if result2.timed_out and limit and remaining() <= 1.0:
                out.timed_out = True

    if pours_later and not (cancelled and cancelled()):
        if status:
            status("Pour nets: fanout vias, ties and stitching in the remaining room...")
        pours = copy.deepcopy(options)
        pours.pours_only = True                       # same scope, no signal routing
        pours.time_limit_s = 0                        # the pour stages are bounded; always finish them
        pours.movable_uuids = []                      # nothing is ripped up here: the vias are fixed copper again
        try:
            problem3, result3 = one_pass(pours)
        except Exception as e:
            out.stats["pour_error"] = str(e)
            problem3 = None
        if problem3 is not None:
            out.fanout += result3.fanout
            out.stitched = result3.stitched
            out.seconds += result3.seconds
    if apply_paths and added:
        # SWIG wrappers do not compare by item: tell the ripped-up ones apart by UUID
        gone = {str(item.m_Uuid.AsString()) for item in _REMOVED}
        live = [t for t in added if str(t.m_Uuid.AsString()) not in gone]
        out.stats["trimmed"] = native_apply.trim_overlaps(board, live)
    if apply_paths and not (cancelled and cancelled()):
        report = tune_lengths(board, options, status, out.pair_exceptions)
        if report is not None:
            out.tuning, out.tuning_lines = report.groups, report.lines()
    return out


def waived_skew(board, options: NativeOptions, pair_exceptions) -> set:
    """Nets of pairs whose skew does not matter: the router waived their
    coupling as unnecessary and the whole pair is no longer than the
    uncoupled length allowed (a pair's few millimetres between a connector
    and its ESD part). Marks those exceptions with "skew_waived"."""
    out = set()
    by_name = {n.GetNetname(): c for c, n in board.GetNetsByNetcode().items() if c > 0}
    lengths = native_tuning.track_lengths(board)
    limit = options.pair_uncoupled_max_mm * 1_000_000
    for e in pair_exceptions or []:
        longest = max(lengths.get(by_name.get(n, -1), 0) for n in e["nets"])
        e["skew_waived"] = bool(e.get("exempt_skew") and longest <= limit)
        if e["skew_waived"]:
            out |= set(e["nets"])
    return out


def tune_lengths(board, options: NativeOptions, status=None, pair_exceptions=None):
    """Length matching after routing (and after the overlap merge): the
    options' length groups get meanders, wrapped in KiCad tuning patterns.
    A pair whose coupling the router waived as unnecessary (ends too close)
    is not skew-tuned either. None when there is nothing to tune. Main thread
    only (it edits the board)."""
    if not options.tune_lengths or not options.length_groups:
        return None
    waived = waived_skew(board, options, pair_exceptions)
    groups = [g for g in options.length_groups if not (g.get("pair") and set(g["nets"]) & waived)]
    if not groups:
        return None
    try:
        report = native_tuning.tune(board, groups, status=status, removed_sink=_REMOVED,
                                    kicad_patterns=options.kicad_tuning_patterns,
                                    rounded=getattr(options, "tune_rounded", True))
    except Exception as e:
        if status:
            status(f"Length matching failed: {e!r}")
        return None
    if status:
        for line in report.lines():
            status("Length matching: " + line)
    return report
