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
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--focus", default="", help="extra close-ups: x,y[,half];x,y ... in mm")
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
    o.shove = args.shove
    o.time_limit_s = 0             # same input, same result: no wall-clock cut-offs in a benchmark
    o.fine_budget_s = 0
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

    tracks = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK"]
    vias = [t for t in board.GetTracks() if t.GetClass() == "PCB_VIA"]
    ref = pcbnew.LoadBoard(str(ref_path))
    ref_tracks = [t for t in ref.GetTracks() if t.GetClass() == "PCB_TRACK"]

    report = drc(routed_path, out / "drc.json")
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
    for t in off45[:5]:
        issues.append({"kind": "off_45", "x": t.GetStart().x / 1e6, "y": t.GetStart().y / 1e6, "what": t.GetNetname()})

    metrics = {
        "board": ref_path.name,
        "connections_before": before,
        "routed": job.routed, "open_nets": job.failed, "escalated": job.escalated,
        "fanout_vias": job.fanout, "stitching_vias": job.stitched,
        "pass_errors": {k: v for k, v in job.stats.items() if k.endswith("_error")},
        "route_seconds": round(seconds, 1),
        "stages": stages,
        "unconnected_after": unconnected,
        "drc": {f"{t}/{s}": n for (t, s), n in sorted(violations.items())},
        "drc_errors": sum(n for (t, s), n in violations.items() if s == "error"),
        "dangling": violations.get(("track_dangling", "warning"), 0),
        "segments": len(tracks), "vias": len(vias),
        "length_mm": round(sum(t.GetLength() for t in tracks) / 1e6, 1),
        "off_45": len(off45), "doubled_back": len(doubled),
        "reference": {"segments": len(ref_tracks), "vias": len([t for t in ref.GetTracks() if t.GetClass() == "PCB_VIA"]),
                      "length_mm": round(sum(t.GetLength() for t in ref_tracks) / 1e6, 1)},
        "escape_side_agreement": f"{len(compared) - len(disagree)} of {len(compared)}",
        "escape_side_disagreements": disagree,
        "nets_much_longer_than_reference": longer[:15],
        "issues": issues[:30],
    }

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
        for spec in [s for s in args.focus.split(";") if s.strip()]:
            v = [float(x) for x in spec.split(",")]
            spots.append((v[0], v[1], v[2] if len(v) > 2 else 3.0, "requested close-up"))
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
    print(json.dumps({k: metrics[k] for k in ("routed", "open_nets", "unconnected_after", "drc_errors", "dangling",
                                              "off_45", "doubled_back", "length_mm", "vias", "route_seconds",
                                              "escape_side_agreement")}), flush=True)


if __name__ == "__main__":
    main()
