"""Length matching: meanders on routed tracks so the nets of a group come
out the same length (a memory byte lane, a parallel bus, the two halves of
a differential pair).

Runs on the board after routing, the way KiCad's length tuner does it by
hand: the shorter nets of a group get trapezoid bumps on their straight runs
(corners rounded with arc fillets, or 45-degree chamfers), as many and as tall as the room beside each run allows,
until each is within the group's tolerance of the target. Room is checked
against the real copper (pads, tracks, vias, keepouts, the board edge) at the
nets' clearance; the pours are refilled afterwards and do not count.
"""
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pcbnew

CHAMFER_LOSS = 8 - 4 * math.sqrt(2)          # per bump: 2A - CHAMFER_LOSS * c is the length it adds
FILLET_LOSS = 8 - 2 * math.pi                # the same with each corner an arc of radius c
MIN_FILLET = 20_000                          # smaller corners stay chamfered (an arc would be degenerate)
ROUNDED = True                               # module default; tune(rounded=...) sets it per run


@dataclass
class TuneReport:
    groups: List[dict] = field(default_factory=list)     # per group: name, target, before/after per net
    added: List[object] = field(default_factory=list)    # board items created
    removed: int = 0
    patterns: List[dict] = field(default_factory=list)   # one per tuned net: what a KiCad tuning pattern needs
    kicad_patterns: int = 0                              # how many became KiCad tuning patterns on the board

    def lines(self) -> List[str]:
        out = []
        for g in self.groups:
            mm = lambda v: f"{v / 1e6:.2f}"
            out.append(f"{g['name']}: target {mm(g['target'])} mm +- {mm(g['tolerance'])}: "
                       f"spread {mm(g['spread_before'])} -> {mm(g['spread_after'])} mm"
                       + ("" if g["ok"] else f" (still off: {', '.join(g['short'])})"))
        if self.kicad_patterns:
            out.append(f"{self.kicad_patterns} meander(s) are KiCad tuning patterns: KiCad's length tuner "
                       "rebuilds them (Edit > Update All Tuning Patterns); where it finds no room a pattern "
                       "shows 'too short', and Ctrl+Z after that update brings the router's meanders back")
        return out


def kicad_corner_percent(board) -> int:
    """KiCad's corner size for tuning patterns (Board Setup > Length-tuning Patterns)."""
    import json
    from pathlib import Path
    try:
        pro = Path(board.GetFileName()).with_suffix(".kicad_pro")
        d = json.loads(pro.read_text(encoding="utf-8"))["board"]["design_settings"]["tuning_pattern_settings"]
        return int(d["single_track_defaults"]["corner_radius_percentage"])
    except Exception:
        return 80


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


def layer_heights(board) -> Dict[int, Tuple[float, float]]:
    """KiCad layer id -> (top, bottom) of that copper in the stackup, nm from
    the top surface. From the board's stackup when it has one, else the board
    thickness shared evenly."""
    from .. import stackup as stk
    ids = list(board.GetEnabledLayers().CuStack())[:board.GetCopperLayerCount()]
    st = stk.board_stackup(board)
    out: Dict[int, Tuple[float, float]] = {}
    if st is not None and st.layers == len(ids):
        z = 0.0
        for i, lid in enumerate(ids):
            t = st.copper_mm[i] * 1e6
            out[lid] = (z, z + t)
            z += t + (st.dielectrics[i].thickness_mm * 1e6 if i < len(st.dielectrics) else 0)
        return out
    total = float(board.GetDesignSettings().GetBoardThickness())
    step = total / max(1, len(ids) - 1)
    for i, lid in enumerate(ids):
        out[lid] = (i * step, i * step)
    return out


def track_lengths(board, codes=None, vias: bool = True) -> Dict[int, int]:
    """Net code -> its length the way KiCad's length tuner and DRC measure it
    (nm): the tracks, plus each via's height through the stackup between the
    outermost copper layers the net uses there."""
    out: Dict[int, int] = {}
    ends: Dict[Tuple[int, int, int], set] = {}
    via_list = []
    for t in board.GetTracks():
        c = t.GetNetCode()
        if c <= 0 or (codes is not None and c not in codes):
            continue
        if t.GetClass() == "PCB_VIA":
            via_list.append(t)
            continue
        if t.GetClass() not in ("PCB_TRACK", "PCB_ARC"):
            continue
        out[c] = out.get(c, 0) + int(t.GetLength())
        if vias:
            for p in (t.GetStart(), t.GetEnd()):
                ends.setdefault((c, p.x, p.y), set()).add(t.GetLayer())
    if vias and via_list:
        heights = layer_heights(board)
        pads: Dict[int, list] = {}
        for pad in board.GetPads():
            if pad.GetNetCode() > 0:
                pads.setdefault(pad.GetNetCode(), []).append(pad)
        for v in via_list:
            c = v.GetNetCode()
            p = v.GetPosition()
            used = set(ends.get((c, p.x, p.y), set()))
            for pad in pads.get(c, []):
                if pad.HitTest(p):
                    used |= {l for l in pad.GetLayerSet().CuStack() if l in heights}
            used = [l for l in used if l in heights]
            if len(used) < 2:
                continue
            top = min(heights[l][0] for l in used)
            bottom = max(heights[l][1] for l in used)
            out[c] = out.get(c, 0) + int(bottom - top)
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
    legs p apart, corners cut by c: the points after the baseline. Rounded
    (fillets), each corner is three points: arc start, arc middle, arc end."""
    def pt(along, out):
        return (int(round(ax + ux * along + nx * out)), int(round(ay + uy * along + ny * out)))
    corners = [((x0 - c, 0), (x0, 0), (x0, c)), ((x0, a - c), (x0, a), (x0 + c, a)),
               ((x0 + p - c, a), (x0 + p, a), (x0 + p, a - c)), ((x0 + p, c), (x0 + p, 0), (x0 + p + c, 0))]
    out = []
    for (s0, s1), (q0, q1), (e0, e1) in corners:
        out.append(pt(s0, s1))
        if _rounded(c):
            # the fillet's middle: from the arc centre toward the square corner, c out
            cx, cy = s0 + e0 - q0, s1 + e1 - q1
            k = c / math.hypot(q0 - cx, q1 - cy)
            out.append(pt(cx + (q0 - cx) * k, cy + (q1 - cy) * k))
        out.append(pt(e0, e1))
    return out


def _rounded(c: float) -> bool:
    return ROUNDED and c >= MIN_FILLET


def _extra(a: float, p: float) -> Tuple[float, float]:
    """(length a bump of amplitude a adds, its corner size)."""
    c = min(p / 4, a / 4)
    return 2 * a - (FILLET_LOSS if _rounded(c) else CHAMFER_LOSS) * c, c


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
    # the path as lines (point) and fillets (start, middle, end)
    steps: List[tuple] = [(ax, ay)]
    for x0, side, a in sorted(bumps):
        nx, ny = -uy * side, ux * side
        c = _extra(a, p)[1]
        q = _bump(ax, ay, ux, uy, nx, ny, x0, p, a, c)
        if _rounded(c):
            for k in range(0, len(q), 3):
                steps += [q[k], (q[k + 1], q[k + 2])]
        else:
            steps += q
    steps.append((bx, by))
    new = []
    at = steps[0]
    for st in steps[1:]:
        arc = isinstance(st[0], tuple)
        end = st[1] if arc else st
        if end == at:
            continue
        if arc:
            t = pcbnew.PCB_ARC(board)
            t.SetStart(pcbnew.VECTOR2I(*at))
            t.SetMid(pcbnew.VECTOR2I(*st[0]))
            t.SetEnd(pcbnew.VECTOR2I(*end))
        else:
            t = pcbnew.PCB_TRACK(board)
            t.SetStart(pcbnew.VECTOR2I(*at))
            t.SetEnd(pcbnew.VECTOR2I(*end))
        at = end
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
    per_track: List[List[_Slot]] = []
    for t in tracks:
        found = _slots(room, t, p, float(amin), float(amax), clr, avoid_net)
        per_track.append(found)
        slots += found
        if sum(_extra(sl.amax, p)[0] for sl in found) >= need:
            # one straight run holds it all: keep the meander in one place (one
            # KiCad tuning pattern per net, the way the length tuner makes them)
            slots = found
            break
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
    pieces = []
    for track, bumps in by_track.values():
        before = track.GetLength()
        s_, e_ = track.GetStart(), track.GetEnd()
        base = (track.GetLayer(), s_.x, s_.y, e_.x, e_.y, track.GetWidth())
        items = _rebuild(board, room, track, bumps, p)
        gained = sum(i.GetLength() for i in items) - before
        added += gained
        new += items
        removed.append(track)
        pieces.append({"base": base, "items": items, "gained": gained, "spacing": int(p),
                       "amplitudes": [a for _, _, a in bumps]})
    lengthen.last_pieces = pieces
    return added, new, removed


def tune(board, groups: List[dict], status: Optional[Callable[[str], None]] = None,
         spacing_nm: int = 0, amin_nm: int = 200_000, amax_nm: int = 1_000_000,
         removed_sink: Optional[list] = None, kicad_patterns: bool = True,
         skip_nets: Optional[set] = None, rounded: bool = True) -> TuneReport:
    """Bring each group's nets to its target length (the longest member, or
    the group's own target) within its tolerance. groups: [{"name", "nets":
    [net names], "tolerance": nm, "target": nm or 0}]. Meander spacing and
    amplitudes default to KiCad's tuning pattern defaults. With kicad_patterns
    each tuned net's meander is wrapped in a KiCad tuning pattern (base line,
    target length window, spacing and amplitudes), so KiCad's length tuner owns
    it: it shows the pattern's handles and "Edit > Update All Tuning Patterns"
    rebuilds it with KiCad's own meander engine. skip_nets: names not tuned
    (pairs whose coupling was waived as unnecessary). rounded: the bumps'
    corners are arc fillets (KiCad's "round" corner style), else chamfers."""
    global ROUNDED
    ROUNDED = rounded
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
            if skip_nets and board.FindNet(c).GetNetname() in skip_nets:
                continue
            need = target - lengths[c]
            if need <= (tol if g.get("target") else tol / 2):
                continue                      # within the window (or longer: meanders cannot shorten)
            net = board.FindNet(c)
            clr = int(ds.m_MinClearance)
            try:
                clr = max(clr, int(ds.m_NetSettings.GetEffectiveNetClass(net.GetNetname()).GetClearance()))
            except Exception:
                pass
            lengthen.last_pieces = []
            added, new, removed = lengthen(board, room, c, need, clr, spacing_nm, amin_nm, amax_nm,
                                           avoid_net=partner.get(c, 0))
            if lengthen.last_pieces:
                # the largest meander becomes a KiCad tuning pattern for this net
                main = max(lengthen.last_pieces, key=lambda pc: pc["gained"])
                report.patterns.append({"net": net.GetNetname(), "base": main["base"],
                                        "members": [str(i.m_Uuid.AsString()) for i in main["items"]],
                                        "target": target, "tolerance": tol, "spacing": main["spacing"],
                                        "amin": amin_nm, "amax": max([amax_nm] + main["amplitudes"]),
                                        "length": lengths[c] + added, "corner": kicad_corner_percent(board),
                                        "rounded": rounded})
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
    if kicad_patterns and report.patterns:
        from . import kicad_tuning
        try:
            report.kicad_patterns = kicad_tuning.attach(board, report.patterns)
        except Exception as e:
            say(f"Could not make KiCad tuning patterns: {e!r}")
    return report
