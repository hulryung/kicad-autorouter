"""One routing job from the board's point of view: read, route, apply, and
escalate on what failed. Shared by the dialog, the AI assistant and the
headless tests so they all behave the same.

Escalation: connections the first pass could not route are tried again on a
finer grid (half the pitch), at the neck width, with cheaper vias, in a run
scoped to just those nets. That is what a designer does by hand for the last
few stubborn connections, and what the AI assistant found by itself.
"""
from ..compat import zone_filler

import copy
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pcbnew

from . import watchdog
from . import apply as native_apply, astar as native_astar, extract as native_extract, router as native_router, tuning as native_tuning
from .problem import NativeOptions, SCOPE_BOARD, SCOPE_SELECTION

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
    wall_capped: bool = False       # the job's wall-clock cap (NativeOptions.wall_limit_s) cut it short
    tuning: List[dict] = field(default_factory=list)       # length groups: target, lengths before/after
    tuning_lines: List[str] = field(default_factory=list)
    pair_exceptions: List[dict] = field(default_factory=list)   # pairs connected directly, and why
    pair_loose: List[dict] = field(default_factory=list)        # pairs coupled but finished single-ended at one end
    # The pads of every net with a copper pour, as its copper (the fills
    # included) joined them when the job started: {net code: [pad UUIDs of
    # each group of two or more]}. check_plane_pads compares the finished,
    # refilled board with it: a pad the pour joined before the job and the
    # job's copper cut off is no connection the router lists (issue #10).
    plane_start: dict = field(default_factory=dict)
    plane_codes: List[int] = field(default_factory=list)   # the job's pour nets (not routed with tracks)


# Of the time limit; the finer second pass gets the rest. The rip-up rounds
# of the first pass close more of the last few opens than the finer grid
# does (its failures are capped open searches): on a CM5 whose outward pair
# fans wall in row pins, rip-up round 2 routes I2C_SDA/SCL once it has the
# budget, while a pass cut at 0.7 of a slower work rate left both to the
# finer pass, which joined neither. 0.15 still gives that pass 90 s of 10 min.
FIRST_PASS_SHARE = 0.85


WALL_FACTOR = 1.5            # the automatic wall-clock cap: this times the time limit...
WALL_MIN_EXTRA_S = 2 * 60.0  # ...plus this (a 4-minute job stops by 8 minutes, a 10-minute one by 17)
# Past the cap the pour passes still run (a pad without its plane via is an
# open connection), within this grace; then the job stops hard: a board read
# or pour stage still going is cut off (watchdog.Overrun) and reported.
POST_CAP_SHARE = 0.2
POST_CAP_MIN_S = 60.0


def post_cap_grace(cap: float) -> float:
    return max(POST_CAP_MIN_S, POST_CAP_SHARE * cap) if cap else 0.0


def wall_cap(options: NativeOptions) -> float:
    """Seconds of wall clock the whole job may take (0 = no cap)."""
    w = getattr(options, "wall_limit_s", None)
    if w is not None:
        return max(0.0, float(w))
    limit = options.time_limit_s if options.time_limit_s and options.time_limit_s > 0 else 0.0
    return limit * WALL_FACTOR + WALL_MIN_EXTRA_S if limit else 0.0


def fine_options(options: NativeOptions, failed_nets: List[str], board) -> NativeOptions:
    """The second pass's options: half the pitch, the neck width, cheaper
    vias, scoped to the open nets, no more pour vias."""
    fine = copy.deepcopy(options)
    fine.pitch_mm = max(0.025, options.pitch_mm / 2)
    fine.via_cost = max(5, options.via_cost // 3)
    fine.max_expansions = options.max_expansions * 3
    fine.scope, fine.scope_nets, fine.scope_refs = SCOPE_SELECTION, list(failed_nets), []
    # the pairs outside a scope the user chose stay put; a whole-board job's
    # own pairs are this job's to move (its coupled lines are locked anyway)
    fine.scope_locks_pairs = options.scope_locks_pairs and options.scope != SCOPE_BOARD
    fine.fanout_vias = False
    fine.dual_escapes = True       # leave a stubborn pad by either end, whichever the search finds open
    # Keep the user's fabrication constraint through escalation. A finer
    # search may only use vias in small pads when the user opted in.
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
            apply_paths: bool = True, extract_fn=None, on_layers=None, placed=None, on_board=None) -> JobResult:
    """The job under a watchdog (see watchdog.py): its steps timed, a step
    that stalls reported with where it is, and a hard stop at the wall cap
    plus the pour passes' grace. The slow steps land in stats["slow_stages"]
    ([name, seconds]) and stats["stalls"] ([name, seconds, where])."""
    cap = wall_cap(options)
    hard = time.time() + cap + post_cap_grace(cap) if cap else 0.0
    w = watchdog.start(status, hard_deadline=hard)
    vias_before = _via_ids(board) if apply_paths and not options.via_in_small_pads else None
    try:
        out = _run_job(board, options, status, progress, on_path, on_remove, cancelled, escalate,
                       apply_paths, extract_fn, on_layers, placed, on_board)
    finally:
        watchdog.stop(w)
    if vias_before is not None:
        # Defensive audit: every pass must respect the fabrication constraint.
        try:
            hits = _vias_in_small_pads(board, vias_before, int(options.via_in_pad_min_mm * 1_000_000))
        except Exception as e:
            hits = []
            out.stats["via_in_pad_error"] = repr(e)
        if hits:
            out.stats["vias_in_small_pads"] = hits
            if status:
                status(f"{len(hits)} via(s) violate the small-SMD-pad restriction: {', '.join(hits[:8])}"
                       + (" ..." if len(hits) > 8 else ""))
    out.stats["slow_stages"] = [list(t) for t in w.slow()]
    out.stats["stalls"] = [[n, round(sec), where] for n, sec, where in w.stalls]
    if w.overrun:
        out.stats["overrun"] = w.overrun
    return out


def _via_ids(board) -> set:
    return {str(t.m_Uuid.AsString()) for t in board.GetTracks() if t.GetClass() == "PCB_VIA"}


def _vias_in_small_pads(board, before: set, min_nm: int) -> List[str]:
    """'REF.PAD' of each small SMD pad a via this job added overlaps (its own net's)."""
    by_net: Dict[int, list] = {}
    for pad in board.GetPads():
        if pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD:
            size = pad.GetSize()
            if min(size.x, size.y) < min_nm:
                by_net.setdefault(pad.GetNetCode(), []).append(pad)
    hits = []
    for v in board.GetTracks():
        if v.GetClass() != "PCB_VIA" or str(v.m_Uuid.AsString()) in before:
            continue
        pos = v.GetPosition()
        try:
            r = v.GetWidth(pcbnew.F_Cu) // 2             # KiCad 9+: per-layer via size
        except TypeError:
            r = v.GetWidth() // 2
        for pad in by_net.get(v.GetNetCode(), []):
            if pad.HitTest(pos, r):
                hits.append(f"{pad.GetParentFootprint().GetReference()}.{pad.GetNumber()}")
    return sorted(set(hits))


def _overrun(out, e, status, what: str) -> None:
    """A pass cut off at the job's hard deadline: keep what the job has."""
    out.timed_out = out.wall_capped = True
    out.stats.setdefault("overrun_passes", []).append(what)
    if status:
        status(f"Hard stop: {what} cut off past the wall-clock cap and its grace ({e}).")


def _run_job(board, options: NativeOptions, status, progress, on_path, on_remove, cancelled, escalate,
             apply_paths, extract_fn, on_layers, placed, on_board) -> JobResult:
    """The whole job, in the order a designer works:
      1. signals at the normal grid (pour vias held back);
      2. a finer second pass on what is still open;
      3. the pours: fanout vias, ties and stitching in the room that is left.
    When apply_paths is False the caller adds paths itself through on_path
    (the dialog does, for live display); extract_fn lets the caller run the
    board reads on its UI thread; on_layers is told the layer list of each
    pass before its paths arrive; placed (with apply_paths False) returns the
    UUIDs of the items the caller has put on the board for this job so far,
    so a later pass may move the job's own plane vias as it does otherwise;
    on_board runs a function on the thread that owns the board and returns
    its result (the dialog's UI thread; called directly when None)."""
    extract = extract_fn or (lambda b, o, st: native_extract.extract(b, o, status=st))
    out = JobResult()
    added: list = []                       # board items this job created (apply_paths)
    paired_items: list = []                # ...of which the coupled pairs' lines
    job_started = native_astar.clock()        # the wall clock, or the search work done (benchmarks)
    limit = options.time_limit_s if options.time_limit_s and options.time_limit_s > 0 else 0.0
    cap = wall_cap(options)
    if cap:
        options = copy.copy(options)
        options.wall_deadline = time.time() + cap

    def wall_over() -> bool:
        """Past the job's wall-clock cap (see NativeOptions.wall_limit_s)?"""
        return bool(cap) and time.time() > options.wall_deadline

    def job_items() -> list:
        """UUIDs of the items this job has on the board now (ripped ones gone)."""
        if not apply_paths:
            return list(placed()) if placed else []
        gone = {str(item.m_Uuid.AsString()) for item in _REMOVED}
        return [u for u in (str(item.m_Uuid.AsString()) for item in added) if u not in gone]

    def remaining() -> float:
        """Seconds left of the job's time limit (0 = no limit)."""
        return max(0.0, limit - (native_astar.clock() - job_started)) if limit else 0.0

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

    if apply_paths or on_path:
        # which pads each pour joins before this job lays any copper (the
        # board's fills as they are: nothing refilled, nothing changed)
        try:
            snap = lambda: plane_pad_snapshot(board, options.skip_nets)
            out.plane_start = (on_board(snap) if on_board else snap()) or {}
        except Exception as e:
            out.stats["plane_snapshot_error"] = repr(e)

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
            options.movable_uuids = job_items()
        problem0 = result0 = None       # its owner maps (a GB at 0.1 mm) need not live on through the job
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
    try:
        problem, result = one_pass(first)
    except watchdog.Overrun as e:
        _overrun(out, e, status, "the routing pass")
        # nothing was tried: not "0 routed, 0 failed". The scoped nets are
        # open (a whole-board job names no list, the flag says it)
        out.stats["routing_not_tried"] = True
        if options.scope == SCOPE_SELECTION and options.scope_nets:
            out.failed = sorted(set(options.scope_nets))
            out.failed_detail = [{"net": n, "reason": "not tried: the board read was cut off at the hard stop"}
                                 for n in out.failed]
        if status:
            status("Nothing was routed: the board read did not finish before the hard stop "
                   "(a finer pitch multiplies the read time; try a coarser pitch or a longer limit).")
        return out
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
    out.pair_loose = list(getattr(result, "pair_loose", []))
    out.stats["budget_cuts"] = getattr(result, "budget_cuts", 0)
    # The pass's owner maps are not read again: let them go before the next
    # board read (the finer pass's maps are four times the size, and holding
    # both on a big board is gigabytes). The result stays (its failures).
    pour_joins = dict(problem.stats.get("pour_joins", {}))
    first_nets = problem.nets
    problem = None
    if options.movable_uuids and (apply_paths or placed):
        # the plane vias as they stand now (ripped ones gone, restored ones in)
        options = copy.copy(options)
        options.movable_uuids = job_items()

    # the finer pass needs room in the time limit (its board read alone takes a while)
    time_for_more = not limit or remaining() > 60.0
    if not time_for_more:
        out.timed_out = True
        if status:
            status("Time limit reached: no finer second pass.")
    elif wall_over():
        time_for_more = False
        out.timed_out = out.wall_capped = True
        if status:
            status(f"Wall-clock cap reached ({cap / 60:.0f} min): no finer second pass.")
    if getattr(result, "wall_capped", False):
        out.wall_capped = True
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
        except watchdog.Overrun as e:
            _overrun(out, e, status, "the finer second pass")
            problem2 = None
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
        problem2 = None                 # (its maps: as the first pass's, above)

    # Routes that ended on their net's pour rather than at a pad: the pour
    # was the one filled before this run, and a route of another net laid
    # across it since (or the refill pulling back from the new copper) can
    # leave such an end in a piece the pads are no longer in. Refill, and
    # route what came apart again, pad to pad this time.
    joins = {name: info for name, info in pour_joins.items() if name not in out.failed}
    if joins and (apply_paths or on_path) and not out.cancelled and not (cancelled and cancelled()):
        try:
            check = lambda: _open_pour_joins(board, joins, result)
            apart = on_board(check) if on_board else check()
        except Exception as e:
            out.stats["pour_check_error"] = repr(e)
            apart = []
        if apart and wall_over():
            out.wall_capped = True
            if status:
                status(f"Wall-clock cap reached: {len(apart)} net(s) that came apart from their pour are left open.")
            out.failed = sorted(set(out.failed) | set(apart))
            apart = []
        if apart:
            if status:
                status(f"{len(apart)} net(s) came apart from their pour at a route's end: routing them pad to pad...")
            again = copy.deepcopy(options)
            again.pour_goals = False
            again.scope, again.scope_nets, again.scope_refs = SCOPE_SELECTION, list(apart), []
            again.scope_locks_pairs = options.scope_locks_pairs and options.scope != SCOPE_BOARD
            again.fanout_vias = False
            again.stitch_vias = False
            again.time_limit_s = max(1.0, min(60.0, remaining())) if limit else 60.0
            try:
                problem5, result5 = one_pass(again)
            except watchdog.Overrun as e:
                _overrun(out, e, status, "the pour re-route")
                problem5 = None
            except Exception as e:
                out.stats["pour_check_error"] = repr(e)
                problem5 = None
            if problem5 is not None:
                out.stats["pour_joins_redone"] = result5.routed
                out.routed += result5.routed
                out.seconds += result5.seconds
                out.failed = sorted(set(out.failed) | {problem5.nets[c.net].name for c in result5.failed})
            problem5 = None

    problem3 = None
    if pours_later and not (cancelled and cancelled()):
        if status:
            status("Pour nets: fanout vias, ties and stitching in the remaining room...")
        pours = copy.deepcopy(options)
        pours.pours_only = True                       # same scope, no signal routing
        pours.time_limit_s = 0                        # the pour stages run on past the cap, to the hard stop
        pours.wall_deadline = 0.0
        pours.movable_uuids = []                      # nothing is ripped up here: the vias are fixed copper again
        try:
            problem3, result3 = one_pass(pours)
        except watchdog.Overrun as e:
            _overrun(out, e, status, "the pour pass")
            problem3 = None
        except Exception as e:
            out.stats["pour_error"] = str(e)
            problem3 = None
        if problem3 is not None:
            out.fanout += result3.fanout
            out.stitched = result3.stitched
            out.seconds += result3.seconds
    pours_done = problem3 is not None
    pour_nets = problem3.nets if pours_done else first_nets
    problem3 = None
    # Signal rerouting as well as power repairs can split surface copper.
    # Refill once more and reconnect isolated islands; extraction returns
    # immediately when there are no stitch candidates.
    if (pours_later and pours_done and options.stitch_vias
            and not (cancelled and cancelled())):
        islands = copy.deepcopy(options)
        islands.pours_only = True
        islands.repair_islands_only = True
        islands.fanout_vias = False
        islands.stitch_vias = False
        islands.movable_uuids = []
        islands.time_limit_s = 0
        islands.wall_deadline = 0.0
        if watchdog.overdue():
            problem4 = None
            _overrun(out, "no time left", status, "the pour island check")
        else:
            if status:
                status("Checking repaired pours for isolated copper...")
            try:
                problem4, result4 = one_pass(islands)
            except watchdog.Overrun as e:
                _overrun(out, e, status, "the pour island check")
                problem4 = None
        if problem4 is not None:
            out.stitched += result4.stitched
            out.seconds += result4.seconds
    pour_codes = {code for code, net in (pour_nets or {}).items() if not net.route}
    out.plane_codes = sorted(c for c in pour_codes if not _skipped(pour_nets[c].name, options.skip_nets))
    if out.plane_start and (apply_paths or on_path) and not out.cancelled and not (cancelled and cancelled()):
        # A pad its pour joined before the job, that the job's copper has cut
        # off (two routes closing the pour's way in between a QFP's pins):
        # no connection of the job, and no via site where the pour pass or
        # the island check could tie it. Route it as a stray of its net, pad
        # to the nearest pad (and its copper) the net still has.
        _rejoin_cut_off(board, out, options, one_pass, on_board, status,
                        remaining() if limit else 0.0, wall_over())
    if apply_paths and added:
        # SWIG wrappers do not compare by item: tell the ripped-up ones apart by UUID
        gone = {str(item.m_Uuid.AsString()) for item in _REMOVED}
        live = [t for t in added if str(t.m_Uuid.AsString()) not in gone]
        watchdog.stage("clean-up of the copper")
        out.stats.update(finish_copper(board, live, options, cancelled))
    if apply_paths and not (cancelled and cancelled()):
        # pour nets are not connections the router lists: a pad its plane
        # via could not reach, or one the job's copper cut off its pour,
        # stays open without a word in `failed` (one net reported, 23 opens).
        # Ask the board, after the refill. (A caller that places the paths
        # itself runs check_plane_pads after its own clean-up.)
        watchdog.stage("pour nets left open")
        for line in check_plane_pads(board, out, options.skip_nets):
            if status:
                status(line)
    if apply_paths and not (cancelled and cancelled()):
        watchdog.stage("length tuning")
        report = tune_lengths(board, options, status, out.pair_exceptions)
        if report is not None:
            out.tuning, out.tuning_lines = report.groups, report.lines()
    if apply_paths and first_nets is not None:
        # each pair's two lengths and vias as routed and tuned: a 9.5 mm skew on a pair
        # nobody gave a length group was in no report
        try:
            out.stats["pair_lengths"] = _pair_lengths(board, first_nets)
        except Exception as e:
            out.stats["pair_lengths_error"] = repr(e)
        for d in out.stats.get("pair_lengths", []):
            out.pair_log.append((d["pair"], f"lengths {d['length_mm'][0]:.1f} / {d['length_mm'][1]:.1f} mm "
                                             f"(skew {d['skew_mm']:.1f} mm), vias {d['vias'][0]} / {d['vias'][1]}"))
    return out


def _pair_lengths(board, nets) -> List[dict]:
    """Track length and via count of both nets of every coupled pair the
    job knew of, as the board has them now."""
    pairs = sorted({tuple(sorted((code, net.partner))) for code, net in nets.items()
                    if net.partner and net.partner in nets})
    if not pairs:
        return []
    want = {c for pair in pairs for c in pair}
    length = {c: 0.0 for c in want}
    vias = {c: 0 for c in want}
    for item in board.GetTracks():
        code = item.GetNetCode()
        if code not in want:
            continue
        if item.GetClass() == "PCB_VIA":
            vias[code] += 1
        else:
            length[code] += item.GetLength() / 1e6
    out = []
    for a, b in pairs:
        if nets[a].name > nets[b].name:
            a, b = b, a
        out.append({"pair": f"{nets[a].name} / {nets[b].name}",
                    "length_mm": [round(length[a], 2), round(length[b], 2)],
                    "skew_mm": round(abs(length[a] - length[b]), 2),
                    "vias": [vias[a], vias[b]]})
    return out


def _skipped(name: str, skip_patterns) -> bool:
    import fnmatch
    return any(fnmatch.fnmatchcase(name, pat) or name == pat for pat in skip_patterns or ())


def _uid(item) -> str:
    return str(item.m_Uuid.AsString())


def _pad_label(pad) -> str:
    fp = pad.GetParentFootprint()
    return f"{fp.GetReference() if fp is not None else '?'}.{pad.GetNumber()}"


def _refill(board) -> None:
    zone_filler(board).Fill(board.Zones())
    board.BuildConnectivity()


def plane_pad_snapshot(board, skip_patterns=()) -> dict:
    """{net code: [set of pad UUIDs per group of two or more pads]} for every
    net with a copper pour (rule areas aside), as the board's copper and its
    fills join the pads now. Taken before a job lays any copper: nothing is
    refilled (a job must not change what it starts from), so a stale fill is
    what it is compared with."""
    codes = {z.GetNetCode() for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetCode() > 0}
    if not codes:
        return {}
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    out = {}
    for code in sorted(codes):
        net = board.FindNet(code)
        name = str(net.GetNetname()) if net is not None else ""
        if not name or _skipped(name, skip_patterns):
            continue
        groups = [{_uid(p) for p in g} for g in native_apply._pad_groups(board, conn, code) if len(g) > 1]
        if groups:
            out[code] = groups
    return out


def _fill_area_at(board, pad) -> float:
    """The area (mm^2) of the biggest piece of its net's fill under the pad's
    centre on any of its layers: 0 for a pad no fill reaches."""
    best = 0.0
    pos = pad.GetPosition()
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetCode() != pad.GetNetCode():
            continue
        for lid in z.GetLayerSet().CuStack():
            if not pad.IsOnLayer(lid):
                continue
            try:
                polys = z.GetFilledPolysList(lid)
                for i in range(polys.OutlineCount()):
                    if polys.Contains(pos, i):
                        best = max(best, abs(polys.Outline(i).Area()) / 1e12)
            except Exception:
                continue
    return best


def lost_plane_pads(board, start: dict) -> List[dict]:
    """The pads of start (plane_pad_snapshot) that the board's copper, as
    filled now, no longer joins to the rest of their group: [{"net", "pour":
    True, "lost": True, "pads": [ref.pad, up to 8], "uuids", "reason"}], one
    entry per net. Of a group that came apart, the part with most of its
    pads stays (then the bigger group now, then the bigger fill under it);
    the other parts are the lost ones. Call it on a refilled board."""
    if not start:
        return []
    conn = board.GetConnectivity()
    out = []
    for code, groups in sorted(start.items()):
        now = native_apply._pad_groups(board, conn, code)
        where, pad_of = {}, {}
        for k, g in enumerate(now):
            for p in g:
                where[_uid(p)] = k
                pad_of[_uid(p)] = p
        lost = []
        for g in groups:
            parts: dict = {}
            for u in g:
                if u in where:
                    parts.setdefault(where[u], []).append(u)
            if len(parts) < 2:
                continue
            keep = max(parts, key=lambda k: (len(parts[k]), len(now[k]),
                                              max(_fill_area_at(board, pad_of[u]) for u in parts[k])))
            lost += sorted((u for k, us in parts.items() if k != keep for u in us),
                           key=lambda u: _pad_label(pad_of[u]))
        if not lost:
            continue
        net = board.FindNet(code)
        name = str(net.GetNetname()) if net is not None else str(code)
        labels = [_pad_label(pad_of[u]) for u in lost]
        out.append({"net": name, "pour": True, "lost": True, "pads": labels[:8], "uuids": lost,
                    "reason": f"{len(lost)} pad(s) its pour joined before the job are cut off after the "
                              f"final refill: the job's copper closed the pour's way in, and no via or "
                              f"track joined them again"})
    return out


def _rejoin_cut_off(board, out: JobResult, options: NativeOptions, one_pass, on_board, status,
                    left: float, over: bool) -> None:
    """Refill, find the pads the job cut off their pour (lost_plane_pads) and
    route each as a stray of its net, pad to the nearest pad of the net
    (with its copper), on any routable layer, the way the router joins a
    pad no pour reaches. What stays cut off is reported by check_plane_pads."""
    try:
        def check():
            _refill(board)
            return lost_plane_pads(board, out.plane_start)
        lost = on_board(check) if on_board else check()
    except Exception as e:
        out.stats["plane_rejoin_error"] = repr(e)
        return
    if not lost:
        return
    if over:
        out.wall_capped = True
        return
    nets = sorted({d["net"] for d in lost})
    n = sum(len(d["uuids"]) for d in lost)
    if status:
        status(f"{n} pad(s) of {', '.join(nets)} cut off their pour by the new copper: routing them to their net...")
    again = copy.deepcopy(options)
    again.scope, again.scope_nets, again.scope_refs = SCOPE_SELECTION, nets, []
    again.stray_pads = [u for d in lost for u in d["uuids"]]
    again.pour_goals = False           # pad to pad: the pour near the pad is the piece it was cut off with
    again.fanout_vias = again.stitch_vias = False
    again.pours_only = again.repair_islands_only = False
    again.shove = False
    again.movable_uuids = []
    again.time_limit_s = max(1.0, min(60.0, left)) if left else 60.0
    try:
        problem, result = one_pass(again)
    except watchdog.Overrun as e:
        _overrun(out, e, status, "the cut-off pads' routes")
        return
    except Exception as e:
        out.stats["plane_rejoin_error"] = repr(e)
        return
    if problem is not None:
        out.stats["cut_off_pads"] = n
        out.stats["cut_off_routed"] = result.routed
        out.routed += result.routed
        out.seconds += result.seconds
        detail = list(getattr(result, "failed_detail", []))
        if detail:
            out.stats["cut_off_detail"] = detail


def check_plane_pads(board, out: JobResult, skip_patterns=(), refill: bool = True) -> List[str]:
    """The job's last word on its pour nets, on the finished board (after
    the clean-up; refilled here): the pads a pour joined at the start that
    it no longer joins (lost_plane_pads), and the pour nets whose pads are
    in more than one group (_open_pour_nets). Both go into out.failed and
    out.failed_detail (stats "plane_pads_lost", "pour_nets_open"); returns
    status lines. Once per job: the pipeline runs it when it placed the
    paths, a caller that placed them itself (the dialog) after its own
    finish_copper."""
    if out.stats.get("plane_checked"):
        return []
    out.stats["plane_checked"] = True
    try:
        if refill:
            _refill(board)
        lost = lost_plane_pads(board, out.plane_start)
    except Exception as e:
        out.stats["plane_lost_error"] = repr(e)
        lost = []
    try:
        still = _open_pour_nets(board, set(out.plane_codes), skip_patterns, refill=False)
    except Exception as e:
        out.stats["pour_open_error"] = repr(e)
        still = []
    lines = []
    if lost:
        if out.stats.get("cut_off_pads"):
            for d in lost:
                d["reason"] += " (a route from the pad to the rest of the net found no way either)"
        out.stats["plane_pads_lost"] = [{k: v for k, v in d.items() if k != "uuids"} for d in lost]
        for d in lost:
            more = len(d["uuids"]) - len(d["pads"])
            lines.append(f"{d['net']}: {', '.join(d['pads'])}{f' and {more} more' if more > 0 else ''} "
                         f"cut off its pour by the new copper (open after the refill)")
    # a net already named by its lost pads: its group count says the same
    named = {d["net"] for d in lost}
    still = [d for d in still if d["net"] not in named]
    if still:
        out.stats["pour_nets_open"] = still
        for d in still:
            lines.append(f"{d['net']}: {d['groups'] - 1} pad group(s) not joined to the pour "
                         f"({', '.join(d['pads'])})")
    found = [{k: v for k, v in d.items() if k != "uuids"} for d in lost] + still
    if found:
        out.failed = sorted(set(out.failed) | {d["net"] for d in found})
        out.failed_detail = list(out.failed_detail) + found
    return lines


def _open_pour_nets(board, codes, skip_patterns=(), refill: bool = True) -> List[dict]:
    """Pour nets whose pads the refilled board still holds in more than one
    group: [{"net", "pour": True, "groups", "pads": [ref.pad of each group
    apart from the biggest, up to 8], "reason"}]."""
    if not codes:
        return []
    if refill:
        _refill(board)
    conn = board.GetConnectivity()
    out = []
    for code in sorted(codes):
        net = board.FindNet(code) if hasattr(board, "FindNet") else None
        name = str(net.GetNetname()) if net is not None else ""
        if not name or _skipped(name, skip_patterns):
            continue
        groups = native_apply._pad_groups(board, conn, code)
        if len(groups) <= 1:
            continue
        apart = [f"{p.GetParentFootprint().GetReference()}.{p.GetNumber()}" for g in groups[1:] for p in g][:8]
        out.append({"net": name, "pour": True, "groups": len(groups), "pads": apart,
                    "reason": f"{len(groups) - 1} pad group(s) not joined to the pour or the rest of the net "
                              f"(no via reached this net's fill, or the via landed in another net's pour)"})
    return out


def _open_pour_joins(board, joins: dict, result) -> List[str]:
    """Of the nets whose routes may have ended on their pour (extract's
    "pour_joins": pad groups before the run, connections in all and in
    scope), those the refilled board has in more groups than the routed
    connections leave."""
    zone_filler(board).Fill(board.Zones())
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    failed = Counter(c.net for c in result.failed)
    out = []
    for name, info in joins.items():
        net = board.FindNet(name)
        if net is None:
            continue
        code = net.GetNetCode()
        if info["route"]:
            # a routed net: known only when all of it was this job's to join
            if info["scoped"] != info["all"] or failed[code]:
                continue
            expected = 1
        else:
            # a plane net: each routed stray is one group fewer
            expected = info["groups"] - (info["scoped"] - failed[code])
        if len(native_apply._pad_groups(board, conn, code)) > max(1, expected):
            out.append(name)
    return out


def finish_copper(board, live: list, options: NativeOptions, cancelled=None) -> dict:
    """The clean-up of a run's copper once nothing will be ripped up any
    more, the same whether the pipeline placed the paths or the dialog did
    (live display): shared copper written once, tiny off-angle joins snapped,
    vias that join nothing taken off with the stubs that led to them, every
    junction welded into a shared end point, power rails widened where there
    is room, pour islands the copper cut off tied back to their net. live: the items the run created that
    are still on the board. Returns counts (and *_error entries)."""
    stats: dict = {}

    def alive(items):
        gone = {str(item.m_Uuid.AsString()) for item in native_apply._REMOVED}
        return [t for t in items if str(t.m_Uuid.AsString()) not in gone]

    stats["trimmed"] = native_apply.trim_overlaps(board, live)
    try:
        # tiny odd-angle joins where a run ends off the grid: the 45-degree
        # segments either side meet directly instead
        stats["tiny_joins_snapped"] = native_apply.snap_tiny_joins(board, alive(live))
    except Exception as e:
        stats["snap_error"] = repr(e)
    if cancelled and cancelled():
        return stats
    # vias that ended up joining nothing (a pad via that missed the plane's
    # fill, a fanout via a route made unnecessary): off the board
    live = alive(live)
    try:
        stats["dangling_vias_removed"] = native_apply.prune_dangling_vias(board, live)
    except Exception as e:
        stats["prune_error"] = repr(e)
    # every junction of the run's copper a shared end point (via centres,
    # T points split), the way KiCad's own router leaves it
    try:
        live[:] = alive(live)
        welded = native_apply.weld_junctions(board, live, locked=options.locked_uuids)
        stats["welded"] = sum(welded.values())
        stats["weld_detail"] = welded
    except Exception as e:                     # cosmetic: never lose a routed board over it
        stats["weld_error"] = repr(e)
    # power rails: as wide as the finished board leaves room for
    try:
        stats["power_widened"] = native_apply.widen_power(board, alive(live), options.power_patterns)
    except Exception as e:
        stats["widen_error"] = repr(e)
    if cancelled and cancelled():
        return stats
    # pieces of a pour that the finished copper cut off with only their own
    # pads in them: a stub and via to the net's connected fill
    try:
        # the job's hard stop bounds the island repair too (after a scoped
        # pass it was the slow step: 20 minutes of a "4-minute" job)
        stop = lambda: bool(cancelled and cancelled()) or watchdog.overdue()
        stats["islands_joined"] = native_apply.repair_islands(board, live, cancelled=stop)
    except Exception as e:
        stats["island_error"] = repr(e)
    return stats


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
