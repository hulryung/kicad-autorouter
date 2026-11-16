"""Routing style profile of a board: how its copper is laid out, so a designer's
board and the router's result can be compared figure by figure.

  <KiCad python3> scripts/style.py designer.kicad_pcb [routed.kicad_pcb] [--json out.json]
  <KiCad python3> scripts/style.py designer.kicad_pcb --learn board.style.json   (for improve_bench.py --style)

With two boards, prints both side by side. Measured on tracks only (zones are
counted, not measured), per net class where it matters:

  layers      share of track length per copper layer, and per net class
  direction   per layer: horizontal / vertical / diagonal share of length
  widths      length-weighted widths per net class, against the class width
  angles      0/90 vs 45 vs other segments, arcs, corners per 100 mm
  vias        count, sizes, per routed connection, stitching (no track) share
  pours       copper zones per net: nets carried by local pours instead of tracks
  spacing     gap from a track to the nearest parallel foreign track (median, share at minimum)
  detour      routed length / straight pad-to-pad distance per two-pad net
"""
import argparse
import json
import math
import sys
from collections import Counter, defaultdict

MM = 1e6


def load(path):
    import pcbnew
    return pcbnew.LoadBoard(path)


def _class(board, track):
    try:
        return track.GetNetClassName() or "Default"
    except Exception:
        return board.GetNetInfo().GetNetItem(track.GetNetCode()).GetNetClassName()


def _dir(dx, dy):
    a = math.degrees(math.atan2(abs(dy), abs(dx)))
    if a < 2:
        return "h"
    if a > 88:
        return "v"
    if abs(a - 45) < 2:
        return "d45"
    return "other"


def profile(path, plane_nets=()):
    import pcbnew
    b = load(path)
    tracks, vias, arcs = [], [], 0
    for t in b.GetTracks():
        c = t.GetClass()
        if c == "PCB_VIA":
            vias.append(t)
        else:
            if c == "PCB_ARC":
                arcs += 1
            tracks.append(t)
    names = {i: b.GetLayerName(i) for i in b.GetEnabledLayers().CuStack()}
    total = sum(t.GetLength() for t in tracks) or 1

    layer_len = Counter()
    class_layer = defaultdict(Counter)
    dir_len = defaultdict(Counter)
    width_len = defaultdict(Counter)
    angle = Counter()
    for t in tracks:
        L, ln = names.get(t.GetLayer(), "?"), t.GetLength()
        cls = _class(b, t)
        layer_len[L] += ln
        class_layer[cls][L] += ln
        width_len[cls][round(t.GetWidth() / MM, 3)] += ln
        if t.GetClass() == "PCB_ARC":
            angle["arc"] += 1
            continue
        d = t.GetEnd() - t.GetStart()
        k = _dir(d.x, d.y)
        dir_len[L][k] += ln
        if ln >= 1000:
            angle["orth" if k in ("h", "v") else k] += 1

    ns = b.GetDesignSettings().m_NetSettings
    class_width = {}
    for n, c in ns.GetNetclasses().items():
        class_width[str(n)] = round(c.GetTrackWidth() / MM, 3)
    class_width["Default"] = round(ns.GetDefaultNetclass().GetTrackWidth() / MM, 3)

    # vias: on a net's track end (signal via) or alone in a pour (stitching)
    ends = defaultdict(set)
    for t in tracks:
        for p in (t.GetStart(), t.GetEnd()):
            ends[t.GetNetCode()].add((p.x // 50000, p.y // 50000))
    stitch, sizes, via_class = 0, Counter(), Counter()
    for v in vias:
        p = v.GetPosition()
        r = v.GetWidth(pcbnew.F_Cu) // 2
        key = (p.x // 50000, p.y // 50000)
        near = any((key[0] + i, key[1] + j) in ends[v.GetNetCode()]
                   for i in range(-(r // 50000) - 1, r // 50000 + 2) for j in range(-(r // 50000) - 1, r // 50000 + 2))
        if not near:
            stitch += 1
        sizes[(round(v.GetWidth(pcbnew.F_Cu) / MM, 2), round(v.GetDrillValue() / MM, 2))] += 1
        via_class["plane" if v.GetNetname() in plane_nets else _class(b, v)] += 1

    pours = Counter()
    for z in b.Zones():
        if not z.GetIsRuleArea() and z.GetNetname():
            pours[z.GetNetname()] += 1

    # spacing: for each straight segment, the nearest parallel foreign segment on its layer
    segs = [t for t in tracks if t.GetClass() == "PCB_TRACK" and t.GetLength() >= 1 * MM]
    grid = defaultdict(list)
    cell = 2 * MM
    for i, t in enumerate(segs):
        m = (t.GetStart() + t.GetEnd()) / 2
        grid[(t.GetLayer(), int(m.x // cell), int(m.y // cell))].append(i)
    gaps, at_min = [], 0
    clearance = ns.GetDefaultNetclass().GetClearance()
    for i, t in enumerate(segs):
        s, e = t.GetStart(), t.GetEnd()
        dx, dy = e.x - s.x, e.y - s.y
        ln = math.hypot(dx, dy)
        ux, uy = dx / ln, dy / ln
        m = (s + e) / 2
        best = None
        for gi in range(-1, 2):
            for gj in range(-1, 2):
                for j in grid.get((t.GetLayer(), int(m.x // cell) + gi, int(m.y // cell) + gj), ()):
                    o = segs[j]
                    if o.GetNetCode() == t.GetNetCode():
                        continue
                    os_, oe = o.GetStart(), o.GetEnd()
                    odx, ody = oe.x - os_.x, oe.y - os_.y
                    oln = math.hypot(odx, ody)
                    if abs(ux * ody / oln - uy * odx / oln) > 0.02:      # not parallel
                        continue
                    om = (os_ + oe) / 2
                    along = (om.x - m.x) * ux + (om.y - m.y) * uy
                    if abs(along) > (ln + oln) / 2:                       # no overlap along the run
                        continue
                    perp = abs(-(om.x - m.x) * uy + (om.y - m.y) * ux)
                    g = perp - (t.GetWidth() + o.GetWidth()) / 2
                    if g > 0 and (best is None or g < best):
                        best = g
        if best is not None and best < 2 * MM:
            gaps.append(best / MM)
            if best <= clearance * 1.1:
                at_min += 1

    # detour on two-pad nets with tracks
    pads = defaultdict(list)
    for f in b.GetFootprints():
        for p in f.Pads():
            if p.GetNetCode() > 0:
                pads[p.GetNetCode()].append(p.GetPosition())
    net_len = Counter()
    for t in tracks:
        net_len[t.GetNetCode()] += t.GetLength()
    detours = []
    for code, pp in pads.items():
        if len(pp) == 2 and net_len[code] > 0:
            d = math.hypot(pp[0].x - pp[1].x, pp[0].y - pp[1].y)
            if d > 2 * MM:
                detours.append(net_len[code] / d)

    def shares(c, tot=None):
        tot = tot or sum(c.values()) or 1
        return {k: round(100 * v / tot, 1) for k, v in sorted(c.items(), key=lambda kv: -kv[1])}

    def med(v):
        v = sorted(v)
        return round(v[len(v) // 2], 3) if v else None

    corners = angle["orth"] + angle["d45"] + angle["other"]
    return {
        "board": path.split("/")[-1],
        "track_mm": round(total / MM),
        "layers": shares(layer_len),
        "class_layers": {k: shares(v) for k, v in class_layer.items()},
        "direction": {k: shares(v) for k, v in dir_len.items()},
        "widths": {k: {"class": class_width.get(k), "used": {str(w): s for w, s in list(shares(v).items())[:4]}}
                   for k, v in width_len.items()},
        "segments": {"orth": angle["orth"], "d45": angle["d45"], "other": angle["other"], "arcs": arcs,
                     "per_100mm": round(100 * corners / (total / MM), 1)},
        "vias": {"count": len(vias), "stitching": stitch, "by_class": dict(via_class.most_common()),
                 "sizes": {f"{d}/{h}": n for (d, h), n in sizes.most_common(4)}},
        "pours": {"zones": sum(pours.values()), "nets": len(pours),
                  "top": dict(pours.most_common(8))},
        "spacing": {"median_gap_mm": med(gaps), "at_min_share": round(100 * at_min / len(gaps), 1) if gaps else None,
                    "samples": len(gaps)},
        "detour": {"median": med(detours), "over_1_5": round(100 * sum(d > 1.5 for d in detours) / len(detours), 1)
                   if detours else None, "nets": len(detours)},
    }


def show(profiles):
    def row(label, *vals):
        print(f"  {label:<26}" + "".join(f"{str(v):<44}" for v in vals))
    print("  " + " " * 26 + "".join(f"{p['board']:<44}" for p in profiles))
    row("track length mm", *[p["track_mm"] for p in profiles])
    row("layers %", *[p["layers"] for p in profiles])
    layers = sorted({L for p in profiles for L in p["direction"]})
    for L in layers:
        row(f"  {L} direction %", *[p["direction"].get(L, {}) for p in profiles])
    row("segments", *[p["segments"] for p in profiles])
    row("vias", *[{k: p["vias"][k] for k in ("count", "stitching")} for p in profiles])
    row("  via sizes", *[p["vias"]["sizes"] for p in profiles])
    row("  vias by class", *[p["vias"]["by_class"] for p in profiles])
    row("pours", *[{k: p["pours"][k] for k in ("zones", "nets")} for p in profiles])
    row("spacing", *[p["spacing"] for p in profiles])
    row("detour (2-pad nets)", *[p["detour"] for p in profiles])
    classes = sorted({c for p in profiles for c in p["widths"]})
    for c in classes:
        row(f"class {c}", *[(p["widths"].get(c, {}).get("class"), p["widths"].get(c, {}).get("used"))
                            for p in profiles])
        row("  layers %", *[p["class_layers"].get(c, {}) for p in profiles])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("boards", nargs="+")
    ap.add_argument("--planes", default="GND", help="plane nets, counted apart in the via figures")
    ap.add_argument("--json", default="")
    ap.add_argument("--learn", default="", help="write the first board's routing style (engine/style.py) here, "
                    "for improve_bench.py --style")
    a = ap.parse_args()
    if a.learn:
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "plugins"))
        from autorouter.engine import style as style_mod
        learnt = style_mod.learn(load(a.boards[0]))
        with open(a.learn, "w") as f:
            json.dump(learnt, f, indent=1)
        print(json.dumps(learnt, indent=1))
    planes = set(s for s in a.planes.split(",") if s)
    ps = [profile(p, planes) for p in a.boards]
    show(ps)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(ps, f, indent=1)


if __name__ == "__main__":
    sys.exit(main())
