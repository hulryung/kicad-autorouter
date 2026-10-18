"""One benchmark run for the improvement loop (run with KiCad's Python).

Copies a finished reference board, strips every track and via, routes it
with the plugin code under --plugins, and writes into --out:

  routed.kicad_pcb   the result (the reference file is never written)
  metrics.json       connectivity, DRC by type, artwork figures, comparison with the reference
  images/            the routed board and the reference side by side: full view and close-ups

  <KiCad python3> scripts/improve_bench.py --reference board.kicad_pcb --plugins <repo>/plugins
                                           --out dir [--skip GND,GND1] [--shove] [--pitch 0.1]
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


def kicad_cli():
    for cand in (shutil.which("kicad-cli"), "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
                 r"C:\Program Files\KiCad\bin\kicad-cli.exe"):
        if cand and os.path.isfile(cand):
            return cand
    return None


# ---- artwork measures ---------------------------------------------------------

def octilinear_off(tracks):
    """Segments (longer than 1 µm) not at a multiple of 45 degrees."""
    out = []
    for t in tracks:
        d = t.GetEnd() - t.GetStart()
        if math.hypot(d.x, d.y) < 1000:
            continue
        a = math.degrees(math.atan2(d.y, d.x)) % 45
        if min(a, 45 - a) > 0.5:
            out.append(t)
    return out


def doubled_back(tracks):
    """Same-net, same-layer segments running over each other (a route that
    came back over its own stub)."""
    out = []
    by_key = defaultdict(list)
    for t in tracks:
        by_key[(t.GetNetCode(), t.GetLayer())].append(t)
    for group in by_key.values():
        for i, a in enumerate(group):
            ax, ay = a.GetStart().x, a.GetStart().y
            adx, ady = a.GetEnd().x - ax, a.GetEnd().y - ay
            la = math.hypot(adx, ady)
            if la < 1:
                continue
            for b in group[i + 1:]:
                bx, by = b.GetStart().x, b.GetStart().y
                bdx, bdy = b.GetEnd().x - bx, b.GetEnd().y - by
                if math.hypot(bdx, bdy) < 1 or abs(adx * bdy - ady * bdx) > 1000 * la:
                    continue
                if abs((bx - ax) * ady - (by - ay) * adx) > 1000 * la:
                    continue
                t0 = ((bx - ax) * adx + (by - ay) * ady) / la
                t1 = ((b.GetEnd().x - ax) * adx + (b.GetEnd().y - ay) * ady) / la
                if min(la, max(t0, t1)) - max(0, min(t0, t1)) > max(a.GetWidth(), b.GetWidth()):
                    out.append((a.GetNetname(), ax / 1e6, ay / 1e6))
    return out


def acute_corners(tracks, pads=()):
    import pcbnew
    """Corners sharper than 90 degrees: two segments of one net and layer
    meeting end to end (and nothing else there) that turn back by more than
    90 degrees, like a pad stub going up and the route coming back down."""
    ends = defaultdict(list)
    for t in tracks:
        for p, q in ((t.GetStart(), t.GetEnd()), (t.GetEnd(), t.GetStart())):
            ends[(t.GetNetCode(), t.GetLayer(), p.x, p.y)].append((q.x - p.x, q.y - p.y, t))
    out = []
    for (code, layer, x, y), legs in ends.items():
        if len(legs) != 2:
            continue
        (ax, ay, a), (bx, by, b) = legs
        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
        if la < 1 or lb < 1:
            continue
        cos = (ax * bx + ay * by) / (la * lb)         # angle between the two legs at the corner
        if 0.05 < cos < 0.999:                          # under 87 degrees, not a straight overlap
            if any(p.GetNetCode() == code and p.HitTest(pcbnew.VECTOR2I(x, y)) for p in pads):
                continue                                # hidden in pad copper
            out.append((a.GetNetname(), x / 1e6, y / 1e6))
    return out


def per_net(board):
    """Track length, vias and layers per net."""
    nets = defaultdict(lambda: {"length_mm": 0.0, "vias": 0, "segments": 0})
    for t in board.GetTracks():
        d = nets[t.GetNetname()]
        if t.GetClass() == "PCB_VIA":
            d["vias"] += 1
        else:
            d["length_mm"] += t.GetLength() / 1e6
            d["segments"] += 1
    return nets


def escape_sides(board, min_pads=8):
    """For the pins of the larger parts: does the track leave the pad towards
    the part's centre (IN, under the body) or away from it (OUT)?"""
    import pcbnew
    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    out = {}
    for fp in board.GetFootprints():
        pads = [p for p in fp.Pads() if p.GetNetCode() > 0]
        if len(pads) < min_pads:
            continue
        c = fp.GetPosition()
        for p in pads:
            if p.GetNetname().startswith("unconnected"):
                continue
            pos = p.GetPosition()
            side = None
            for t in tracks:
                if t.GetNetCode() != p.GetNetCode() or t.GetLayer() != p.GetPrincipalLayer():
                    continue
                for a, e in ((t.GetStart(), t.GetEnd()), (t.GetEnd(), t.GetStart())):
                    if abs(a.x - pos.x) < 50_000 and abs(a.y - pos.y) < 50_000 and (e.x, e.y) != (a.x, a.y):
                        v = (e.x - a.x, e.y - a.y)
                        side = "IN" if v[0] * (c.x - pos.x) + v[1] * (c.y - pos.y) > 0 else "OUT"
                        break
                if side:
                    break
            out[f"{fp.GetReference()}.{p.GetNumber()} {p.GetNetname()}"] = side
    return out


INTENT_RULES = {"diff_pair_gap_out_of_range", "diff_pair_uncoupled_length_too_long", "length_out_of_range",
                "skew_out_of_range", "track_width"}


def intent_metrics(board, o, job, si_plan) -> dict:
    """How well the routed board meets the design intent: impedance widths
    per layer, pair coupling and skew, length groups, and completion per
    routing group (with the order they were routed in)."""
    import math
    from autorouter.engine.native.extract import pair_partner
    names = {c: n.GetNetname() for c, n in board.GetNetsByNetcode().items()}
    by_net = {}
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK" and t.GetNetCode() > 0:
            by_net.setdefault(names[t.GetNetCode()], []).append(t)
    pads_of = {}
    for p in board.GetPads():
        pads_of.setdefault(names.get(p.GetNetCode(), ""), []).append(p)
    # 1. widths of the controlled nets, by length; a pad stub (a segment
    # touching a pad) may be necked down and does not count
    ok_len = all_len = 0.0
    wrong = []
    for name, z in o.impedance.items():
        for t in by_net.get(name, []):
            want = z["widths"].get(t.GetLayer())
            if want is None:
                continue
            on_pad = any(p.HitTest(t.GetStart()) or p.HitTest(t.GetEnd()) for p in pads_of.get(name, []))
            if on_pad and t.GetLength() < 1.5e6:
                continue
            all_len += t.GetLength()
            if abs(t.GetWidth() - want) <= 5000:
                ok_len += t.GetLength()
            else:
                wrong.append({"net": name, "layer": board.GetLayerName(t.GetLayer()), "width_mm": t.GetWidth() / 1e6,
                              "want_mm": want / 1e6, "x": t.GetStart().x / 1e6, "y": t.GetStart().y / 1e6,
                              "what": f"{name} {t.GetWidth() / 1e6:.3f} mm, want {want / 1e6:.3f}"})
    # 2. pairs: share of each half's length running at the pair spacing beside the other half
    pairs = []
    from autorouter.engine.native import tuning as _t
    kicad_len_all = {names[c]: v for c, v in _t.track_lengths(board).items()}
    seen = set()
    routed_names = set(by_net)
    for name in sorted(routed_names):
        other = pair_partner(name, routed_names)
        if not other or name in seen:
            continue
        seen |= {name, other}
        a, b = by_net[name], by_net[other]
        z = o.impedance.get(name)
        gap = None
        if z and z.get("gaps"):
            gap = z["gaps"].get(a[0].GetLayer())
        coupled = total = 0.0
        for t in a:
            s, e = t.GetStart(), t.GetEnd()
            n = max(1, int(t.GetLength() / 100_000))
            for k in range(n):
                x = s.x + (e.x - s.x) * (k + 0.5) / n
                y = s.y + (e.y - s.y) * (k + 0.5) / n
                best = None
                for u in b:
                    if u.GetLayer() != t.GetLayer():
                        continue
                    us, ue = u.GetStart(), u.GetEnd()
                    dx, dy = ue.x - us.x, ue.y - us.y
                    ll = dx * dx + dy * dy
                    q = 0.0 if ll == 0 else max(0.0, min(1.0, ((x - us.x) * dx + (y - us.y) * dy) / ll))
                    d = math.hypot(x - (us.x + q * dx), y - (us.y + q * dy))
                    best = d if best is None or d < best else best
                total += t.GetLength() / n
                pitch = (t.GetWidth() + (gap or 0)) if gap else None
                if best is not None and (abs(best - pitch) < 30_000 if pitch else best < 3 * t.GetWidth()):
                    coupled += t.GetLength() / n
        la = kicad_len_all.get(name, sum(t.GetLength() for t in a))
        lb = kicad_len_all.get(other, sum(t.GetLength() for t in b))
        exc = next((e for e in job.pair_exceptions if name in e["nets"]), None)
        pairs.append({"pair": f"{name} / {other}", "coupled_pct": round(100 * coupled / total, 1) if total else 0.0,
                      "skew_mm": round(abs(la - lb) / 1e6, 3), "length_mm": round(max(la, lb) / 1e6, 2),
                      # connected directly on purpose (coupling impossible or unnecessary), and why
                      "exception": exc["text"] if exc else None,
                      "exception_waives_skew": any(e.get("skew_waived") for e in job.pair_exceptions
                                                   if name in e["nets"])})
    # 3. length groups after tuning
    groups = []
    from autorouter.engine.native import tuning
    kicad_len = {names[c]: v for c, v in tuning.track_lengths(board).items()}      # as KiCad measures (vias too)
    tuned = {g["name"]: g for g in job.tuning}
    waived = {n for e in job.pair_exceptions if e.get("skew_waived") for n in e["nets"]}
    for g in o.length_groups:
        if g.get("pair") and set(g["nets"]) & waived:
            groups.append({"name": g["name"], "pair": True, "waived": "pair connected directly: coupling and "
                           "skew not needed", "ok": True, "off_by_mm": {}, "where": None})
            continue
        lens = {n: kicad_len.get(n, 0) for n in g["nets"]}
        routed = {n: v for n, v in lens.items() if v > 0}
        if not routed:
            continue
        target = g["target"] or max(routed.values())
        tol = max(g["tolerance"], 10_000)
        off = {n: round((v - target) / 1e6, 3) for n, v in routed.items() if abs(v - target) > tol}
        where = None
        if off:
            t = max(by_net[next(iter(off))], key=lambda t: t.GetLength())
            where = (t.GetStart().x / 1e6, t.GetStart().y / 1e6)
        before = tuned.get(g["name"], {}).get("spread_before")
        groups.append({"name": g["name"], "pair": bool(g.get("pair")), "target_mm": round(target / 1e6, 3),
                       "tolerance_mm": round(tol / 1e6, 3),
                       "spread_before_mm": round(before / 1e6, 3) if before is not None else None,
                       "spread_after_mm": round((max(routed.values()) - min(routed.values())) / 1e6, 3),
                       "ok": not off, "off_by_mm": off, "where": where})
    # 4. routing groups: how many nets each had, which stayed open
    open_by_group = {}
    group_of = job.stats.get("net_groups", {})
    for n in job.failed:
        open_by_group.setdefault(group_of.get(n, "?"), []).append(n)
    return {
        "stackup": si_plan.source if si_plan else None,
        "plan": si_plan.lines if si_plan else [],
        "warnings": si_plan.warnings if si_plan else [],
        "impedance_width_ok_pct": round(100 * ok_len / all_len, 1) if all_len else None,
        "wrong_width": wrong[:10],
        "pairs": pairs,
        "length_groups": groups,
        "length_groups_ok": f"{sum(1 for g in groups if g['ok'])} of {len(groups)}",
        "route_order": list(o.route_order),
        "nets_per_group": job.stats.get("groups", {}),
        "open_by_group": open_by_group,
        "tuning_log": job.tuning_lines,
        "pairs_routed_coupled": job.pairs,
        "pair_via_swaps": getattr(job, "pair_swaps", 0),
        "pair_exceptions": [f"{e['nets'][0]} / {e['nets'][1]}: {e['text']}" for e in job.pair_exceptions],
        "kicad_tuning_patterns": sum(1 for _ in board.Generators()),
        "pair_log": [f"{n}: {why}" for n, why in job.pair_log],
    }


def drc(board_path: Path, report: Path):
    cli = kicad_cli()
    if not cli:
        return None
    subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "--refill-zones",
                    "-o", str(report), str(board_path)], capture_output=True, timeout=600)
    try:
        return json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- images -------------------------------------------------------------------

def _svg_to_png(svg: Path, png: Path, size: int) -> bool:
    if sys.platform == "darwin":
        subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(png.parent), str(svg)],
                       capture_output=True, timeout=120)
        made = png.parent / (svg.name + ".png")
        if made.exists():
            made.replace(png)
            return True
    exe = shutil.which("rsvg-convert")
    if exe:
        subprocess.run([exe, "-w", str(size), "-o", str(png), str(svg)], capture_output=True, timeout=120)
        return png.exists()
    return False


class Renderer:
    """kicad-cli SVG of a board with its pours unfilled (so tracks show), then
    PNG crops by editing the SVG viewBox. Same crops for routed and reference."""

    LAYERS = "F.Cu,In1.Cu,In2.Cu,B.Cu,F.Fab,Edge.Cuts"

    def __init__(self, board_path: Path, name: str, workdir: Path):
        import pcbnew
        self.name, self.workdir = name, workdir
        board = pcbnew.LoadBoard(str(board_path))
        edges = [d.GetBoundingBox() for d in board.GetDrawings() if d.GetLayer() == pcbnew.Edge_Cuts]
        self.left = min(e.GetLeft() for e in edges) / 1e6
        self.top = min(e.GetTop() for e in edges) / 1e6
        self.width = max(e.GetRight() for e in edges) / 1e6 - self.left
        self.height = max(e.GetBottom() for e in edges) / 1e6 - self.top
        for zone in board.Zones():
            if not zone.GetIsRuleArea():
                zone.UnFill()
        copy = workdir / f"{name}-nozones.kicad_pcb"
        pcbnew.SaveBoard(str(copy), board)
        self.svg = workdir / f"{name}.svg"
        subprocess.run([kicad_cli(), "pcb", "export", "svg", "--layers", self.LAYERS, "--exclude-drawing-sheet",
                        "--page-size-mode", "2", "--mode-single", "-o", str(self.svg), str(copy)],
                       capture_output=True, timeout=300)
        self.text = self.svg.read_text(encoding="utf-8") if self.svg.exists() else ""
        m = re.search(r'width="([\d.]+)mm" height="([\d.]+)mm"', self.text)
        self.page = (float(m.group(1)), float(m.group(2))) if m else (self.width, self.height)
        # the page is centred on the board outline
        self.ox = self.left - (self.page[0] - self.width) / 2
        self.oy = self.top - (self.page[1] - self.height) / 2

    def crop(self, label: str, cx: float, cy: float, half: float, size: int = 1000):
        if not self.text:
            return None
        vx, vy = cx - self.ox - half, cy - self.oy - half
        txt = re.sub(r'width="[\d.]+mm" height="[\d.]+mm" viewBox="[^"]+"',
                     f'width="{size}px" height="{size}px" viewBox="{vx:.3f} {vy:.3f} {2 * half:.3f} {2 * half:.3f}"',
                     self.text, count=1)
        svg = self.workdir / f"{self.name}-{label}.svg"
        svg.write_text(txt, encoding="utf-8")
        png = self.workdir / f"{self.name}-{label}.png"
        ok = _svg_to_png(svg, png, size)
        svg.unlink(missing_ok=True)
        return png if ok else None

    def full(self, size: int = 1800):
        half = max(self.width, self.height) / 2 + 1
        return self.crop("full", self.left + self.width / 2, self.top + self.height / 2, half, size)


# ---- the run ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--plugins", required=True, help="the plugins/ folder whose router code is measured")
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip", default="GND,GND1,GND2")
    ap.add_argument("--pitch", type=float, default=0.1)
    ap.add_argument("--shove", action="store_true")
    ap.add_argument("--time-limit", type=float, default=0.0,
                    help="job time limit in minutes, as the dialog sets it (default: none, same input same result)")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--focus", default="", help="extra close-ups: x,y[,half];x,y ... in mm")
    ap.add_argument("--intent", default="", help="design intent JSON (engine/si.py SiConfig): stackup, "
                    "impedance classes, length groups, routing order; also written as net classes and DRC rules")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(args.plugins).resolve()))
    import pcbnew
    from autorouter.engine import session
    from autorouter.engine.native import pipeline
    from autorouter.engine.native.problem import NativeOptions

    ref_path = Path(args.reference).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    routed_path = out / "routed.kicad_pcb"
    assert routed_path != ref_path

    # the board's project files travel along (net classes and DRC rules live there)
    for ext in (".kicad_pro", ".kicad_dru"):
        side = ref_path.with_suffix(ext)
        if side.exists():
            shutil.copy(side, out / ("routed" + ext))

    board = pcbnew.LoadBoard(str(ref_path))
    items = list(board.GetTracks())
    for t in items:
        board.Remove(t)
        t.thisown = 0
    session._REMOVED.extend(items)
    board.BuildConnectivity()
    before = board.GetConnectivity().GetUnconnectedCount(True)

    o = NativeOptions(pitch_mm=args.pitch)
    o.skip_nets = [s for s in args.skip.split(",") if s]
    from autorouter.engine.native.extract import ground_plane_layers
    planes = ground_plane_layers(board)
    if planes:
        # what the dialog does by default: no tracks on an inner ground plane
        n = len(list(board.GetEnabledLayers().CuStack()))
        o.routable_layers = [i not in planes for i in range(n)]
        print(f"  ground plane layers left unrouted: {planes}", flush=True)
    si_cfg = si_plan = None
    if args.intent:
        from autorouter.engine import si
        si_cfg = si.SiConfig.from_dict(json.loads(Path(args.intent).read_text(encoding="utf-8")))
        if si_cfg.fab:
            from autorouter.engine import fab
            for line in fab.apply(board, si_cfg.fab):
                print(f"  [fab] {line}", flush=True)
        si_plan = si.plan(board, si_cfg)
        o = si.apply_to_options(o, si_cfg, si_plan)
        for line in si_plan.lines + [f"warning: {w}" for w in si_plan.warnings]:
            print("  [intent] " + line, flush=True)
        # KiCad gets the same intent: net classes on the board, rules in the copy's .kicad_dru
        for line in si.apply_net_classes(board, si_cfg, si_plan):
            print("  [intent] " + line, flush=True)
    o.shove = args.shove
    for kv in [x for x in os.environ.get("AUTOROUTER_BENCH_SET", "").split(",") if x]:
        k, v = kv.split("=")
        setattr(o, k, json.loads(v))          # quick experiments: AUTOROUTER_BENCH_SET=protect_groups=false
    if args.time_limit > 0:
        o.time_limit_s = args.time_limit * 60.0     # the dialog's limits: the job's, the second pass's, each connection's
    else:
        o.time_limit_s = 0         # same input, same result: no wall-clock cut-offs in a benchmark
        o.fine_budget_s = 0
        o.conn_time_s = 0
    stages = []
    t0 = time.time()

    def status(text):
        stages.append((round(time.time() - t0, 1), text))
        print(f"  [{time.time() - t0:5.0f}s] {text}", flush=True)

    last = [0.0]

    def progress(done, total, routed, failed, name):
        if time.time() - last[0] >= 10:
            last[0] = time.time()
            print(f"  [{time.time() - t0:5.0f}s] {done}/{total} routed {routed} failed {failed}  {name}", flush=True)

    job = pipeline.run_job(board, o, status=status, progress=progress, escalate=True)
    seconds = time.time() - t0
    session.refill_zones(board)
    board.BuildConnectivity()
    pcbnew.SaveBoard(str(routed_path), board)
    if si_cfg is not None:
        from autorouter.engine import si
        from autorouter.engine.native import tuning
        names = {c: n.GetNetname() for c, n in board.GetNetsByNetcode().items()}
        lengths = {names[c]: l for c, l in tuning.track_lengths(board).items()}
        saved = pcbnew.LoadBoard(str(routed_path))     # the rules file goes next to the routed copy
        si.write_drc_rules(saved, si.drc_rules(board, si_cfg, si_plan, lengths, job.pair_exceptions))
        if si_cfg.fab:
            from autorouter.engine import fab
            fab.write_drc_rules(saved, si_cfg.fab)

    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    ref = pcbnew.LoadBoard(str(ref_path))
    ref_tracks = [t for t in ref.GetTracks() if t.GetClass() == "PCB_TRACK"]

    print(f"  [{time.time() - t0:5.0f}s] saved; DRC...", flush=True)
    report = drc(routed_path, out / "drc.json")
    print(f"  [{time.time() - t0:5.0f}s] DRC done; measuring...", flush=True)
    violations = Counter()
    issues = []                     # positions worth a close-up
    unconnected = None
    if report is not None:
        for v in report.get("violations", []):
            if v["type"] == "lib_footprint_issues" or v["type"].startswith("lib_"):
                continue
            violations[(v["type"], v["severity"])] += 1
            if v.get("items"):
                p = v["items"][0]["pos"]
                issues.append({"kind": v["type"], "x": p["x"], "y": p["y"], "what": v["items"][0]["description"]})
        unconnected = len(report.get("unconnected_items", []))
        for u in report.get("unconnected_items", []):
            it = u["items"]
            issues.append({"kind": "unconnected", "x": it[0]["pos"]["x"], "y": it[0]["pos"]["y"],
                           "what": " <-> ".join(i["description"] for i in it)})

    # comparison with the reference artwork
    mine, theirs = per_net(board), per_net(ref)
    longer = []
    for name, d in mine.items():
        r = theirs.get(name)
        if r and r["length_mm"] > 1 and d["length_mm"] > r["length_mm"] * 1.3 + 1:
            longer.append({"net": name, "length_mm": round(d["length_mm"], 1), "reference_mm": round(r["length_mm"], 1),
                           "vias": d["vias"], "reference_vias": r["vias"]})
    longer.sort(key=lambda r: -(r["length_mm"] - r["reference_mm"]))
    sides_mine, sides_ref = escape_sides(board), escape_sides(ref)
    compared = {k: (sides_mine.get(k), v) for k, v in sides_ref.items() if v}
    disagree = {k: {"routed": a, "reference": b} for k, (a, b) in compared.items() if a and a != b}
    off45 = octilinear_off(tracks)
    doubled = doubled_back(tracks)
    acute = acute_corners(tracks, list(board.GetPads()))
    for net, x, y in acute[:5]:
        issues.append({"kind": "acute_corner", "x": x, "y": y, "what": net})
    for t in off45[:5]:
        issues.append({"kind": "off_45", "x": t.GetStart().x / 1e6, "y": t.GetStart().y / 1e6, "what": t.GetNetname()})

    intent = intent_metrics(board, o, job, si_plan) if si_cfg is not None else None
    if intent:
        for w in intent.get("wrong_width", [])[:5]:
            issues.append({"kind": "impedance_width", "x": w["x"], "y": w["y"], "what": w["what"]})
        for g in intent.get("length_groups", []):
            if not g["ok"] and g.get("where"):
                issues.append({"kind": "length_mismatch", "x": g["where"][0], "y": g["where"][1], "what": g["name"]})
    metrics = {
        "board": ref_path.name,
        "intent": intent,
        "connections_before": before,
        "routed": job.routed, "open_nets": job.failed, "escalated": job.escalated,
        "fanout_vias": job.fanout, "stitching_vias": job.stitched,
        "pass_errors": {k: v for k, v in job.stats.items() if k.endswith("_error")},
        "route_seconds": round(seconds, 1),
        "stages": stages,
        "unconnected_after": unconnected,
        "drc": {f"{t}/{s}": n for (t, s), n in sorted(violations.items())},
        # physical errors (clearance, shorts...) apart from the design-intent rules (width, gap, skew, length)
        "drc_errors": sum(n for (t, s), n in violations.items() if s == "error" and t not in INTENT_RULES),
        "rule_errors": sum(n for (t, s), n in violations.items() if s == "error" and t in INTENT_RULES),
        "dangling": violations.get(("track_dangling", "warning"), 0),
        "segments": len(tracks), "vias": len(vias),
        "length_mm": round(sum(t.GetLength() for t in tracks) / 1e6, 1),
        "off_45": len(off45), "doubled_back": len(doubled), "acute_corners": len(acute),
        "reference": {"segments": len(ref_tracks), "vias": len([t for t in ref.GetTracks() if t.GetClass() == "PCB_VIA"]),
                      "length_mm": round(sum(t.GetLength() for t in ref_tracks) / 1e6, 1)},
        "escape_side_agreement": f"{len(compared) - len(disagree)} of {len(compared)}",
        "escape_side_disagreements": disagree,
        "nets_much_longer_than_reference": longer[:15],
        "issues": issues[:30],
    }

    print(f"  [{time.time() - t0:5.0f}s] measured; images...", flush=True)
    images = []
    if not args.no_images and kicad_cli():
        imgdir = out / "images"
        imgdir.mkdir(exist_ok=True)
        r_mine = Renderer(routed_path, "routed", imgdir)
        r_ref = Renderer(ref_path, "reference", imgdir)
        for r in (r_mine, r_ref):
            p = r.full()
            if p:
                images.append({"what": f"{r.name}: whole board", "path": str(p)})
        # close-ups: the issues first, then the largest parts, then any asked for
        spots = [(i["x"], i["y"], 3.0, f"{i['kind']}: {i['what'][:80]}") for i in issues]
        fps = sorted(board.GetFootprints(), key=lambda f: -len(list(f.Pads())))[:2]
        for fp in fps:
            bb = fp.GetBoundingBox(False)
            spots.append((bb.Centre().x / 1e6, bb.Centre().y / 1e6,
                          max(bb.GetWidth(), bb.GetHeight()) / 2e6 + 2.0, f"around {fp.GetReference()}"))
        asked = []
        for spec in [s for s in args.focus.split(";") if s.strip()]:
            v = [float(x) for x in spec.split(",")]
            asked.append((v[0], v[1], v[2] if len(v) > 2 else 3.0, "requested close-up"))
        spots = asked + spots                      # the close-ups asked for come first
        taken = []
        for x, y, half, what in spots:
            # skip a spot an earlier close-up of at least its size already shows
            if any(h >= half and abs(x - a) < h * 0.6 and abs(y - b) < h * 0.6 for a, b, h in taken):
                continue
            taken.append((x, y, half))
            k = len(taken)
            if k > 8:
                break
            a = r_mine.crop(f"closeup{k}", x, y, half)
            b = r_ref.crop(f"closeup{k}", x, y, half)
            if a:
                images.append({"what": f"routed close-up {k} at ({x:.2f}, {y:.2f}) mm: {what}", "path": str(a)})
            if b:
                images.append({"what": f"reference close-up {k}, same place", "path": str(b)})
    metrics["images"] = images
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1), encoding="utf-8")
    print(json.dumps({k: metrics[k] for k in ("routed", "open_nets", "unconnected_after", "drc_errors", "rule_errors",
                                              "dangling", "acute_corners",
                                              "off_45", "doubled_back", "length_mm", "vias", "route_seconds",
                                              "escape_side_agreement")}), flush=True)
    if intent:
        print(json.dumps({"impedance_width_ok_pct": intent["impedance_width_ok_pct"],
                          "length_groups_ok": intent["length_groups_ok"],
                          "pairs": [(p["pair"], p["coupled_pct"], p["skew_mm"]) for p in intent["pairs"]]}), flush=True)


if __name__ == "__main__":
    main()
