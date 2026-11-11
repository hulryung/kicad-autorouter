"""Layout-convention audit of a routed board (run with KiCad's Python).

Measures what a reviewer looks at on a hand-routed board, so a routed result
can be compared with a designer's version of the same board
(docs/routing-policies.md):

  - IC pins (QFP, QFN, SOIC): the side each track leaves its pad by (outward,
    under the body, sideways), the straight run before the first bend, and
    where the first via sits;
  - two-pin parts: the side tracks leave by, and other nets' tracks under them;
  - vias against the SMD pads of their own net (in the pad, touching, near);
  - neighbouring pins of one net: joined pad to pad or outside the row.

  <KiCad python3> scripts/conventions.py board.kicad_pcb [--json out.json] [--list]

--list prints the IC pins that do not leave outward.
"""
import json
import math
import sys
from collections import Counter, defaultdict

import pcbnew

SMD = (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD_ATTRIB_CONN)


def unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n else (0.0, 0.0)


def pad_hit(pad, x, y):
    return pad.HitTest(pcbnew.VECTOR2I(int(x), int(y)))


class Net:
    def __init__(self):
        self.segs = defaultdict(list)     # layer -> [(ax, ay, bx, by, width)]
        self.vias = []                    # (x, y, r)


def load(path):
    board = pcbnew.LoadBoard(path)
    nets = defaultdict(Net)
    for t in board.GetTracks():
        code = t.GetNetCode()
        if t.Type() == pcbnew.PCB_VIA_T:
            p = t.GetPosition()
            nets[code].vias.append((p.x, p.y, t.GetWidth(pcbnew.F_Cu) / 2))
        else:
            a, b = t.GetStart(), t.GetEnd()
            if math.hypot(a.x - b.x, a.y - b.y) < 1000:
                continue
            nets[code].segs[t.GetLayer()].append((a.x, a.y, b.x, b.y, t.GetWidth()))
    return board, nets


def ring(fp):
    pads = [p for p in fp.Pads() if p.GetAttribute() in SMD]
    if len(pads) < 6:
        return None
    xs = [p.GetPosition().x for p in pads]
    ys = [p.GetPosition().y for p in pads]
    box = (min(xs), min(ys), max(xs), max(ys))
    if box[2] - box[0] < 500_000 or box[3] - box[1] < 500_000:
        return None
    return box


def perimeter(fp, box, tol=300_000):
    """{pad: (nx, ny)} for pads on the pin ring, with the outward normal."""
    out = []
    inner = 0
    for p in fp.Pads():
        if p.GetAttribute() not in SMD:
            continue
        q = p.GetPosition()
        d = [(q.x - box[0], (-1, 0)), (box[2] - q.x, (1, 0)), (q.y - box[1], (0, -1)), (box[3] - q.y, (0, 1))]
        d.sort(key=lambda e: e[0])
        if d[0][0] <= tol:
            # the pad's long axis decides at a corner
            n = d[0][1]
            if d[1][0] <= tol:
                size = p.GetSize()
                ang = math.radians(p.GetOrientationDegrees())
                ax = (math.cos(ang), math.sin(ang)) if size.x >= size.y else (-math.sin(ang), math.cos(ang))
                n = max((d[0][1], d[1][1]), key=lambda v: abs(v[0] * ax[0] + v[1] * ax[1]))
            out.append((p, n))
        else:
            inner += 1
    return out, inner


def pitch_of(pads):
    pts = [(p.GetPosition().x, p.GetPosition().y) for p in pads]
    best = 1e18
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            best = min(best, math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]))
    return best


def exits(pad, net, layer):
    """Tracks leaving a pad: [(ux, uy, straight_nm, via (x, y) or None, via_dist_nm)]."""
    segs = net.segs.get(layer, [])
    ends = defaultdict(list)
    for i, s in enumerate(segs):
        ends[(s[0], s[1])].append(i)
        ends[(s[2], s[3])].append(i)
    vias = {(x, y) for x, y, _ in net.vias}
    out = []
    seen = set()
    # start segments: one end inside the pad
    stack = []
    for i, s in enumerate(segs):
        a_in, b_in = pad_hit(pad, s[0], s[1]), pad_hit(pad, s[2], s[3])
        if a_in or b_in:
            stack.append((i, a_in, b_in))
    for i, a_in, b_in in stack:
        if a_in and b_in:
            continue
        s = segs[i]
        if (i,) in seen:
            continue
        seen.add((i,))
        (sx, sy), (ex, ey) = ((s[0], s[1]), (s[2], s[3])) if a_in else ((s[2], s[3]), (s[0], s[1]))
        ux, uy = unit(ex - sx, ey - sy)
        length = math.hypot(ex - sx, ey - sy)
        # where it crosses the pad edge
        lo, hi = 0.0, length
        for _ in range(20):
            mid = (lo + hi) / 2
            if pad_hit(pad, sx + ux * mid, sy + uy * mid):
                lo = mid
            else:
                hi = mid
        straight = length - lo
        travelled = straight
        via = None
        cur, pt, d = i, (ex, ey), (ux, uy)
        bent = False
        for _ in range(200):
            if pt in vias:
                via = pt
                break
            nxt = [j for j in ends[pt] if j != cur]
            if len(nxt) != 1:
                break
            j = nxt[0]
            t = segs[j]
            other = (t[2], t[3]) if (t[0], t[1]) == pt else (t[0], t[1])
            nd = unit(other[0] - pt[0], other[1] - pt[1])
            seg_len = math.hypot(other[0] - pt[0], other[1] - pt[1])
            if not bent and nd[0] * d[0] + nd[1] * d[1] > 0.999:
                straight += seg_len
            else:
                bent = True
            travelled += seg_len
            cur, pt, d = j, other, nd
            if travelled > 6_000_000:
                break
        out.append((ux, uy, straight, via, travelled if via else None))
    return out


def audit(path):
    board, nets = load(path)
    res = {
        "body": Counter(), "body_fine": Counter(), "body_coarse": Counter(),
        "straight_out": [], "via_first": Counter(), "via_dist": [],
        "passive": Counter(), "via_pad": Counter(), "adjacent": Counter(),
        "under_passive": 0, "passives": 0, "details": [],
    }
    all_pads = list(board.GetPads())
    for fp in board.GetFootprints():
        smd = [p for p in fp.Pads() if p.GetAttribute() in SMD]
        box = ring(fp)
        if box is not None:
            per, inner = perimeter(fp, box)
            if not per or inner > 0.3 * len(smd):
                continue
            fine = pitch_of([p for p, _ in per]) < 800_000
            cls = "body_fine" if fine else "body_coarse"
            for pad, n in per:
                code = pad.GetNetCode()
                if code <= 0:
                    continue
                layer = pad.GetLayer()
                ex = exits(pad, nets[code], layer)
                pos = pad.GetPosition()
                if not ex:
                    hasvia = any(pad_hit(pad, x, y) for x, y, _ in nets[code].vias)
                    res["body"]["no track (via in pad)" if hasvia else "no track"] += 1
                    continue
                for ux, uy, straight, via, vdist in ex:
                    dot = ux * n[0] + uy * n[1]
                    kind = "out" if dot >= 0.9 else "out-diag" if dot >= 0.5 else "in" if dot <= -0.9 \
                        else "in-diag" if dot <= -0.5 else "side"
                    res["body"][kind] += 1
                    res[cls][kind] += 1
                    if kind == "out":
                        res["straight_out"].append(straight / 1e6)
                    if via is not None:
                        under = box[0] < via[0] < box[2] and box[1] < via[1] < box[3]
                        res["via_first"]["under body" if under else "outside"] += 1
                        res["via_dist"].append(vdist / 1e6)
                    res["details"].append((fp.GetReference(), pad.GetNumber(), pad.GetNetname(), kind,
                                           round(straight / 1e6, 2),
                                           None if via is None else round(vdist / 1e6, 2)))
            # adjacent same-net pins
            plist = sorted(per, key=lambda e: (e[1], e[0].GetPosition().x, e[0].GetPosition().y))
            pit = pitch_of([p for p, _ in per])
            for i, (pa, na) in enumerate(plist):
                for pb, nb in plist[i + 1:i + 3]:
                    if na != nb or pa.GetNetCode() != pb.GetNetCode() or pa.GetNetCode() <= 0:
                        continue
                    qa, qb = pa.GetPosition(), pb.GetPosition()
                    if math.hypot(qa.x - qb.x, qa.y - qb.y) > pit * 1.2:
                        continue
                    direct = False
                    for s in nets[pa.GetNetCode()].segs.get(pa.GetLayer(), []):
                        if (pad_hit(pa, s[0], s[1]) and pad_hit(pb, s[2], s[3])) or \
                                (pad_hit(pb, s[0], s[1]) and pad_hit(pa, s[2], s[3])):
                            direct = True
                    res["adjacent"]["joined pad to pad" if direct else "not joined directly"] += 1
        elif len(smd) == 2 and len(list(fp.Pads())) == 2:
            a, b = smd
            qa, qb = a.GetPosition(), b.GetPosition()
            res["passives"] += 1
            for pad, other in ((a, qb), (b, qa)):
                code = pad.GetNetCode()
                if code <= 0:
                    continue
                q = pad.GetPosition()
                ax = unit(q.x - other.x, q.y - other.y)
                for ux, uy, straight, via, vdist in exits(pad, nets[code], pad.GetLayer()):
                    dot = ux * ax[0] + uy * ax[1]
                    kind = "end" if dot >= 0.9 else "end-diag" if dot >= 0.5 else "inner" if dot <= -0.5 else "side"
                    res["passive"][kind] += 1
            # foreign tracks under the part
            layer = a.GetLayer()
            mx, my = (qa.x + qb.x) / 2, (qa.y + qb.y) / 2
            hit = False
            for code, net in nets.items():
                if code in (a.GetNetCode(), b.GetNetCode()):
                    continue
                for s in net.segs.get(layer, []):
                    # distance from the midpoint to the segment
                    dx, dy = s[2] - s[0], s[3] - s[1]
                    L2 = dx * dx + dy * dy
                    t = max(0.0, min(1.0, ((mx - s[0]) * dx + (my - s[1]) * dy) / L2))
                    if math.hypot(mx - s[0] - t * dx, my - s[1] - t * dy) < s[4] / 2 + 50_000:
                        hit = True
            if hit:
                res["under_passive"] += 1
    # vias against the SMD pads of their net
    pads_by_net = defaultdict(list)
    for p in all_pads:
        if p.GetAttribute() in SMD:
            pads_by_net[p.GetNetCode()].append(p)
    for code, net in nets.items():
        for x, y, r in net.vias:
            best = None
            for p in pads_by_net.get(code, []):
                q = p.GetPosition()
                if abs(q.x - x) > 5_000_000 or abs(q.y - y) > 5_000_000:
                    continue
                if pad_hit(p, x, y):
                    best = -1
                    break
                # gap from the via's edge to the pad's box
                bb = p.GetBoundingBox()
                dx = max(bb.GetLeft() - x, 0, x - bb.GetRight())
                dy = max(bb.GetTop() - y, 0, y - bb.GetBottom())
                g = math.hypot(dx, dy) - r
                best = g if best is None else min(best, g)
            if best is None:
                res["via_pad"]["far"] += 1
            elif best == -1:
                res["via_pad"]["in pad"] += 1
            elif best < 0:
                res["via_pad"]["overlaps pad"] += 1
            elif best < 150_000:
                res["via_pad"]["< 0.15 mm"] += 1
            elif best < 400_000:
                res["via_pad"]["0.15-0.4 mm"] += 1
            else:
                res["via_pad"][">= 0.4 mm"] += 1
    return res


def med(v):
    v = sorted(v)
    return round(v[len(v) // 2], 2) if v else None


def share(c):
    tot = sum(c.values()) or 1
    return ", ".join(f"{k} {v} ({100 * v // tot}%)" for k, v in c.most_common())


def main():
    path = sys.argv[1]
    r = audit(path)
    print("==", path.split("/")[-1])
    print(" IC pin exits      :", share(r["body"]))
    print("   fine (<0.8 mm)  :", share(r["body_fine"]))
    print("   coarse          :", share(r["body_coarse"]))
    so = r["straight_out"]
    print(f"   straight run of outward exits: median {med(so)} mm, "
          f"under 0.2 mm: {sum(1 for v in so if v < 0.2)}/{len(so)}")
    print("   first via       :", share(r["via_first"]), "median dist", med(r["via_dist"]))
    print(" same-net neighbours:", share(r["adjacent"]))
    print(f" passives          : {r['passives']}, exits: {share(r['passive'])}; "
          f"foreign track under: {r['under_passive']}")
    print(" vias vs own pads  :", share(r["via_pad"]))
    if "--list" in sys.argv:
        for d in r["details"]:
            if d[3] in ("in", "in-diag", "side"):
                print("   ", d)
    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
        r2 = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in r.items()}
        json.dump(r2, open(out, "w"))


main()
