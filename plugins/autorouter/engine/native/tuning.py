"""Length matching: meanders on routed tracks so the nets of a group come
out the same length (a memory byte lane, a parallel bus, the two halves of
a differential pair).

Runs on the board after routing, the way KiCad's length tuner does it by
hand: the shorter nets of a group get trapezoid bumps (45-degree corners) on
their straight runs, as many and as tall as the room beside each run allows,
until each is within the group's tolerance of the target. Room is checked
against the real copper (pads, tracks, vias, keepouts, the board edge) at the
nets' clearance; the pours are refilled afterwards and do not count.
"""
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pcbnew

CHAMFER_LOSS = 8 - 4 * math.sqrt(2)          # per bump: 2A - CHAMFER_LOSS * c is the length it adds


@dataclass
class TuneReport:
    groups: List[dict] = field(default_factory=list)     # per group: name, target, before/after per net
    added: List[object] = field(default_factory=list)    # board items created
    removed: int = 0

    def lines(self) -> List[str]:
        out = []
        for g in self.groups:
            mm = lambda v: f"{v / 1e6:.2f}"
            out.append(f"{g['name']}: target {mm(g['target'])} mm +- {mm(g['tolerance'])}: "
                       f"spread {mm(g['spread_before'])} -> {mm(g['spread_after'])} mm"
                       + ("" if g["ok"] else f" (still off: {', '.join(g['short'])})"))
        return out


def kicad_pattern_defaults(board, amin_nm: int, amax_nm: int) -> Tuple[int, int, int]:
    """KiCad's single-track tuning pattern defaults from the project (Board
    Setup > Length-tuning Patterns): (spacing, min amplitude, max amplitude)."""
    import json
    from pathlib import Path
    spacing = 600_000
    try:
        pro = Path(board.GetFileName()).with_suffix(".kicad_pro")
        d = json.loads(pro.read_text(encoding="utf-8"))["board"]["design_settings"]["tuning_pattern_settings"]
        t = d["single_track_defaults"]
        spacing = int(float(t["spacing"]) * 1e6)
        amin_nm = int(float(t["min_amplitude"]) * 1e6)
        amax_nm = int(float(t["max_amplitude"]) * 1e6)
    except Exception:
        pass
    return spacing, amin_nm, amax_nm


def track_lengths(board, codes=None) -> Dict[int, int]:
    """Net code -> length of its tracks (nm)."""
    out: Dict[int, int] = {}
    for t in board.GetTracks():
        if t.GetClass() not in ("PCB_TRACK", "PCB_ARC"):
            continue
        c = t.GetNetCode()
        if c <= 0 or (codes is not None and c not in codes):
            continue
        out[c] = out.get(c, 0) + int(t.GetLength())
    return out


def _uid(item) -> str:
    """SWIG hands out a new wrapper per lookup: items are told apart by UUID."""
    return str(item.m_Uuid.AsString())


def _seg_dist(a, b) -> float:
    """Distance between segments a and b, each (x1, y1, x2, y2)."""
    def pt_seg(px, py, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ll))
        return math.hypot(px - x1 - t * dx, py - y1 - t * dy)

    def cross(ox, oy, ax, ay, bx, by):
        return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)
    x1, y1, x2, y2 = a
    x3, y3, x4, y4 = b
    d1, d2 = cross(x3, y3, x4, y4, x1, y1), cross(x3, y3, x4, y4, x2, y2)
    d3, d4 = cross(x1, y1, x2, y2, x3, y3), cross(x1, y1, x2, y2, x4, y4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(pt_seg(x1, y1, *b), pt_seg(x2, y2, *b), pt_seg(x3, y3, *a), pt_seg(x4, y4, *a))


class _Room:
    """The copper and outline the meanders must keep clear of, per layer."""

    def __init__(self, board):
        self.board = board
        ds = board.GetDesignSettings()
        self.min_clr = int(ds.m_MinClearance)
        self.edge_clr = max(int(ds.m_CopperEdgeClearance), 0)
        self.items: Dict[int, List[Tuple[Tuple[int, int, int, int], object, int, object]]] = {}
        for t in board.GetTracks():
            self._add(t, [t.GetLayer()] if t.GetClass() != "PCB_VIA" else list(t.GetLayerSet().CuStack()))
        for pad in board.GetPads():
            self._add(pad, list(pad.GetLayerSet().CuStack()))
        self.keepouts = []
        for zone in board.Zones():
            try:
                if zone.GetIsRuleArea() and zone.GetDoNotAllowTracks():
                    self.keepouts.append(zone)
            except Exception:
                pass
        self.outline = pcbnew.SHAPE_POLY_SET()
        if not board.GetBoardPolygonOutlines(self.outline, True) or self.outline.OutlineCount() == 0:
            self.outline = None
        # the outline's edges (outer and cut-outs), for the edge clearance
        self.edges: List[Tuple[int, int, int, int]] = []
        if self.outline is not None:
            chains = []
            for i in range(self.outline.OutlineCount()):
                chains.append(self.outline.Outline(i))
                for j in range(self.outline.HoleCount(i)):
                    chains.append(self.outline.Hole(i, j))
            for ch in chains:
                n = ch.PointCount()
                for k in range(n):
                    a, b = ch.CPoint(k), ch.CPoint((k + 1) % n)
                    self.edges.append((a.x, a.y, b.x, b.y))

    def _add(self, item, layers):
        bb = item.GetBoundingBox()
        box = (bb.GetX(), bb.GetY(), bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight())
        uid = _uid(item)
        for lid in layers:
            self.items.setdefault(lid, []).append((box, item, item.GetNetCode(), uid))

    def forget(self, item):
        lid, uid = item.GetLayer(), _uid(item)
        self.items[lid] = [e for e in self.items.get(lid, []) if e[3] != uid]

    def remember(self, item):
        self._add(item, [item.GetLayer()])

    def clear(self, lid: int, pts: List[Tuple[int, int]], width: int, code: int, clr: int, skip=()) -> bool:
        """Is the polyline pts (width wide) clear of everything at clr?"""
        segs = [pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(*pts[i]), pcbnew.VECTOR2I(*pts[i + 1]), width)
                for i in range(len(pts) - 1)]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        reach = width // 2 + clr + 1
        box = (min(xs) - reach, min(ys) - reach, max(xs) + reach, max(ys) + reach)
        if self.outline is not None:
            for x, y in pts:
                if not self.outline.Contains(pcbnew.VECTOR2I(x, y)):
                    return False
            edge = width // 2 + self.edge_clr
            for i in range(len(pts) - 1):
                sx0, sy0 = min(pts[i][0], pts[i + 1][0]) - edge, min(pts[i][1], pts[i + 1][1]) - edge
                sx1, sy1 = max(pts[i][0], pts[i + 1][0]) + edge, max(pts[i][1], pts[i + 1][1]) + edge
                for ex0, ey0, ex1, ey1 in self.edges:
                    if max(ex0, ex1) < sx0 or min(ex0, ex1) > sx1 or max(ey0, ey1) < sy0 or min(ey0, ey1) > sy1:
                        continue
                    if _seg_dist(pts[i] + pts[i + 1], (ex0, ey0, ex1, ey1)) < edge:
                        return False
        skip_ids = {_uid(k) for k in skip}
        for b, item, net, uid in self.items.get(lid, []):
            if b[2] < box[0] or b[0] > box[2] or b[3] < box[1] or b[1] > box[3]:
                continue
            if uid in skip_ids:
                continue
            try:
                shape = item.GetEffectiveShape(lid)
            except Exception:
                shape = item.GetEffectiveShape()
            need = clr
            if net != code:
                try:
                    need = max(clr, int(item.GetOwnClearance(lid)))
                except Exception:
                    pass
            for s in segs:
                try:
                    if shape.Collide(s, need):
                        return False
                except TypeError:
                    if shape.Collide(s.GetSeg(), need + width // 2):
                        return False
        for zone in self.keepouts:
            if not zone.IsOnLayer(lid):
                continue
            outline = zone.Outline()
            for s in segs:
                if outline.Collide(s.GetSeg(), width // 2):
                    return False
        return True


def _bump(ax, ay, ux, uy, nx, ny, x0: float, p: float, a: float, c: float) -> List[Tuple[int, int]]:
    """One trapezoid bump starting x0 along the run (u) on side n, amplitude a,
    legs p apart, corners chamfered by c: the points after the baseline."""
    def pt(along, out):
        return (int(round(ax + ux * along + nx * out)), int(round(ay + uy * along + ny * out)))
    return [pt(x0 - c, 0), pt(x0, c), pt(x0, a - c), pt(x0 + c, a), pt(x0 + p - c, a), pt(x0 + p, a - c),
            pt(x0 + p, c), pt(x0 + p + c, 0)]


def _extra(a: float, p: float) -> Tuple[float, float]:
    """(length a bump of amplitude a adds, its chamfer)."""
    c = min(p / 4, a / 4)
    return 2 * a - CHAMFER_LOSS * c, c


@dataclass
class _Slot:
    track: object
    x0: float              # along the run, start of the bump's first leg
    side: int
    amax: float            # largest amplitude that fits


def _runs(board, code: int):
    """The net's straight segments (tracks only), longest first."""
    segs = [t for t in board.GetTracks() if t.GetClass() == "PCB_TRACK" and t.GetNetCode() == code]
    return sorted(segs, key=lambda t: -t.GetLength())


def _slots(room: _Room, track, p: float, amin: float, amax_cap: float, clr: int, avoid_net: int) -> List[_Slot]:
    """Places along a straight track where a bump fits, with its tallest amplitude."""
    s, e = track.GetStart(), track.GetEnd()
    ax, ay, bx, by = s.x, s.y, e.x, e.y
    length = math.hypot(bx - ax, by - ay)
    w = track.GetWidth()
    margin = max(p, 2 * w)
    if length < 2 * margin + p:
        return []
    ux, uy = (bx - ax) / length, (by - ay) / length
    out: List[_Slot] = []
    x0 = margin + p / 4
    lid = track.GetLayer()
    code = track.GetNetCode()
    while x0 + p + p / 4 <= length - margin:
        best = None
        for side in (1, -1):
            nx, ny = -uy * side, ux * side
            lo, hi = 0.0, amax_cap
            # the tallest amplitude whose bump is clear (bisection, 6 steps)
            if not room.clear(lid, _bump(ax, ay, ux, uy, nx, ny, x0, p, amin, _extra(amin, p)[1]), w, code, clr,
                              skip=(track,)):
                continue
            lo = amin
            for _ in range(6):
                mid = (lo + hi) / 2
                if room.clear(lid, _bump(ax, ay, ux, uy, nx, ny, x0, p, mid, _extra(mid, p)[1]), w, code, clr,
                              skip=(track,)):
                    lo = mid
                else:
                    hi = mid
            if best is None or lo > best.amax:
                best = _Slot(track, x0, side, lo)
        if best is not None:
            out.append(best)
        x0 += 2 * p
    return out


def _rebuild(board, room: _Room, track, bumps: List[Tuple[float, int, float]], p: float) -> List[object]:
    """Replace a straight track with the same run carrying the bumps
    [(x0, side, amplitude)]; returns the new tracks."""
    s, e = track.GetStart(), track.GetEnd()
    ax, ay, bx, by = s.x, s.y, e.x, e.y
    length = math.hypot(bx - ax, by - ay)
    ux, uy = (bx - ax) / length, (by - ay) / length
    pts = [(ax, ay)]
    for x0, side, a in sorted(bumps):
        nx, ny = -uy * side, ux * side
        pts += _bump(ax, ay, ux, uy, nx, ny, x0, p, a, _extra(a, p)[1])
    pts.append((bx, by))
    clean = [pts[0]]
    for q in pts[1:]:
        if q != clean[-1]:
            clean.append(q)
    new = []
    for i in range(len(clean) - 1):
        t = pcbnew.PCB_TRACK(board)
        t.SetStart(pcbnew.VECTOR2I(*clean[i]))
        t.SetEnd(pcbnew.VECTOR2I(*clean[i + 1]))
        t.SetWidth(track.GetWidth())
        t.SetLayer(track.GetLayer())
        t.SetNetCode(track.GetNetCode())
        board.Add(t)
        room.remember(t)
        new.append(t)
    room.forget(track)
    board.Remove(track)
    return new


def lengthen(board, room: _Room, code: int, need: float, clr: int, spacing: int, amin: int, amax: int,
             avoid_net: int = 0) -> Tuple[float, List[object], List[object]]:
    """Add about `need` nm of length to a net with bumps. Returns (added
    length, new items, removed items)."""
    tracks = _runs(board, code)
    if not tracks:
        return 0.0, [], []
    w = max(t.GetWidth() for t in tracks)
    p = float(max(spacing, w + clr))
    slots: List[_Slot] = []
    for t in tracks:
        slots += _slots(room, t, p, float(amin), float(amax), clr, avoid_net)
        if sum(_extra(sl.amax, p)[0] for sl in slots) >= need * 1.5:
            break                                    # enough room found; no need to survey the rest
    if not slots:
        return 0.0, [], []
    # the fewest bumps: the tallest slots first, then all of them scaled down
    # by one factor so the total comes out at `need`
    slots.sort(key=lambda sl: -sl.amax)
    chosen, total = [], 0.0
    for sl in slots:
        chosen.append(sl)
        total += _extra(sl.amax, p)[0]
        if total >= need:
            break
    # neighbouring slots on one track overlap when on opposite sides only if
    # they share a leg: slots are 2p apart, a bump is p wide, so they never do
    def extra_at(t):
        return sum(_extra(amin + t * (sl.amax - amin), p)[0] for sl in chosen)
    # fewer bumps while even the lowest ones overshoot
    while len(chosen) > 1 and extra_at(0.0) > need:
        chosen.pop()
    if total <= need:
        amps = [sl.amax for sl in chosen]           # all the room there is
    elif extra_at(0.0) <= need:
        lo, hi = 0.0, 1.0
        for _ in range(40):
            mid = (lo + hi) / 2
            if extra_at(mid) < need:
                lo = mid
            else:
                hi = mid
        amps = [amin + hi * (sl.amax - amin) for sl in chosen]
    else:
        # less than one minimum bump: a lower one (down to the chamfer)
        lo, hi = 0.0, float(amin)
        for _ in range(40):
            mid = (lo + hi) / 2
            if _extra(mid, p)[0] < need:
                lo = mid
            else:
                hi = mid
        amps = [hi]
    by_track: Dict[int, Tuple[object, List]] = {}
    for sl, a in zip(chosen, amps):
        by_track.setdefault(id(sl.track), (sl.track, []))[1].append((sl.x0, sl.side, a))
    new, removed = [], []
    added = 0.0
    for track, bumps in by_track.values():
        before = track.GetLength()
        items = _rebuild(board, room, track, bumps, p)
        added += sum(i.GetLength() for i in items) - before
        new += items
        removed.append(track)
    return added, new, removed


def tune(board, groups: List[dict], status: Optional[Callable[[str], None]] = None,
         spacing_nm: int = 0, amin_nm: int = 200_000, amax_nm: int = 1_000_000,
         removed_sink: Optional[list] = None) -> TuneReport:
    """Bring each group's nets to its target length (the longest member, or
    the group's own target) within its tolerance. groups: [{"name", "nets":
    [net names], "tolerance": nm, "target": nm or 0}]. Meander spacing and
    amplitudes default to KiCad's tuning pattern defaults."""
    say = status or (lambda t: None)
    report = TuneReport()
    if not spacing_nm:
        spacing_nm, amin_nm, amax_nm = kicad_pattern_defaults(board, amin_nm, amax_nm)
    by_name = {n.GetNetname(): c for c, n in board.GetNetsByNetcode().items() if c > 0}
    room = _Room(board)
    ds = board.GetDesignSettings()
    for g in groups:
        codes = [by_name[n] for n in g["nets"] if n in by_name]
        if not codes:
            continue
        lengths = track_lengths(board, set(codes))
        routed = [c for c in codes if lengths.get(c, 0) > 0]
        if not routed:
            continue
        tol = max(int(g.get("tolerance") or 0), 10_000)
        target = int(g.get("target") or 0) or max(lengths[c] for c in routed)
        before = {c: lengths.get(c, 0) for c in codes}
        say(f"Length matching {g['name']}: target {target / 1e6:.2f} mm")
        partner = {}
        if g.get("pair") and len(codes) == 2:
            partner = {codes[0]: codes[1], codes[1]: codes[0]}
        for c in routed:
            need = target - lengths[c]
            if need <= (tol if g.get("target") else tol / 2):
                continue                      # within the window (or longer: meanders cannot shorten)
            net = board.FindNet(c)
            clr = int(ds.m_MinClearance)
            try:
                clr = max(clr, int(ds.m_NetSettings.GetEffectiveNetClass(net.GetNetname()).GetClearance()))
            except Exception:
                pass
            added, new, removed = lengthen(board, room, c, need, clr, spacing_nm, amin_nm, amax_nm,
                                           avoid_net=partner.get(c, 0))
            report.added += new
            report.removed += len(removed)
            if removed_sink is not None:
                for item in removed:
                    item.thisown = 0
                    removed_sink.append(item)
        after = track_lengths(board, set(codes))
        vals_b = [before[c] for c in routed]
        vals_a = [after.get(c, 0) for c in routed]
        short = [board.FindNet(c).GetNetname() for c in routed if abs(after.get(c, 0) - target) > tol]
        report.groups.append({"name": g["name"], "target": target, "tolerance": tol,
                              "before": {board.FindNet(c).GetNetname(): before[c] for c in routed},
                              "after": {board.FindNet(c).GetNetname(): after.get(c, 0) for c in routed},
                              "spread_before": max(vals_b) - min(vals_b), "spread_after": max(vals_a) - min(vals_a),
                              "ok": not short, "short": short})
    return report
