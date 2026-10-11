"""The built-in router end to end, without the GUI: read a board through
pcbnew, route every connection, put the copper on the board, save it and run
KiCad's DRC on the result. Needs KiCad's Python and kicad-cli; skips otherwise.

  <KiCad python3> tests/test_native_e2e.py [board.kicad_pcb] [--skip GND,VCC] [--pitch 0.1] [--out dir]
                                           [--sheet /Power/] [--refs R1,U2] [--track current|<nm>] [--via current]
"""
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_native_e2e: skipped (needs KiCad's Python)")
    sys.exit(0)

from autorouter.engine.native import apply, extract, router
from autorouter.engine.native.problem import (NativeOptions, SCOPE_SELECTION, SCOPE_SHEETS, SIZE_CURRENT,
                                              SIZE_FIXED)

HERE = Path(__file__).resolve().parent
print = functools.partial(print, flush=True)   # progress must show through pipes


def kicad_cli():
    for cand in (shutil.which("kicad-cli"), "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"):
        if cand and os.path.isfile(cand):
            return cand
    return None


def main(argv):
    args = argv[1:]
    board_file = HERE / "fixtures" / "obd2-adapter.kicad_pcb"
    skip, pitch, out = [], 0.1, None
    options = NativeOptions()
    while args:
        a = args.pop(0)
        if a == "--skip":
            skip = [s for s in args.pop(0).split(",") if s]
        elif a == "--pitch":
            pitch = float(args.pop(0))
        elif a == "--out":
            out = Path(args.pop(0))
        elif a == "--sheet":
            options.scope, options.scope_sheets = SCOPE_SHEETS, args.pop(0).split(",")
        elif a == "--refs":
            options.scope, options.scope_refs = SCOPE_SELECTION, args.pop(0).split(",")
        elif a == "--track":
            v = args.pop(0)
            options.track_mode = SIZE_CURRENT if v == "current" else ("auto" if v == "auto" else SIZE_FIXED)
            options.track_width = int(v) if v not in ("current", "auto") else 0
        elif a == "--turn":
            options.turn_cost = int(args.pop(0))
        elif a == "--via-cost":
            options.via_cost = int(args.pop(0))
        elif a == "--via":
            args.pop(0)                      # only "current" is supported here
            options.via_mode = SIZE_CURRENT
        else:
            board_file = Path(a)
    out = out or Path(tempfile.mkdtemp(prefix="native-e2e-"))
    out.mkdir(parents=True, exist_ok=True)

    board = pcbnew.LoadBoard(str(board_file))
    before = board.GetConnectivity().GetUnconnectedCount(True)
    options.pitch_mm, options.skip_nets = pitch, skip
    t0 = time.time()
    print("sizes:", extract.board_sizes(board))
    print("sheets:", extract.sheets(board))
    problem = extract.extract(board, options, status=lambda s: print("  ", s))
    print(f"extract: {problem.stats}")
    if options.scope != "board":
        assert problem.stats["connections"] < problem.stats["all_connections"], "scope did not narrow the work"
        for c in problem.connections:
            net = problem.nets[c.net]
            refs = net.clusters[c.a].refs | net.clusters[c.b].refs
            if options.scope == SCOPE_SELECTION:
                assert refs & set(options.scope_refs) or net.name in options.scope_nets, (net.name, refs)
    if options.track_mode == SIZE_FIXED:
        assert all(n.rules.width == max(options.track_width, extract.board_sizes(board)["min_track_width"])
                   for n in problem.nets.values())
    t1 = time.time()

    def progress(done, total, routed, failed, name):
        if done % 25 == 0 or done == total:
            print(f"  {done}/{total} routed={routed} failed={failed} ({time.time() - t1:.0f}s) {name}")

    from autorouter.engine.native import pipeline
    job = pipeline.run_job(board, options, status=lambda s: print("  ", s), progress=progress,
                           escalate="--no-escalate" not in sys.argv, apply_paths=True)
    print(f"job: {job.routed} routed ({job.escalated} on pass 2), open {job.failed}, {job.fanout} fanout/tie vias, "
          f"{job.stitched} stitching vias, {job.seconds:.0f}s")

    class _R:                      # the statistics below read a router result
        pass
    result = _R()
    result.routed, result.failed, result.ripped, result.fanout, result.pairs, result.stitched = (
        job.routed, [], job.ripped, job.fanout, job.pairs, job.stitched)
    result.seconds, result.pair_log, result.paths = job.seconds, job.pair_log, []
    print(f"route: {result.routed} routed, {len(result.failed)} failed, {result.ripped} ripped up, "
          f"{result.fanout} fanout vias, {result.pairs} coupled pairs, {result.stitched} stitching vias "
          f"in {result.seconds:.1f}s")
    pairs = {n.name: problem.nets[n.partner].name for n in problem.nets.values() if n.partner}
    print("pairs:", pairs, "paired connections:", sum(1 for c in problem.connections if c.pair),
          "pair log:", getattr(result, "pair_log", []))
    import math
    from collections import Counter
    segs = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")
    length = sum(t.GetLength() for t in segs) / 1e6
    print(f"artwork: {len(segs)} segments, {vias} vias, {length:.0f} mm, "
          f"layers {dict(Counter(board.GetLayerName(t.GetLayer()) for t in segs))}")
    for name in job.failed[:20]:
        print("   failed:", name)
    items = len(segs) + vias
    from autorouter.engine import session
    print(f"refilled {session.refill_zones(board)} zones")
    target = out / board_file.name
    for sidecar in (".kicad_pro", ".kicad_prl"):
        src = board_file.with_suffix(sidecar)
        if src.exists():
            shutil.copy(src, target.with_suffix(sidecar))
    pcbnew.SaveBoard(str(target), board)
    reloaded = pcbnew.LoadBoard(str(target))
    after = reloaded.GetConnectivity().GetUnconnectedCount(True)
    # reading the routed board again (now with tracks and vias as obstacles)
    # must work too: a second, incremental run starts from here
    again = extract.extract(reloaded, options)
    print(f"re-extract on the routed board: {again.stats['connections']} connections left in scope")
    print(f"board: {items} items added, unconnected {before} -> {after}, saved {target}")

    cli = kicad_cli()
    summary = {"routed": result.routed, "failed": len(result.failed), "items": items,
               "unconnected_before": before, "unconnected_after": after,
               "extract_seconds": problem.stats["extract_seconds"], "route_seconds": result.seconds}
    if cli:
        report = out / "drc.json"
        subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-error", "--exit-code-violations",
                        "-o", str(report), str(target)], capture_output=True, text=True)
        if report.exists():
            drc = json.loads(report.read_text())
            kinds = {}
            for v in drc.get("violations", []):
                kinds[v.get("type")] = kinds.get(v.get("type"), 0) + 1
            summary["drc_violations"] = len(drc.get("violations", []))
            summary["drc_by_type"] = kinds
            summary["drc_unconnected"] = len(drc.get("unconnected_items", []))
            print(f"drc: {summary['drc_violations']} violations {kinds}, "
                  f"{summary['drc_unconnected']} unconnected items")
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    assert job.routed + job.fanout + job.stitched > 0, "nothing was routed"
    assert after < before, "the board is not better connected than before"
    print("test_native_e2e: ok")


if __name__ == "__main__":
    main(sys.argv)
