"""A route function for the assistant that works without the dialog: runs
the built-in router on a loaded board (headless tests, scripts)."""
from pathlib import Path
from typing import Callable, Optional

from ..native import apply as native_apply, extract as native_extract, router as native_router
from ..native.problem import NativeOptions, SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS


def native_options_from(spec_options: dict, base: Optional[NativeOptions] = None) -> NativeOptions:
    """Turn the assistant's option dict (mm, plain names) into NativeOptions."""
    o = base or NativeOptions()
    m = {"pitch_mm": "pitch_mm", "via_cost": "via_cost", "neck_down": "neck_down", "fanout_vias": "fanout_vias",
         "stitch_vias": "stitch_vias", "stitch_pitch_mm": "stitch_pitch_mm", "diff_pairs": "diff_pairs",
         "allow_vias": "allow_vias", "routable_layers": "routable_layers", "horizontal_layers": "horizontal_layers",
         "power_pours": "power_pours", "rip_up": "rip_up", "max_expansions": "max_expansions",
         "track_mode": "track_mode", "move_existing": "shove", "plane_nets": "plane_nets",
         "route_order": "route_order", "tune_lengths": "tune_lengths", "protect_groups": "protect_groups"}
    for key, attr in m.items():
        if key in spec_options and spec_options[key] is not None:
            setattr(o, attr, spec_options[key])
    if "skip_nets" in spec_options:
        o.skip_nets = list(spec_options["skip_nets"] or [])
    if "time_limit_min" in spec_options and spec_options["time_limit_min"] is not None:
        o.time_limit_s = float(spec_options["time_limit_min"]) * 60.0
    if "track_width_mm" in spec_options and spec_options["track_width_mm"]:
        o.track_width = int(round(float(spec_options["track_width_mm"]) * 1e6))
    return o


def apply_hints(o: NativeOptions, spec: dict, layer_names: Optional[list] = None) -> NativeOptions:
    """Per-route hints from the assistant: allowed layers, a fixed width, a
    via cost, and rectangles to keep clear (mm)."""
    hints = spec.get("hints") or {}
    if hints.get("layers") and layer_names:
        wanted = {n.lower() for n in hints["layers"]}
        o.routable_layers = [name.lower() in wanted for name in layer_names]
    if hints.get("width_mm"):
        o.track_mode, o.track_width = "fixed", int(round(float(hints["width_mm"]) * 1e6))
    if hints.get("via_cost") is not None:
        o.via_cost = int(hints["via_cost"])
    if hints.get("avoid_mm"):
        o.keepouts = [tuple(int(round(float(v) * 1e6)) for v in rect[:4]) for rect in hints["avoid_mm"] if len(rect) >= 4]
    return o


def apply_scope(o: NativeOptions, spec: dict, selection: Optional[dict] = None) -> NativeOptions:
    scope = spec.get("scope", "board")
    if scope == "nets":
        o.scope, o.scope_nets, o.scope_refs = SCOPE_SELECTION, list(spec.get("nets") or []), []
    elif scope == "sheets":
        o.scope, o.scope_sheets = SCOPE_SHEETS, list(spec.get("sheets") or [])
    elif scope == "selection" and selection:
        o.scope, o.scope_refs, o.scope_nets = SCOPE_SELECTION, list(selection.get("refs", [])), list(selection.get("nets", []))
    else:
        o.scope = SCOPE_BOARD
    return o


def make_route_fn(board, base: Optional[NativeOptions] = None, on_path: Optional[Callable] = None,
                  log: Optional[Callable[[str], None]] = None, selection: Optional[dict] = None):
    """route_fn(spec) -> outcome dict, routing straight onto the given board."""
    def route_fn(spec: dict) -> dict:
        o = apply_scope(native_options_from(spec.get("options", {}), base), spec, selection)
        o = apply_hints(o, spec, [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()])
        if spec.get("options", {}).get("remove_existing"):
            from .. import session
            session.remove_tracks(board)
        from ..native import pipeline
        try:
            job = pipeline.run_job(board, o, on_path=on_path, apply_paths=on_path is None,
                                   escalate=spec.get("options", {}).get("escalate", True))
        except Exception as e:
            return {"error": f"could not read the board: {e}"}
        from .. import session
        try:
            session.refill_zones(board)
        except Exception:
            pass
        try:
            board.BuildConnectivity()             # the count below is stale otherwise
        except Exception:
            pass
        outcome = {"error": None, "scope": spec.get("scope", "board"),
                   "connections_in_scope": job.stats.get("connections"),
                   "routed": job.routed, "failed": job.failed, "ripped_up": job.ripped,
                   "fanout_vias": job.fanout, "coupled_pairs": job.pairs, "stitching_vias": job.stitched,
                   "escalated": job.escalated, "passes": job.passes, "seconds": round(job.seconds, 1),
                   "timed_out": job.timed_out,
                   "pair_log": [list(x) for x in job.pair_log],
                   "failed_detail": job.failed_detail,
                   "length_matching": job.tuning_lines,
                   "unconnected_after": board.GetConnectivity().GetUnconnectedCount(True)}
        outcome["summary"] = (f"{job.routed} routed ({job.escalated} on the finer second pass), "
                              f"{len(job.failed)} failed, {job.fanout} fanout vias, "
                              f"{job.stitched} stitching vias; {outcome['unconnected_after']} unconnected left")
        if log:
            log("[ai] " + outcome["summary"])
        return outcome
    return route_fn
