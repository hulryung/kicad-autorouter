"""Reading the board into a RoutingProblem through pcbnew (main thread only)."""
import fnmatch
import math
import os
from array import array
import time
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

import pcbnew

from .grid import BLOCKED, Grid, jog
from .problem import (ExistingPath, Cluster, Connection, LayerInfo, NativeOptions, NetInfo, NetRules,
                      RoutedPath, RoutingProblem, SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS,
                      SIZE_AUTO, SIZE_CURRENT, SIZE_FIXED)

MARGIN_NM = 500_000


def _poly_rings(poly) -> List[Tuple[List[Tuple[int, int]], List[List[Tuple[int, int]]]]]:
    """[(outline points, [hole points, ...]), ...] of a SHAPE_POLY_SET."""
    out = []
    for i in range(poly.OutlineCount()):
        ring = poly.Outline(i)
        pts = [(ring.CPoint(k).x, ring.CPoint(k).y) for k in range(ring.PointCount())]
        holes = []
        for j in range(poly.HoleCount(i)):
            hole = poly.Hole(i, j)
            holes.append([(hole.CPoint(k).x, hole.CPoint(k).y) for k in range(hole.PointCount())])
        out.append((pts, holes))
    return out


def _box(shape) -> Tuple[int, int, int, int]:
    bb = shape.BBox()
    return bb.GetX(), bb.GetY(), bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight()


def board_sizes(board) -> dict:
    """KiCad's routing size settings: the net classes' defaults are per net, but
    the board also carries pre-defined track widths and via sizes (Board Setup
    > Pre-defined Sizes) and the editor's current track/via choice."""
    ds = board.GetDesignSettings()
    widths = sorted({int(w) for w in ds.m_TrackWidthList if int(w) > 0})
    vias = sorted({(int(v.m_Diameter), int(v.m_Drill)) for v in ds.m_ViasDimensionsList if int(v.m_Diameter) > 0})
    return {"track_widths": widths, "via_sizes": vias,
            "current_track_width": int(ds.GetCurrentTrackWidth()),
            "current_via": (int(ds.GetCurrentViaSize()), int(ds.GetCurrentViaDrill())),
            "custom_sizes": bool(ds.UseCustomTrackViaSize()),
            "min_track_width": int(ds.m_TrackMinWidth), "min_via": int(ds.m_ViasMinSize),
            "min_clearance": int(ds.m_MinClearance)}


class BoardHints:
    """What the board already says about each net's track width: the width
    of tracks it carries today and the narrowest pad it lands on."""
    def __init__(self, board):
        self.existing: Dict[int, Counter] = {}
        self.pad_min: Dict[int, int] = {}
        for item in board.GetTracks():
            if item.GetClass() == "PCB_TRACK" and item.GetNetCode() > 0:
                self.existing.setdefault(item.GetNetCode(), Counter())[int(item.GetWidth())] += 1
        for pad in board.GetPads():
            code = pad.GetNetCode()
            if code <= 0:
                continue
            size = pad.GetSize()
            short = min(int(size.x), int(size.y))
            if short > 0:
                self.pad_min[code] = min(self.pad_min.get(code, short), short)

    def existing_width(self, code: int) -> Optional[int]:
        counts = self.existing.get(code)
        return counts.most_common(1)[0][0] if counts else None


def auto_width(board, net, nc, hints: BoardHints, options: NativeOptions) -> int:
    """The width a designer would pick:
    1. tracks already on the board for this net: their usual width;
    2. a power net (by name): the widest pre-defined width that still fits the
       narrowest pad it lands on, never below the net class;
    3. otherwise the net class width (explicit classes included)."""
    code = net.GetNetCode()
    name = net.GetNetname()
    existing = hints.existing_width(code)
    if existing:
        return existing
    if any(fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(name.upper(), pat.upper())
           for pat in options.power_patterns):
        # power: as wide as the narrowest pad allows, never below the class
        sizes = board_sizes(board)
        pad = hints.pad_min.get(code, 0)
        fitting = [w for w in sizes["track_widths"] if w <= pad]
        if fitting:
            return max(max(fitting), nc.GetTrackWidth())
    return nc.GetTrackWidth()


def net_rules(board, net, options: Optional[NativeOptions] = None,
              hints: Optional[BoardHints] = None) -> NetRules:
    """Width, clearance and via of a net: the effective net class (which is
    how KiCad resolves netclass assignments and patterns), overridden by the
    editor's current sizes or a fixed size when the options say so, and never
    below the board's minimums."""
    ds = board.GetDesignSettings()
    nc = ds.m_NetSettings.GetEffectiveNetClass(net.GetNetname())
    width, via_d, via_drill = nc.GetTrackWidth(), nc.GetViaDiameter(), nc.GetViaDrill()
    if options is not None:
        if options.track_mode == SIZE_AUTO and hints is not None:
            width = auto_width(board, net, nc, hints, options)
        elif options.track_mode == SIZE_CURRENT:
            width = ds.GetCurrentTrackWidth()
        elif options.track_mode == SIZE_FIXED and options.track_width > 0:
            width = options.track_width
        if options.via_mode == SIZE_CURRENT:
            via_d, via_drill = ds.GetCurrentViaSize(), ds.GetCurrentViaDrill()
        elif options.via_mode == SIZE_FIXED and options.via_diameter > 0:
            via_d, via_drill = options.via_diameter, options.via_drill or nc.GetViaDrill()
    return NetRules(width=max(int(width), ds.m_TrackMinWidth),
                    clearance=max(nc.GetClearance(), ds.m_MinClearance),
                    via_diameter=max(int(via_d), ds.m_ViasMinSize),
                    via_drill=max(int(via_drill), 1))


def neck_width(board, rules: NetRules) -> int:
    """The narrowest pre-defined track width the board offers (not below the
    board minimum); the net's own width if there is nothing narrower."""
    sizes = board_sizes(board)
    candidates = [w for w in sizes["track_widths"] if sizes["min_track_width"] <= w < rules.width]
    return min(candidates) if candidates else rules.width


def tight_pads(fp, limit: int) -> Set[str]:
    """Pad numbers of a footprint that have another pad closer than limit
    (centre to centre): fine-pitch pins whose escapes need care."""
    pads = [(p.GetNumber(), p.GetPosition().x, p.GetPosition().y) for p in fp.Pads()]
    tight: Set[str] = set()
    limit2 = limit * limit
    for i, (ni, xi, yi) in enumerate(pads):
        for nj, xj, yj in pads[i + 1:]:
            if (xi - xj) ** 2 + (yi - yj) ** 2 < limit2:
                tight.add(ni)
                tight.add(nj)
    return tight


def pad_escape(pad, fp, rules: NetRules, pitch: int,
               towards: Optional[Tuple[int, int]] = None,
               then: Optional[Tuple[int, int]] = None, outward: bool = False) -> Optional[List[Tuple[int, int]]]:
    """Where a track may leave an SMD pad: points on the pad's long axis, just
    far enough past either pad end that the track is clear of the neighbouring
    pads (clearance + half width + one cell). The side facing `towards` (the
    nearest other pad of the net) comes first, else the side pointing away
    from the footprint centre. When `towards` sits beside the pad rather than
    off one of its ends (a USB-C receptacle's A6 next to B6), it says nothing
    about the side and `then` (the nearest pad of the net on another
    footprint) decides instead. With outward (a two-pin part: resistor,
    capacitor, LED), the side of the pad facing away from the part is an
    exit too: that is how a designer leaves a chip resistor, straight out of
    its end, rather than off the corners of the pad. None for through-hole
    pads, which can be entered from any side."""
    if pad.GetAttribute() not in (pcbnew.PAD_ATTRIB_SMD, pcbnew.PAD_ATTRIB_CONN):
        return None
    size = pad.GetSize()
    sx, sy = int(size.x), int(size.y)
    if sx <= 0 or sy <= 0:
        return None
    angle = math.radians(pad.GetOrientationDegrees())
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    if sx >= sy:
        axis, half = (cos_a, sin_a), sx / 2
    else:
        axis, half = (-sin_a, cos_a), sy / 2
    reach = half + rules.clearance + rules.width / 2 + pitch
    pos = pad.GetPosition()
    centre = fp.GetPosition() if fp is not None else pos
    if towards is not None and then is not None \
            and abs(axis[0] * (towards[0] - pos.x) + axis[1] * (towards[1] - pos.y)) <= pitch:
        towards = then
    dirs = [(axis[0], axis[1], reach), (-axis[0], -axis[1], reach)]
    if outward:
        # the short axis, on the side away from the part's centre
        nx, ny = -axis[1], axis[0]
        if nx * (pos.x - centre.x) + ny * (pos.y - centre.y) < 0:
            nx, ny = -nx, -ny
        short = min(sx, sy) / 2
        if abs(nx * (pos.x - centre.x) + ny * (pos.y - centre.y)) > short / 2:
            dirs.append((nx, ny, short + rules.clearance + rules.width / 2 + pitch))
    options = []
    for ux, uy, r in dirs:
        ex = pos.x + ux * r
        ey = pos.y + uy * r
        if towards is not None:
            score = -((ex - towards[0]) ** 2 + (ey - towards[1]) ** 2)   # nearer the target first
        else:
            score = (ex - centre.x) ** 2 + (ey - centre.y) ** 2          # away from the centre first
        options.append((score, int(round(ex)), int(round(ey))))
    options.sort(reverse=True)
    return [(ex, ey) for _, ex, ey in options]


_DEBUG = bool(os.environ.get("AUTOROUTER_DEBUG"))

OUTLET_NM = 600_000          # how far past a pad exit stays clear of other nets' vias and tracks

PAIR_SUFFIXES = (("+", "-"), ("_P", "_N"), ("_p", "_n"), ("DP", "DN"), ("D+", "D-"), ("P", "N"))


def pair_partner(name: str, names: Set[str]) -> Optional[str]:
    """The other net of a differential pair, found by the usual name
    conventions (USB_DP/USB_DN, TX+/TX-, RX_P/RX_N, D+/D-); None if none."""
    for a, b in PAIR_SUFFIXES:
        for first, second in ((a, b), (b, a)):
            if name.endswith(first) and len(name) > len(first):
                base = name[:-len(first)]
                if first in ("P", "N") and (not base or base[-1].isalpha() and base[-1] not in "DXR"):
                    continue                     # STOP/STON are not a pair
                other = base + second
                if other in names and other != name:
                    return other
    return None


def _pick_pair_ends(nets: Dict[int, NetInfo], connections: List[Connection]) -> None:
    """Choose which pads a differential pair's run between two footprints
    lands on. A USB-C receptacle has each line twice (A6/B6 for D+, A7/B7
    for D-), and the spanning tree picks whichever is nearest per net on its
    own: the two ends can come out 1.5 mm apart with the other pins between
    them, or swapped so the pair would have to cross. A designer takes the
    two adjacent pins that keep the pair's sides the same at both ends and
    bridges the duplicates at the connector; so here the cross-footprint
    connection of each net is moved to the pins that are closest to the
    partner's without crossing, where the spanning tree allows it."""

    def anchor(code, ci):
        return nets[code].clusters[ci].anchors[0]

    def dist(p, q):
        dx, dy = abs(p[0] - q[0]), abs(p[1] - q[1])
        return max(dx, dy) + min(dx, dy) // 2

    def crosses(p1, p2, q1, q2):
        def orient(a, b, c):
            v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            return (v > 0) - (v < 0)
        return (orient(p1, p2, q1) * orient(p1, p2, q2) < 0
                and orient(q1, q2, p1) * orient(q1, q2, p2) < 0)

    def side_of(code, conn_list, ca, end_a, end_b):
        """Is a tree still spanning with `ca` replaced by (end_a, end_b)?"""
        k = len(nets[code].clusters)
        parent = list(range(k))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for c in conn_list:
            if c is not ca:
                parent[find(c.a)] = find(c.b)
        return find(end_a) == find(ca.a) and find(end_b) == find(ca.b) and find(end_a) != find(end_b)

    by_net: Dict[int, List[Connection]] = {}
    for c in connections:
        by_net.setdefault(c.net, []).append(c)
    for code_a in sorted(by_net):
        net_a = nets[code_a]
        code_b = net_a.partner
        if not code_b or code_b < code_a or code_b not in by_net:
            continue
        net_b = nets[code_b]
        for ca in by_net[code_a]:
            ra1, ra2 = net_a.clusters[ca.a].refs, net_a.clusters[ca.b].refs
            if not ra1 or not ra2 or ra1 & ra2:
                continue                                   # a bridge within one footprint
            cb, flip = None, False
            for c in by_net[code_b]:
                rb1, rb2 = net_b.clusters[c.a].refs, net_b.clusters[c.b].refs
                if rb1 & rb2:
                    continue
                if ra1 & rb1 and ra2 & rb2:
                    cb, flip = c, False
                    break
                if ra1 & rb2 and ra2 & rb1:
                    cb, flip = c, True
                    break
            if cb is None:
                continue
            b1, b2 = (cb.b, cb.a) if flip else (cb.a, cb.b)
            # candidate pads at each end: the net's clusters on the shared footprints
            fp1 = ra1 & net_b.clusters[b1].refs
            fp2 = ra2 & net_b.clusters[b2].refs
            cand_a1 = [ca.a] + [i for i, cl in enumerate(net_a.clusters) if i != ca.a and cl.refs & fp1]
            cand_a2 = [ca.b] + [i for i, cl in enumerate(net_a.clusters) if i != ca.b and cl.refs & fp2]
            cand_b1 = [b1] + [i for i, cl in enumerate(net_b.clusters) if i != b1 and cl.refs & fp1]
            cand_b2 = [b2] + [i for i, cl in enumerate(net_b.clusters) if i != b2 and cl.refs & fp2]
            if len(cand_a1) * len(cand_a2) * len(cand_b1) * len(cand_b2) == 1:
                continue
            best = None
            for a1 in cand_a1:
                for a2 in cand_a2:
                    if (a1, a2) != (ca.a, ca.b) and not side_of(code_a, by_net[code_a], ca, a1, a2):
                        continue
                    for x1 in cand_b1:
                        for x2 in cand_b2:
                            if (x1, x2) != (b1, b2) and not side_of(code_b, by_net[code_b], cb, x1, x2):
                                continue
                            pa1, pa2 = anchor(code_a, a1), anchor(code_a, a2)
                            pb1, pb2 = anchor(code_b, x1), anchor(code_b, x2)
                            score = (crosses(pa1, pa2, pb1, pb2),
                                     dist(pa1, pb1) + dist(pa2, pb2),
                                     dist(pa1, pa2) + dist(pb1, pb2))
                            if best is None or score < best[0]:
                                best = (score, a1, a2, x1, x2)
            _, a1, a2, x1, x2 = best
            for conn, net, e1, e2 in ((ca, net_a, a1, a2), (cb, net_b, *((x2, x1) if flip else (x1, x2)))):
                if (e1, e2) == (conn.a, conn.b):
                    continue
                conn.a, conn.b = e1, e2
                conn.estimate = dist(net.clusters[e1].anchors[0], net.clusters[e2].anchors[0])
                conn.priority = 0 if (net.clusters[e1].tight or net.clusters[e2].tight) else 1



def _faces_row(fp, pad, pos, exit_pt, reach: int = 4_000_000) -> bool:
    """Does leaving the pad toward exit_pt head into another row of the
    footprint's pads close by (the inside of a two-row connector)?"""
    ux, uy = exit_pt[0] - pos.x, exit_pt[1] - pos.y
    norm = math.hypot(ux, uy)
    if not norm:
        return False
    ux, uy = ux / norm, uy / norm
    for other in fp.Pads():
        q = other.GetPosition()
        vx, vy = q.x - pos.x, q.y - pos.y
        along = vx * ux + vy * uy
        if norm < along <= reach and abs(vx * uy - vy * ux) < 1_000_000:
            return True
    return False

def _room_beyond(grid, own, layer: int, code: int, px: int, py: int, ex: int, ey: int, pitch: int,
                 depth_nm: int = 2_000_000, half_nm: int = 400_000, holders: Optional[dict] = None) -> float:
    """How open the board is past a pad exit: the share of free cells (or the
    net's own) in a 2 mm deep, 0.8 mm wide strip continuing the escape
    direction. Low when a neighbouring part sits right outside the lane.
    With `holders`, the owners met in the strip are collected there."""
    dx, dy = ex - px, ey - py
    n = math.hypot(dx, dy) or 1.0
    ux, uy = dx / n, dy / n
    depth, half = max(1, depth_nm // pitch), max(0, half_nm // pitch)
    free = total = 0
    ahead = depth                                  # free cells straight ahead on the lane centre
    for d in range(1, depth + 1):
        for o in range(-half, half + 1):
            x = ex + (ux * d - uy * o) * pitch
            y = ey + (uy * d + ux * o) * pitch
            cx, cy = grid.cell_x(int(x)), grid.cell_y(int(y))
            total += 1
            if grid.inside(cx, cy) and own[grid.index(layer, cx, cy)] in (0, code):
                free += 1
            else:
                if o == 0 and ahead == depth:
                    ahead = d - 1
                if holders is not None and grid.inside(cx, cy):
                    holders[own[grid.index(layer, cx, cy)]] = holders.get(own[grid.index(layer, cx, cy)], 0) + 1
    if holders is not None:
        holders["ahead"] = round(ahead / depth, 2)
    return free / total if total else 0.0


def _collides(shape, stub, clearance: int) -> bool:
    """Does a copper shape come within clearance of a stub (SHAPE_SEGMENT)?
    Not every SHAPE class accepts another shape in Python (SHAPE_CIRCLE only
    takes a SEG), so fall back to the stub's centreline plus half its width."""
    try:
        return bool(shape.Collide(stub, clearance))
    except TypeError:
        return bool(shape.Collide(stub.GetSeg(), clearance + stub.GetWidth() // 2))


def footprint_of(pad):
    parent = pad.GetParentFootprint()
    if parent is None:
        return None
    try:
        return pcbnew.Cast_to_FOOTPRINT(parent)
    except Exception:
        return parent


def sheet_of(fp) -> str:
    """The schematic sheet a footprint came from, e.g. '/Power/'; '/' is the root."""
    try:
        name = fp.GetSheetname()
    except Exception:
        name = ""
    return name or "/"


def sheets(board) -> List[Tuple[str, int]]:
    """(sheet name, footprint count), sorted by name."""
    counts: Dict[str, int] = {}
    for fp in board.GetFootprints():
        name = sheet_of(fp)
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items())


def selection_scope(board, items) -> Tuple[List[str], List[str]]:
    """Footprint references and net names covered by a set of selected board
    items: footprints and pads give footprints, tracks/vias/zones give nets."""
    refs, nets = set(), set()
    for item in items:
        cls = item.GetClass()
        if cls == "FOOTPRINT":
            refs.add(pcbnew.Cast_to_FOOTPRINT(item).GetReference())
        elif cls == "PAD":
            fp = footprint_of(pcbnew.Cast_to_PAD(item))
            if fp is not None:
                refs.add(fp.GetReference())
        else:
            try:
                code = item.GetNetCode()
                if code > 0:
                    nets.add(board.FindNet(code).GetNetname())
            except Exception:
                pass
    return sorted(refs), sorted(nets)


def copper_layers(board, options: NativeOptions) -> List[LayerInfo]:
    """Every enabled copper layer. Layers the options exclude stay in the
    model (a via goes through them, so their copper matters) but are marked
    not routable, so no track is searched on them."""
    stack = list(board.GetEnabledLayers().CuStack())
    layers = []
    for i, lid in enumerate(stack):
        routable = not (options.routable_layers and i < len(options.routable_layers) and not options.routable_layers[i])
        if options.horizontal_layers and i < len(options.horizontal_layers):
            horizontal = bool(options.horizontal_layers[i])
        else:
            horizontal = (i % 2 == 0)
        layers.append(LayerInfo(len(layers), lid, board.GetLayerName(lid), horizontal, routable))
    if not any(l.routable for l in layers):
        for l in layers:
            l.routable = True
    return layers



def ground_plane_layers(board) -> List[int]:
    """Indices (in the copper stack) of the inner layers that are a ground
    plane: a ground net's pour over most of the board. A track there cuts
    the return path of every signal above and below it, so they are left
    out of routing unless the designer asks for them."""
    try:
        box = board.GetBoardEdgesBoundingBox()
        area = float(box.GetWidth()) * float(box.GetHeight())
    except Exception:
        return []
    if area <= 0:
        return []
    stack = list(board.GetEnabledLayers().CuStack())
    out = []
    for i, lid in enumerate(stack):
        if i == 0 or i == len(stack) - 1:
            continue
        for zone in board.Zones():
            if zone.GetIsRuleArea() or zone.GetNetCode() <= 0 or not zone.IsOnLayer(lid):
                continue
            if "GND" not in zone.GetNetname().upper():
                continue
            try:
                covered = float(zone.Outline().Area()) / area
            except Exception:
                continue
            if covered >= 0.7:
                out.append(i)
                break
    return out

def _zone_covers(zone, x: int, y: int, margin: int = 0) -> bool:
    """Is the point inside (or within margin of) the zone's outline?"""
    try:
        return bool(zone.Outline().Collide(pcbnew.VECTOR2I(x, y), margin))
    except Exception:
        return False


def _plane_nets(board, layer_of: Dict[int, int]) -> Set[int]:
    """Net codes whose pour (on a routable layer) covers at least half of
    the net's pads, with a via's reach of slack."""
    zones_by_net: Dict[int, list] = {}
    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.GetNetCode() <= 0:
            continue
        if any(l in layer_of for l in zone.GetLayerSet().CuStack()):
            zones_by_net.setdefault(zone.GetNetCode(), []).append(zone)
    pads_by_net: Dict[int, list] = {}
    for pad in board.GetPads():
        if pad.GetNetCode() in zones_by_net:
            pads_by_net.setdefault(pad.GetNetCode(), []).append(pad.GetPosition())
    out: Set[int] = set()
    for code, zones in zones_by_net.items():
        pads = pads_by_net.get(code, [])
        if not pads:
            continue
        covered = sum(1 for pos in pads if any(_zone_covers(z, pos.x, pos.y, 1_500_000) for z in zones))
        if covered * 2 >= len(pads):
            out.add(code)
    return out


def _skip_net(net, options: NativeOptions) -> bool:
    name = net.GetNetname()
    if net.GetNetClassName() in options.ignore_net_classes:
        return True
    return any(fnmatch.fnmatchcase(name, pat) or name == pat for pat in options.skip_nets)


def _assign_groups(board, nets: Dict[int, NetInfo], options: NativeOptions, plane_codes: Set[int]) -> None:
    """Each routed net's routing group and its place in the routing order:
    pairs, nets on memory parts, high-speed names (and every net with an
    impedance or length rule), clocks, routed power nets, the rest."""
    from .. import si
    order = [g for g in (options.route_order or si.DEFAULT_ORDER) if g in si.DEFAULT_ORDER]
    order += [g for g in si.DEFAULT_ORDER if g not in order]
    rank = {g: i for i, g in enumerate(order)}
    patterns = {g: list(si.GROUP_PATTERNS.get(g, [])) + list(options.route_groups.get(g, []))
                for g in si.DEFAULT_ORDER}
    memory_codes: Set[int] = set()
    for fp in board.GetFootprints():
        if si.is_memory_part(fp):
            memory_codes |= {p.GetNetCode() for p in fp.Pads() if p.GetNetCode() > 0}
    tuned = {n for g in options.length_groups for n in g.get("nets", [])}
    for n in nets.values():
        power = any(fnmatch.fnmatchcase(n.name.upper(), pat.upper()) or
                    fnmatch.fnmatchcase(n.name.rsplit("/", 1)[-1].upper(), pat.upper())
                    for pat in options.power_patterns)
        if n.partner or si.matches(n.name, patterns[si.GROUP_PAIRS]):
            group = si.GROUP_PAIRS
        elif si.matches(n.name, patterns[si.GROUP_MEMORY]) or (n.code in memory_codes and not power):
            group = si.GROUP_MEMORY
        elif si.matches(n.name, patterns[si.GROUP_HIGH_SPEED]) or n.name in options.impedance or n.name in tuned:
            group = si.GROUP_HIGH_SPEED
        elif si.matches(n.name, patterns[si.GROUP_CLOCK]):
            group = si.GROUP_CLOCK
        elif power or si.matches(n.name, patterns[si.GROUP_POWER]):
            group = si.GROUP_POWER
        else:
            group = si.GROUP_SIGNAL
        if si.matches(n.name, patterns[si.GROUP_SIGNAL]):
            group = si.GROUP_SIGNAL                 # the user's override
        n.group, n.rank = group, rank[group]


def extract(board, options: NativeOptions, status=None) -> RoutingProblem:
    t0 = time.time()
    say = status or (lambda text: None)
    pitch = max(10_000, int(round(options.pitch_mm * 1_000_000)))
    layers = copper_layers(board, options)
    if not layers:
        raise RuntimeError("No routable copper layer is enabled.")
    layer_of: Dict[int, int] = {l.kicad_id: l.index for l in layers}

    outline = pcbnew.SHAPE_POLY_SET()
    if not board.GetBoardPolygonOutlines(outline, True) or outline.OutlineCount() == 0:
        # No usable Edge.Cuts: fall back to the bounding box of what is there.
        bb = board.GetBoardEdgesBoundingBox()
        if bb.GetWidth() <= 0 or bb.GetHeight() <= 0:
            bb = board.ComputeBoundingBox(False)
        if bb.GetWidth() <= 0 or bb.GetHeight() <= 0:
            raise RuntimeError("The board needs a closed outline on Edge.Cuts.")
        outline = pcbnew.SHAPE_POLY_SET()
        outline.NewOutline()
        for x, y in ((bb.GetX(), bb.GetY()), (bb.GetX() + bb.GetWidth(), bb.GetY()),
                     (bb.GetX() + bb.GetWidth(), bb.GetY() + bb.GetHeight()),
                     (bb.GetX(), bb.GetY() + bb.GetHeight())):
            outline.Append(x, y)
    bb = outline.BBox()
    ox, oy = bb.GetX() - MARGIN_NM, bb.GetY() - MARGIN_NM
    w = int(math.ceil((bb.GetWidth() + 2 * MARGIN_NM) / pitch)) + 1
    h = int(math.ceil((bb.GetHeight() + 2 * MARGIN_NM) / pitch)) + 1
    grid = Grid(ox, oy, pitch, w, h, len(layers))

    # Copper zones: a net with a pour on some layer is a plane net; its pads
    # on other layers can be tied to the plane with a via instead of a track.
    zone_layers: Dict[int, Set[int]] = {}
    for zone in board.Zones():
        if zone.GetIsRuleArea() or zone.GetNetCode() <= 0:
            continue
        for lid in zone.GetLayerSet().CuStack():
            if lid in layer_of:
                zone_layers.setdefault(zone.GetNetCode(), set()).add(layer_of[lid])

    # Nets, their rules and the owner maps those rules need. Skipped nets that
    # have a pour stay in the model (so their pads get escapes and vias) but
    # are not routed with tracks.
    say("Reading nets and design rules...")
    hints = BoardHints(board)
    nets: Dict[int, NetInfo] = {}
    # A net whose copper pour lies under at least half of its pads is a plane
    # (a +3V3 layer, a local power pour): its pads get vias to the pour and
    # no tracks are drawn, the way a designer uses a plane.
    plane_codes = _plane_nets(board, layer_of) if options.plane_nets else set()
    # the plane nets' pad vias an earlier pass of this job dropped: movable
    # pieces (a signal walled in by one may rip it up; the pour pass puts it
    # back), so their nets stay in the model even in a pass without pours
    job_items: Set[str] = set(options.movable_uuids)
    job_codes: Set[int] = set()
    if job_items:
        job_codes = {t.GetNetCode() for t in board.GetTracks() if str(t.m_Uuid.AsString()) in job_items}
    for code, net in board.GetNetsByNetcode().items():
        if code <= 0:
            continue
        skipped = _skip_net(net, options) or code in plane_codes
        if skipped and not (options.fanout_vias and code in zone_layers) and code not in job_codes:
            continue
        nets[code] = NetInfo(code, net.GetNetname(), net_rules(board, net, options, hints), route=not skipped)
    # Controlled impedance: the width each layer needs for the net's target.
    # The search clears for the widest of them; each segment is drawn at its
    # own layer's width. It overrides every other width choice (the finer
    # pass's neck width included): a 90 ohm pair at the wrong width is wrong.
    ds = board.GetDesignSettings()
    for n in nets.values():
        z = options.impedance.get(n.name)
        if not z or not n.route:
            continue
        per_layer = {l.index: int(z["widths"][l.kicad_id]) for l in layers
                     if l.routable and l.kicad_id in z["widths"]}
        if not per_layer:
            continue
        n.layer_widths = per_layer
        n.rules = NetRules(max(max(per_layer.values()), ds.m_TrackMinWidth), n.rules.clearance,
                           n.rules.via_diameter, n.rules.via_drill)
    # Differential pairs: both nets take the net class's pair width and gap
    # (the designer's impedance choice in Board Setup) and are routed together.
    ds = board.GetDesignSettings()
    if options.diff_pairs:
        by_name = {n.name: n for n in nets.values() if n.route}
        for n in list(by_name.values()):
            if n.partner:
                continue
            other = pair_partner(n.name, set(by_name))
            if other is None or by_name[other].partner:
                continue
            m = by_name[other]
            nc = ds.m_NetSettings.GetEffectiveNetClass(n.name)
            width = nc.GetDiffPairWidth() if nc.GetDiffPairWidth() > 0 else n.rules.width
            gap = max(nc.GetDiffPairGap(), n.rules.clearance)
            z = options.impedance.get(n.name)
            if z and z.get("diff"):
                # the coupled run goes on the first routable layer: its sizes
                top = next((l for l in layers if l.routable), layers[0])
                width = int(z["widths"].get(top.kicad_id, width))
                gap = int(z["gaps"].get(top.kicad_id, gap))
            for a, b in ((n, m), (m, n)):
                a.partner = b.code
                a.pair_gap = gap
                a.pair_width = max(width, ds.m_TrackMinWidth)
                a.rules = NetRules(max(width, max(a.layer_widths.values() or [0]), ds.m_TrackMinWidth),
                                   a.rules.clearance, a.rules.via_diameter, a.rules.via_drill)
    _assign_groups(board, nets, options, plane_codes)
    # Connections that end on fine-pitch pins are routed at the board's
    # narrowest pre-defined width (the neck width), as a designer would, so
    # the escapes and the tracks between the pins have room. Those widths
    # need owner maps of their own.
    keys = {n.rules.key for n in nets.values()}
    for n in nets.values():
        if n.partner:
            keys.add((2 * (n.pair_width or n.rules.width) + n.pair_gap, n.rules.clearance))
    if options.neck_down:
        for n in nets.values():
            narrow = neck_width(board, n.rules)
            if narrow < n.rules.width:
                keys.add((narrow, n.rules.clearance))
    keys = sorted(keys) or [(board.GetDesignSettings().m_TrackMinWidth, board.GetDesignSettings().m_MinClearance)]
    owners = {key: grid.new_owner_map() for key in keys}
    via_block = bytearray(grid.plane)
    ds = board.GetDesignSettings()
    edge_clr = max(ds.m_CopperEdgeClearance, 0)

    hole_clr = max(getattr(ds, "m_HoleClearance", 0), 0)
    hole_to_hole = max(getattr(ds, "m_HoleToHoleMin", 0), 0)
    pth_clr = int(getattr(options, "pth_hole_clearance_mm", 0.0) * 1_000_000)
    max_via_drill = max([n.rules.via_drill for n in nets.values()] or [0])

    def block_vias_near_hole(x: int, y: int, hole_radius: int) -> None:
        """No via may drill within the hole-to-hole minimum of this hole."""
        r = hole_radius + hole_to_hole + max_via_drill // 2
        for idx in grid.disc_cells(0, x, y, r):
            via_block[idx] = 1

    def clearance_of(code: int) -> int:
        return nets[code].rules.clearance if code in nets else ds.m_MinClearance

    def item_clearance(item, kicad_layer: int, code: int) -> int:
        """The clearance KiCad applies to this item: local overrides and net
        class included, which is what the DRC will measure against."""
        try:
            own = item.GetOwnClearance(kicad_layer)
        except Exception:
            own = 0
        return max(own, clearance_of(code))

    # Paths run between cell centres, and a diagonal step passes up to a few
    # micrometres closer to an obstacle than either of its end cells. A small
    # safety margin on every obstacle keeps the DRC exact.
    safety = max(pitch // 5, 10_000)

    def claim(layer: int, code: int, cells_for, own_clr: Optional[int] = None) -> None:
        """cells_for(r) -> cells; one radius per owner map."""
        if own_clr is None:
            own_clr = clearance_of(code)
        for (width, clearance), own in owners.items():
            r = max(clearance, own_clr) + width // 2 + safety
            grid.mark(own, cells_for(r), code if code > 0 and code in nets else BLOCKED)

    # Board edge: everything outside, plus the edge clearance band.
    say("Rasterizing the board outline...")
    rings = _poly_rings(outline)
    main_outline, holes = rings[0]
    for extra, _ in rings[1:]:
        pass                                            # multiple outlines: route inside the first
    for layer in layers:
        for (width, clearance), own in owners.items():
            cells = grid.outside_polygon_cells(layer.index, main_outline, holes, edge_clr + width // 2)
            grid.mark(own, cells, BLOCKED)
    for idx in range(grid.plane):
        if owners[keys[0]][idx] == BLOCKED:
            via_block[idx] = 1

    # Temporary keepouts for this run (the assistant's "avoid" areas).
    for x0, y0, x1, y1 in options.keepouts:
        for layer in layers:
            cells = grid.cells_where(layer.index, (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                                     lambda x, y: True)
            for own in owners.values():
                grid.mark(own, cells, BLOCKED)
            for idx in cells:
                via_block[idx % grid.plane] = 1

    # Rule areas (keepouts).
    for zone in board.Zones():
        if not zone.GetIsRuleArea():
            continue
        zl = [layer_of[l] for l in zone.GetLayerSet().CuStack() if l in layer_of]
        if not zl:
            continue
        zrings = _poly_rings(zone.Outline())
        if zone.GetDoNotAllowTracks():
            for pts, zholes in zrings:
                for li in zl:
                    claim(li, 0, lambda r, pts=pts, zholes=zholes, li=li:
                          grid.polygon_cells(li, pts, zholes, r))
        if zone.GetDoNotAllowVias():
            for pts, zholes in zrings:
                for idx in grid.polygon_cells(0, pts, zholes, 0):
                    via_block[idx] = 1

    # Pads: obstacles for other nets, targets for their own.
    say("Rasterizing pads...")
    pad_cells: Dict[int, List[Tuple[object, Set[int], Tuple[int, int], str]]] = {}
    tight_by_fp: Dict[int, Set[str]] = {}
    pending = []
    pad_copper: Dict[Tuple[int, int, int], Set[int]] = {}   # (net, pad x, pad y) -> cells inside its copper
    # Every piece of copper as a KiCad shape, in 2 mm buckets per layer, for
    # exact collision tests when planning pad escapes.
    BUCKET = 2_000_000
    copper: Dict[Tuple[int, int, int], List[Tuple[object, int, int]]] = {}

    def add_copper(layer: int, shape, code: int, clr: int, box) -> None:
        bx0, by0 = box[0] // BUCKET, box[1] // BUCKET
        bx1, by1 = box[2] // BUCKET, box[3] // BUCKET
        for bx in range(bx0, bx1 + 1):
            for by in range(by0, by1 + 1):
                copper.setdefault((layer, bx, by), []).append((shape, code, clr))

    def copper_near(layer: int, box) -> List[Tuple[object, int, int]]:
        out = []
        for bx in range(box[0] // BUCKET, box[2] // BUCKET + 1):
            for by in range(box[1] // BUCKET, box[3] // BUCKET + 1):
                out.extend(copper.get((layer, bx, by), ()))
        return out
    for pad in board.GetPads():
        code = pad.GetNetCode()
        pl = [layer_of[l] for l in pad.GetLayerSet().CuStack() if l in layer_of]
        attr = pad.GetAttribute()
        if attr in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
            pl = [l.index for l in layers]               # a hole goes through every layer
        if not pl:
            continue
        pos = pad.GetPosition()
        centre = (grid.cell_x(pos.x), grid.cell_y(pos.y))
        own_cells: Set[int] = set()
        drill = pad.GetDrillSize()
        hole_radius = max(drill.x, drill.y) // 2 if attr in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH) else 0
        for li in pl:
            kicad_layer = layers[li].kicad_id
            shape = pad.GetEffectiveShape(kicad_layer)
            box = _box(shape)
            clr = item_clearance(pad, kicad_layer, code)
            add_copper(li, shape, code if code in nets else 0, clr, box)
            claim(li, code, lambda r, shape=shape, box=box, li=li:
                  grid.cells_where(li, (box[0] - r, box[1] - r, box[2] + r, box[3] + r),
                                   lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), r)), clr)
            if hole_radius:
                # the hole itself, under the hole-clearance rule, on every layer
                # (inner layers may carry no annular ring at all)
                claim(li, code, lambda r, li=li: grid.disc_cells(li, pos.x, pos.y, hole_radius + r),
                      max(clr, hole_clr, pth_clr if attr == pcbnew.PAD_ATTRIB_PTH else 0))
            if code in nets:
                inside = grid.cells_where(li, box, lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), 0))
                own_cells.update(inside)
                if grid.inside(*centre):
                    own_cells.add(grid.index(li, *centre))
        if hole_radius:
            block_vias_near_hole(pos.x, pos.y, hole_radius)
            if attr == pcbnew.PAD_ATTRIB_PTH and pth_clr and pl:
                # the fab's plated-hole rule binds both ways: a via's hole keeps
                # that far from the plated pad's copper too, which can be more
                # than the copper clearance the via barrel already keeps
                r_hole = pth_clr + max_via_drill // 2 + safety
                r_copper = max(n.rules.via_diameter for n in nets.values()) // 2 + clearance_of(code) if nets else 0
                if r_hole > r_copper:
                    kicad_layer = layers[pl[0]].kicad_id
                    shape = pad.GetEffectiveShape(kicad_layer)
                    box = _box(shape)
                    for idx in grid.cells_where(0, (box[0] - r_hole, box[1] - r_hole, box[2] + r_hole, box[3] + r_hole),
                                                lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), r_hole)):
                        via_block[idx] = 1
        if code in nets:
            pending.append((pad, code, pl, pos, own_cells, centre))
            pad_copper.setdefault((code, pos.x, pos.y), set()).update(own_cells)

    # Existing tracks and vias.
    say("Rasterizing existing tracks...")
    existing_vias: Dict[int, List[Tuple[int, int]]] = {}     # net code -> via centres, for idempotent pours
    movable: Dict[int, List] = {}      # shove mode: net code -> unlocked tracks and vias the router may move
    fixed_tracks: Dict[int, List] = {} # ...and the locked ones of those nets (they still join pads)
    for item in board.GetTracks():
        code = item.GetNetCode()
        cls = item.GetClass()
        if cls == "PCB_VIA":
            pos = item.GetPosition()
            existing_vias.setdefault(code, []).append((pos.x, pos.y))
        # shove mode: an unlocked track of a routable net enters the model as a
        # movable path (claimed by the router, rip-up-able), but stays in the
        # exact-shape copper index so pad escapes are planned around it
        is_movable = False
        if options.shove and code in nets and nets[code].route and cls in ("PCB_TRACK", "PCB_VIA"):
            if item.IsLocked():
                fixed_tracks.setdefault(code, []).append(item)
            else:
                movable.setdefault(code, []).append(item)
                is_movable = True
        elif job_items and code in nets and not nets[code].route and cls in ("PCB_TRACK", "PCB_VIA") \
                and str(item.m_Uuid.AsString()) in job_items:
            movable.setdefault(code, []).append(item)
            is_movable = True
        if cls == "PCB_VIA":
            vl = [layer_of[l] for l in item.GetLayerSet().CuStack() if l in layer_of]
            hole = item.GetDrillValue() // 2
            for li in vl:
                kicad_layer = layers[li].kicad_id
                try:
                    radius = item.GetWidth(kicad_layer) // 2      # KiCad 9+: per-layer via size
                except TypeError:
                    radius = item.GetWidth() // 2
                clr = item_clearance(item, kicad_layer, code)
                add_copper(li, pcbnew.SHAPE_CIRCLE(pcbnew.VECTOR2I(pos.x, pos.y), radius), code if code in nets else 0, clr,
                           (pos.x - radius, pos.y - radius, pos.x + radius, pos.y + radius))
                if not is_movable:
                    claim(li, code, lambda r, li=li, radius=radius: grid.disc_cells(li, pos.x, pos.y, radius + r), clr)
            if is_movable:
                continue
            for layer in layers:                         # the hole goes through everything
                claim(layer.index, code, lambda r, li=layer.index: grid.disc_cells(li, pos.x, pos.y, hole + r),
                      max(hole_clr, clearance_of(code)))
            block_vias_near_hole(pos.x, pos.y, hole)
        else:
            li = layer_of.get(item.GetLayer())
            if li is None:
                continue
            shape = item.GetEffectiveShape()
            box = _box(shape)
            add_copper(li, shape, code if code in nets else 0, item_clearance(item, item.GetLayer(), code), box)
            if is_movable:
                continue
            claim(li, code, lambda r, shape=shape, box=box, li=li:
                  grid.cells_where(li, (box[0] - r, box[1] - r, box[2] + r, box[3] + r),
                                   lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), r)),
                  item_clearance(item, item.GetLayer(), code))

    # Copper graphics and text (board and footprint level): netless obstacles.
    say("Rasterizing copper graphics...")
    items = list(board.GetDrawings())
    for fp in board.GetFootprints():
        items.extend(fp.GraphicalItems())
    for item in items:
        for layer in layers:
            if not item.IsOnLayer(layer.kicad_id):
                continue
            try:
                shape = item.GetEffectiveShape(layer.kicad_id)
            except TypeError:
                shape = item.GetEffectiveShape()
            box = _box(shape)
            if box[2] - box[0] > 50_000_000 and box[3] - box[1] > 50_000_000:
                continue                                 # a huge frame: not a routing obstacle
            add_copper(layer.index, shape, 0, ds.m_MinClearance, box)
            claim(layer.index, 0, lambda r, shape=shape, box=box, li=layer.index:
                  grid.cells_where(li, (box[0] - r, box[1] - r, box[2] + r, box[3] + r),
                                   lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), r)))

    # Pad escapes, now that every other piece of copper is on the maps: a stub
    # from the pad centre along the pad axis to an exit point, used only when
    # that lane is free of other nets' copper.
    say("Planning pad escapes...")
    problem_escape_sides: Dict[Tuple[str, str], Tuple[int, int]] = {}
    escape_stats: Dict[str, int] = {"straight": 0, "jog": 0, "jog_at_end": 0, "lane_blocked": 0}
    exit_owner: Dict[int, int] = {}            # exit cell -> net, to keep lanes apart
    outlets: List[Tuple[int, int, int, int, int, int]] = []   # (net, pad x, pad y, exit x, exit y, stub width)
    # Other nets' track centrelines keep out of the first 0.6 mm past an exit
    # (per layer cell: the net it is kept for), so a neighbour's track hugging
    # the pad row cannot wall a pin in. Clearance zones may still overlap it.
    lane_guard = array("i", [0]) * grid.size
    max_width = max([n.rules.width for n in nets.values()] or [0])
    positions_by_net: Dict[int, List[Tuple[int, int]]] = {}
    refs_by_net: Dict[int, List[str]] = {}
    for pad, code, pl, pos, own_cells, centre in pending:
        positions_by_net.setdefault(code, []).append((pos.x, pos.y))
        fp = footprint_of(pad)
        refs_by_net.setdefault(code, []).append(fp.GetReference() if fp else "")
    board_conn = board.GetConnectivity()
    # A differential pair leaves a part side by side: the side its partner's
    # pin on the same footprint was given (the first of the two decides by
    # room), per (footprint, net): unit direction from the pad to its exit.
    # Chosen one pin at a time by local room, the two pins of a pair can be
    # sent opposite ways (one under the body, one out), and the coupled run
    # then has no common side to start from.
    pair_sides: Dict[Tuple[str, int], Tuple[float, float]] = {}

    def _already_wired(pad) -> bool:
        """Is the pad joined to tracks or vias on the board already? Its exit
        then needs no outlet reservation (in shove mode those tracks may
        move, so every pad keeps one)."""
        if options.shove:
            return False
        try:
            return any(item.GetClass() in ("PCB_TRACK", "PCB_VIA", "PCB_ARC") for item in board_conn.GetConnectedItems(pad))
        except Exception:
            return False

    def _touched_by_job(pad) -> bool:
        """Is the pad joined to a track or via an earlier pass of this job dropped?"""
        try:
            return any(str(item.m_Uuid.AsString()) in job_items for item in board_conn.GetConnectedItems(pad))
        except Exception:
            return False

    for pad, code, pl, pos, own_cells, centre in pending:
        fp = footprint_of(pad)
        net = nets[code]
        if not net.route and not options.fanout_vias and not (job_items and _touched_by_job(pad)):
            # a plane net in the model only for its movable vias: a pad with
            # none of them gets no escape lane (those exist for the via alone)
            pad_cells.setdefault(code, []).append((pad, own_cells, centre, fp.GetReference() if fp else "", False))
            continue
        own = owners[net.rules.key]
        wants_outlet = net.route and not _already_wired(pad)
        stub_w = net.rules.width
        others = [q for q in positions_by_net[code] if q != (pos.x, pos.y)]
        towards = min(others, key=lambda q: (q[0] - pos.x) ** 2 + (q[1] - pos.y) ** 2) if others else None
        ref = fp.GetReference() if fp else ""
        abroad = [q for q, r in zip(positions_by_net[code], refs_by_net[code]) if r != ref]
        then = min(abroad, key=lambda q: (q[0] - pos.x) ** 2 + (q[1] - pos.y) ** 2) if abroad else None
        two_pin = options.outward_exits and fp is not None and sum(1 for _ in fp.Pads()) <= 2
        escapes = pad_escape(pad, fp, net.rules, pitch, towards, then, outward=two_pin)
        if escapes and fp is not None and options.neck_down:
            key = fp.GetReference()
            if key not in tight_by_fp:
                # neighbours closer than width + 2 * clearance + a cell of slack
                tight_by_fp[key] = tight_pads(fp, net.rules.width + 2 * net.rules.clearance + pitch + 60_000)
            if pad.GetNumber() in tight_by_fp[key]:
                stub_w = neck_width(board, net.rules)
        pad_key = (fp.GetReference() if fp else "", pad.GetNumber())
        remembered = options.escape_sides.get(pad_key)
        partner_side = pair_sides.get((ref, net.partner)) if (net.partner and ref and escapes) else None
        if partner_side is not None:
            # the escape running the partner's way, if this pad has one
            def _along(e):
                dx, dy = e[0] - pos.x, e[1] - pos.y
                norm = math.hypot(dx, dy) or 1.0
                return (dx * partner_side[0] + dy * partner_side[1]) / norm
            if max(_along(e) for e in escapes) < 0.7:
                partner_side = None
        if escapes and len(escapes) >= 2 and remembered is not None:
            # an earlier pass of this job chose the side before its vias went
            # in; keep it, so those vias do not flip the choice
            escapes.sort(key=lambda e: (e[0] - remembered[0]) ** 2 + (e[1] - remembered[1]) ** 2)
        elif escapes and len(escapes) >= 2 and partner_side is not None:
            escapes.sort(key=lambda e: -_along(e))
            if _DEBUG:
                print(f"[escape] {net.name} {ref}.{pad.GetNumber()} leaves with its pair partner: "
                      f"({escapes[0][0] / 1e6:.2f},{escapes[0][1] / 1e6:.2f})", flush=True)
        elif escapes and len(escapes) == 2:
            # The side the nearest pad of the net points to comes first, but
            # not when the lane beyond it is boxed in (a crystal or resistor
            # right outside a QFP row) while the other side is open: a
            # designer takes the open side, often under the body to a via.
            holders = [{}, {}]
            room = [_room_beyond(grid, own, pl[0], code, pos.x, pos.y, ex, ey, pitch, holders=h)
                    for (ex, ey), h in zip(escapes, holders)]
            if _DEBUG:
                print(f"[room] {net.name} {fp.GetReference() if fp else '?'}.{pad.GetNumber()} "
                      + " | ".join(f"({ex / 1e6:.2f},{ey / 1e6:.2f}) room {r:.2f} "
                                   + str({(nets[h].name if h in nets else h): c for h, c in hd.items()})
                                   for (ex, ey), r, hd in zip(escapes, room, holders)), flush=True)
            if room[0] < 0.6 and room[1] > room[0] + 0.25:
                escapes.reverse()
            elif fp is not None and room[1] >= 0.8 and _faces_row(fp, pad, pos, escapes[0]) \
                    and not _faces_row(fp, pad, pos, escapes[1]):
                # a two-row connector: the channel between the rows is the
                # other row's way out too; the pins leave outward when they can
                escapes.reverse()
        if escapes:
            problem_escape_sides[pad_key] = escapes[0]
            if net.partner and ref and (ref, code) not in pair_sides:
                dx, dy = escapes[0][0] - pos.x, escapes[0][1] - pos.y
                norm = math.hypot(dx, dy) or 1.0
                pair_sides[(ref, code)] = (dx / norm, dy / norm)
        exit_cells: Set[int] = set()
        chosen: List[Tuple[int, int, int, int, int, int, list]] = []
        for ex, ey in (escapes or []):
            # Snap the exit to the grid by flooring on both axes, so the pins
            # of one row all shift the same way and keep their pitch.
            ecx = int(math.floor((ex - grid.ox) / pitch))
            ecy = int(math.floor((ey - grid.oy) / pitch))
            if not grid.inside(ecx, ecy):
                continue
            gx, gy = grid.nm_x(ecx), grid.nm_y(ecy)
            # the grid search must be able to start on the exit cell
            if any(own[grid.index(li, ecx, ecy)] not in (0, code) for li in pl):
                if _DEBUG:
                    holder = [own[grid.index(li, ecx, ecy)] for li in pl]
                    print(f"[escape] {net.name} {fp.GetReference() if fp else '?'}.{pad.GetNumber()} exit cell "
                          f"({gx / 1e6:.2f},{gy / 1e6:.2f}) taken by {[nets[h].name if h in nets else h for h in holder]}", flush=True)
                continue
            # The stub is checked exactly, as the DRC would (the escape lane may
            # legally be narrower than what the owner maps assume). A single
            # straight piece from the pad centre to the grid cell is preferred,
            # as a designer would draw it; if that grazes a neighbour, the
            # on-axis piece plus a short jog is tried.
            margin = stub_w // 2 + net.rules.clearance + 2 * safety
            lane_box = (min(pos.x, ex, gx) - margin, min(pos.y, ey, gy) - margin,
                        max(pos.x, ex, gx) + margin, max(pos.y, ey, gy) + margin)
            nearby = [(shape, other, other_clr) for li in pl
                      for shape, other, other_clr in copper_near(li, lane_box) if other != code]

            def lane_clear(shapes):
                for shape, other, other_clr in nearby:
                    need = max(net.rules.clearance, other_clr) + safety
                    if any(_collides(shape, stub, need) for stub in shapes):
                        if _DEBUG:
                            print(f"[escape] {net.name} {fp.GetReference() if fp else '?'}.{pad.GetNumber()} lane to "
                                  f"({gx / 1e6:.2f},{gy / 1e6:.2f}) grazes {nets[other].name if other in nets else other}", flush=True)
                        return False
                return True

            # The stub runs at 0, 45 or 90 degrees like hand-drawn copper: the
            # grid cell is seldom exactly on the pad axis, so the small offset
            # is taken up by a 45-degree jog, hidden inside the pad when the
            # lane allows it, else at the exit end.
            stub_shapes = None
            for at_end in (False, True):
                pts = jog(pos.x, pos.y, gx, gy, at_end=at_end)
                shapes = [pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(*pts[k]), pcbnew.VECTOR2I(*pts[k + 1]), stub_w)
                          for k in range(len(pts) - 1)]
                if lane_clear(shapes):
                    stub_shapes = shapes
                    ex, ey = pts[1] if len(pts) == 3 else (gx, gy)    # the exit point: the jog, or the cell
                    kind = "jog_at_end" if at_end else ("jog" if len(pts) == 3 else "straight")
                    escape_stats[kind] += 1
                    if _DEBUG:
                        print(f"[escape] {net.name} {fp.GetReference() if fp else '?'}.{pad.GetNumber()} {kind} "
                              f"pad ({pos.x / 1e6:.3f},{pos.y / 1e6:.3f}) cell ({gx / 1e6:.2f},{gy / 1e6:.2f})", flush=True)
                    break
            if stub_shapes is None:
                escape_stats["lane_blocked"] += 1
                if _DEBUG:
                    print(f"[escape] {net.name} {fp.GetReference() if fp else '?'}.{pad.GetNumber()} lane blocked "
                          f"pad ({pos.x / 1e6:.3f},{pos.y / 1e6:.3f}) cell ({gx / 1e6:.2f},{gy / 1e6:.2f})", flush=True)
                continue
            # and this lane's clearance zone must not cover another net's exit
            width, clearance = net.rules.key
            zone_r = max(clearance, ds.m_MinClearance) + width // 2 + safety + stub_w // 2
            zone = []
            for li in pl:
                zone += grid.segment_cells(li, pos.x, pos.y, ex, ey, zone_r)
                zone += grid.segment_cells(li, ex, ey, gx, gy, zone_r)
            if any(exit_owner.get(idx, code) != code for idx in zone):
                if _DEBUG:
                    print(f"[escape] {net.name} {fp.GetReference() if fp else '?'}.{pad.GetNumber()} exit "
                          f"({gx / 1e6:.2f},{gy / 1e6:.2f}) covers another net's exit", flush=True)
                continue
            chosen.append((ex, ey, ecx, ecy, gx, gy, stub_shapes))
            # plane-net pads keep both exits: the fanout via goes to whichever
            # side still has room for it once the signals are routed (in a
            # pass without pours a pad is here for its movable via alone: one
            # exit, the via's side, so the other lane stays open for signals)
            if not (options.dual_escapes or (not net.route and code in zone_layers and options.fanout_vias)):
                break
        if chosen:
            half = stub_w // 2
            # With dual escapes (the pipeline's pass for the nets left open)
            # both sides are exits and the search leaves by whichever is
            # cheaper; only the stub of the exit a route uses is written.
            for ex, ey, ecx, ecy, gx, gy, stub_shapes in chosen:
                for li in pl:
                    idx = grid.index(li, ecx, ecy)
                    exit_cells.add(idx)
                    exit_owner[idx] = code
                    net.endpoints[idx] = (ex, ey, pos.x, pos.y, stub_w)
                    # The stub (pad centre -> exit point -> grid cell) is this net's
                    # copper, with the track's own half width: nobody else may come
                    # within clearance of the escape lane.
                    claim(li, code, lambda r, li=li, ex=ex, ey=ey, gx=gx, gy=gy, half=half:
                          grid.segment_cells(li, pos.x, pos.y, ex, ey, r + half)
                          + grid.segment_cells(li, ex, ey, gx, gy, r + half))
                    for stub in stub_shapes:
                        add_copper(li, stub, code, net.rules.clearance, lane_box)
                    # ...and the first 0.6 mm past the exit is reserved for this
                    # net where it is still free: a neighbour's track hugging
                    # the pad row would otherwise wall the pin in.
                    if wants_outlet and towards is not None:
                        dx, dy = gx - pos.x, gy - pos.y
                        norm = math.hypot(dx, dy) or 1.0
                        fx, fy = int(round(gx + dx / norm * OUTLET_NM)), int(round(gy + dy / norm * OUTLET_NM))
                        r = max(net.rules.clearance, ds.m_MinClearance) + max_width // 2 + safety + half
                        for idx in grid.segment_cells(li, gx, gy, fx, fy, r):
                            cur = lane_guard[idx]
                            lane_guard[idx] = code if cur in (0, code) else -1   # two outlets: nobody's lane
                if wants_outlet and towards is not None:
                    outlets.append((code, pos.x, pos.y, gx, gy, stub_w))
                    # The outlet's first exit is also where the pin drops its
                    # via: keep other nets' centrelines out of that spot on
                    # the layers the pad is not on, or an inner-layer track
                    # running across the pin row leaves the pin no via site.
                    ex, ey, ecx, ecy, gx, gy, _ = chosen[0]
                    dx, dy = gx - pos.x, gy - pos.y
                    norm = math.hypot(dx, dy) or 1.0
                    fx, fy = int(round(gx + dx / norm * OUTLET_NM)), int(round(gy + dy / norm * OUTLET_NM))
                    r = max(net.rules.clearance, ds.m_MinClearance) + max_width // 2 + safety + half
                    for li in range(grid.layers):
                        if li in pl:
                            continue
                        for idx in grid.segment_cells(li, gx, gy, fx, fy, r):
                            cur = lane_guard[idx]
                            lane_guard[idx] = code if cur in (0, code) else -1
            target_cells = exit_cells
            centre = (chosen[0][2], chosen[0][3])
        else:
            target_cells = own_cells
            # No escape lane: the route leaves from the cell at the pad
            # centre, which is seldom on the pad axis. A fine-pitch pin keeps
            # the neck width there too, or a power track as wide as the pin
            # sits off-centre and grazes the neighbouring pad.
            for li in pl:
                if grid.inside(*centre):
                    net.endpoints[grid.index(li, *centre)] = (pos.x, pos.y, pos.x, pos.y, stub_w)
        pad_cells.setdefault(code, []).append((pad, target_cells, centre, fp.GetReference() if fp else "",
                                               stub_w < net.rules.width))

    # Outlet guards: the first 0.6 mm past each exit must stay free of other
    # nets' via barrels (and their holes), or a neighbour's via dropped right
    # past the exit boxes the pin in (a QFP pin whose escape then has nowhere
    # to go). Tracks may still pass there at normal clearance.
    via_guard = array("i", [0]) * grid.plane
    max_via_d = max([n.rules.via_diameter for n in nets.values()] or [0])
    outlet_len = OUTLET_NM
    for code, px, py, gx, gy, stub_w in outlets:
        dx, dy = gx - px, gy - py
        norm = math.hypot(dx, dy) or 1.0
        fx, fy = int(round(gx + dx / norm * outlet_len)), int(round(gy + dy / norm * outlet_len))
        reach = max(max_via_d // 2 + clearance_of(code), max_via_drill // 2 + hole_clr) + stub_w // 2 + safety + pitch
        for idx in grid.segment_cells(0, gx, gy, fx, fy, reach):
            cur = via_guard[idx]
            via_guard[idx] = code if cur in (0, code) else -1

    conn = board.GetConnectivity()

    def _pour_reaches(pad) -> bool:
        """Is this pad already joined to a filled zone of its net (as the
        board's connectivity sees it, i.e. with the current fills)?"""
        try:
            for item in conn.GetConnectedItems(pad):
                if item.GetClass() == "ZONE":
                    return True
        except Exception:
            pass
        return False

    # Fanout candidates: pads of pour nets that are not routed with tracks and
    # that the pour on their own layer does not reach. The router drops a via
    # for each of them after the signal routing, where there is room left (a
    # via placed first would sit in the escape lanes the signals need).
    fanout: List[Tuple[int, int]] = []           # (net code, exit cell)
    fanout_alt: Dict[int, List[int]] = {}        # exit cell -> the same pad's other exits
    fanout_goals: Dict[int, Set[int]] = {}       # exit cell -> cells on the net's pour layers within 3 mm

    def _via_near(code: int, x: int, y: int, within: int) -> bool:
        """Is a via of this net already within the distance (nm)? Then a
        fanout or stitch there would only stack on it (a second run of the
        router, or an AI route call, must not add vias again)."""
        for vx, vy in existing_vias.get(code, ()):
            if abs(vx - x) <= within and abs(vy - y) <= within and (vx - x) ** 2 + (vy - y) ** 2 <= within * within:
                return True
        return False

    def _has_via(pad) -> bool:
        """Is the pad already joined to a via of its net by board copper?"""
        try:
            return any(item.GetClass() == "PCB_VIA" for item in conn.GetConnectedItems(pad))
        except Exception:
            return False

    scope_refs_set: Set[str] = set(options.scope_refs)
    scope_net_set: Set[str] = set(options.scope_nets)
    if options.scope == SCOPE_SHEETS:
        wanted_sheets = set(options.scope_sheets)
        scope_refs_set = {fp.GetReference() for fp in board.GetFootprints() if sheet_of(fp) in wanted_sheets}
        scope_net_set = set()

    plane_zones: Dict[int, list] = {}
    for zone in board.Zones():
        if not zone.GetIsRuleArea() and zone.GetNetCode() in nets:
            plane_zones.setdefault(zone.GetNetCode(), []).append(zone)

    def _pad_exits(code: int, pos) -> List[int]:
        """The pad's exit cells (its stub ends), the pad centre cell when it has no stub."""
        net = nets[code]
        mine = [i for i, e in net.endpoints.items() if e[2] == pos.x and e[3] == pos.y]
        return [i for i in mine if (net.endpoints[i][0], net.endpoints[i][1]) != (pos.x, pos.y)] or mine

    def _plane_goals(code: int, pos, pl) -> Set[int]:
        """Cells on the net's pour (another layer) within 3 mm of the pad:
        where a search may end with a via when no spot next to the pad fits one."""
        goals: Set[int] = set()
        r = int(3_000_000 / pitch)
        cx0, cy0 = grid.cell_x(pos.x), grid.cell_y(pos.y)
        for z in plane_zones.get(code, ()):
            zl = {layer_of[l] for l in z.GetLayerSet().CuStack() if l in layer_of} - set(pl)
            if not zl:
                continue
            for cy in range(max(0, cy0 - r), min(grid.h - 1, cy0 + r) + 1):
                for cx in range(max(0, cx0 - r), min(grid.w - 1, cx0 + r) + 1):
                    if (cx - cx0) ** 2 + (cy - cy0) ** 2 > r * r:
                        continue
                    if _zone_covers(z, grid.nm_x(cx), grid.nm_y(cy), -600_000):
                        for li in zl:
                            goals.add(grid.index(li, cx, cy))
        return goals

    if job_items and not options.fanout_vias and zone_layers:
        # pads whose via from an earlier pass may be ripped up in this one:
        # the router drops the via again right after, from these exits
        for pad, code, pl, pos, own_cells, centre in pending:
            if code not in nets or nets[code].route or code not in zone_layers or not _touched_by_job(pad):
                continue
            exits = _pad_exits(code, pos)
            if not exits:
                continue
            if len(exits) > 1:
                fanout_alt[exits[0]] = exits[1:]
            goals = _plane_goals(code, pos, pl)
            if goals:
                for e in exits:
                    fanout_goals[e] = goals
    if options.fanout_vias and zone_layers:
        for pad, code, pl, pos, own_cells, centre in pending:
            if code not in zone_layers or code not in nets:
                continue
            if options.scope != SCOPE_BOARD:
                fp = pad.GetParentFootprint()
                ref = pcbnew.Cast_to_FOOTPRINT(fp).GetReference() if fp else ""
                if ref not in scope_refs_set and nets[code].name not in scope_net_set:
                    continue                      # pours follow the routing scope
            if _has_via(pad) if not options.shove else _via_near(code, pos.x, pos.y, 1_500_000 + nets[code].rules.via_diameter):
                continue                          # a via already fans this pad out
            if nets[code].route and not (options.power_pours and len(zone_layers[code]) >= 1):
                continue                          # a routed net without a usable pour
            if pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                continue                          # through every layer already
            if not (zone_layers[code] - set(pl)):
                continue                          # no plane on another layer to tie to
            if not any(_zone_covers(z, pos.x, pos.y, 1_500_000) for z in plane_zones.get(code, ())
                       if {layer_of[l] for l in z.GetLayerSet().CuStack() if l in layer_of} - set(pl)):
                continue                          # the pour does not come under this pad
            if options.fanout_only_unconnected and _pour_reaches(pad):
                continue                          # the pour on its own layer already connects it
            exits = _pad_exits(code, pos)
            if exits:                             # no stub: the via is sought around the pad itself
                fanout.append((code, exits[0]))
                if len(exits) > 1:
                    fanout_alt[exits[0]] = exits[1:]
                goals = _plane_goals(code, pos, pl)
                if goals:
                    fanout_goals[exits[0]] = goals

    # Via stitching candidates: for unrouted pour nets whose zones span two or
    # more layers (ground), a grid of points inside the zone outlines on at
    # least two of those layers. The router drops vias there after everything
    # else, where they still fit, to shorten return paths.
    stitch: Dict[int, List[Tuple[int, int]]] = {}
    if options.stitch_vias and options.scope == SCOPE_BOARD:
        step = max(grid.pitch, int(options.stitch_pitch_mm * 1_000_000))
        step_cells = max(1, step // grid.pitch)
        for code, lset in zone_layers.items():
            if code not in nets or len(lset) < 2:
                continue
            if nets[code].route and not options.power_pours:
                continue
            # power pours get a coarser grid than ground
            cells_here = step_cells if not nets[code].route else step_cells * 2
            cover: Dict[int, int] = {}                 # 2-D cell -> number of layers with this net's zone
            for zone in board.Zones():
                if zone.GetIsRuleArea() or zone.GetNetCode() != code:
                    continue
                zl = {layer_of[l] for l in zone.GetLayerSet().CuStack() if l in layer_of}
                for pts, zholes in _poly_rings(zone.Outline()):
                    for idx in grid.polygon_cells(0, pts, zholes, 0):
                        cover[idx] = cover.get(idx, 0) + len(zl)
            spots = []
            half = cells_here * grid.pitch // 2
            for cy in range(cells_here // 2, grid.h, cells_here):
                for cx in range(cells_here // 2, grid.w, cells_here):
                    if cover.get(cy * grid.w + cx, 0) >= 2 and not _via_near(code, grid.nm_x(cx), grid.nm_y(cy), half):
                        spots.append((cx, cy))
            if spots:
                stitch[code] = spots

    # Clusters (pads already joined by copper) and the connections between them.
    say("Building the connection list...")
    connections: List[Connection] = []
    existing: List[ExistingPath] = []
    zones_by_net: Dict[int, List] = {}
    if options.shove:
        say("Reading the existing tracks as movable paths...")
        for zone in board.Zones():
            if not zone.GetIsRuleArea() and zone.GetNetCode() in nets:
                zones_by_net.setdefault(zone.GetNetCode(), []).append(zone)
    for code, entries in pad_cells.items():
        net = nets[code]
        pads = [pad for pad, _, _, _, _ in entries]
        parent = list(range(len(entries)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        pieces = []
        if code in movable and not net.route:
            # this job's own pad vias on a plane net: pieces to rip up, each
            # knowing only the pads it touches (the plane joins the pads: no
            # connections to reroute, and no zone to pull every pad in)
            pieces = _movable_pieces(pads, movable[code], [], layer_of)
        elif options.shove:
            # pads joined by copper the router may not move: zones, locked tracks
            for i, j in _fixed_links(board, pads, fixed_tracks.get(code, []), zones_by_net.get(code, [])):
                parent[find(i)] = find(j)
            pieces = _movable_pieces(pads, movable.get(code, []), zones_by_net.get(code, []), layer_of)
        if not options.shove:
            index_of = {id(pad): i for i, (pad, _, _, _, _) in enumerate(entries)}
            for i, (pad, _, _, _, _) in enumerate(entries):
                try:
                    linked = conn.GetConnectedItems(pad)
                except Exception:
                    linked = []
                for other in linked:
                    j = index_of.get(id(other))
                    if j is None and other.GetClass() == "PAD":
                        # a different wrapper object for the same pad: match by position/footprint
                        for k, (cand, _, _, _, _) in enumerate(entries):
                            if cand.GetPosition() == other.GetPosition() and cand.GetNumber() == other.GetNumber():
                                j = k
                                break
                    if j is not None:
                        parent[find(i)] = find(j)
        groups: Dict[int, Cluster] = {}
        cluster_index: Dict[int, int] = {}
        for i, (pad, cells, centre, ref, tight) in enumerate(entries):
            root = find(i)
            if root not in groups:
                cluster_index[root] = len(groups)
                groups[root] = Cluster()
            cl = groups[root]
            cl.cells.update(cells)
            pos = pad.GetPosition()
            cl.copper.update(pad_copper.get((code, pos.x, pos.y), ()))
            cl.anchors.append(centre)
            if ref:
                cl.refs.add(ref)
            if tight:
                cl.tight = True
        net.clusters = list(groups.values())
        for piece in pieces:
            touched = sorted({cluster_index[find(i)] for i in piece["pads"]})
            rp = RoutedPath(code, piece["width"], piece["segments"], piece["vias"],
                            piece["via_diameter"] or net.rules.via_diameter, piece["via_drill"] or net.rules.via_drill,
                            pid=len(existing) + 1)
            cells: List[int] = []
            for li, x1, y1, x2, y2, w in piece["segments"]:
                cells += grid.segment_cells(li, x1, y1, x2, y2, w // 2)
            for x, y in piece["vias"]:
                for li in range(grid.layers):
                    cells += grid.disc_cells(li, x, y, rp.via_diameter // 2)
            pad_exits = [_pad_exits(code, pads[i].GetPosition()) for i in sorted(piece["pads"])]
            existing.append(ExistingPath(rp, list(zip(touched, touched[1:])), touched[0] if touched else -1,
                                         cells, piece["items"], [e for e in pad_exits if e]))
        if len(net.clusters) < 2 or not net.route:
            continue
        # Minimum spanning tree over clusters by nearest anchor distance (Prim).
        k = len(net.clusters)

        def dist(ca: Cluster, cb: Cluster) -> int:
            best = None
            for ax, ay in ca.anchors:
                for bx, by in cb.anchors:
                    d = max(abs(ax - bx), abs(ay - by)) + min(abs(ax - bx), abs(ay - by)) // 2
                    if best is None or d < best:
                        best = d
            return best

        in_tree = {0}
        best_edge = {j: (dist(net.clusters[0], net.clusters[j]), 0) for j in range(1, k)}
        while len(in_tree) < k:
            j = min(best_edge, key=lambda x: best_edge[x][0])
            d, i = best_edge.pop(j)
            tight = net.clusters[i].tight or net.clusters[j].tight
            neck = neck_width(board, net.rules) if (tight and options.neck_down and not net.partner) else 0
            connections.append(Connection(code, i, j, d, 0 if tight else 1, net.rules.width,
                                          neck=neck if neck < net.rules.width else 0, rank=net.rank))
            in_tree.add(j)
            for m in list(best_edge):
                dm = dist(net.clusters[j], net.clusters[m])
                if dm < best_edge[m][0]:
                    best_edge[m] = (dm, j)

    _pick_pair_ends(nets, connections)

    # Pair up the two connections of a differential pair that join the same
    # two footprints; the router treats such a pair as one coupled route.
    pair_id = 0
    for conn_a in connections:
        net_a = nets[conn_a.net]
        if not net_a.partner or conn_a.pair:
            continue
        refs_a1 = net_a.clusters[conn_a.a].refs
        refs_a2 = net_a.clusters[conn_a.b].refs
        for conn_b in connections:
            if conn_b.net != net_a.partner or conn_b.pair:
                continue
            net_b = nets[conn_b.net]
            refs_b1 = net_b.clusters[conn_b.a].refs
            refs_b2 = net_b.clusters[conn_b.b].refs
            if (refs_a1 & refs_b1 and refs_a2 & refs_b2) or (refs_a1 & refs_b2 and refs_a2 & refs_b1):
                pair_id += 1
                conn_a.pair = conn_b.pair = pair_id
                conn_a.width = conn_b.width = net_a.rules.width
                conn_a.priority = conn_b.priority = 0          # pairs first, they need the room
                break

    all_connections = len(connections)
    connections = in_scope(board, nets, connections, options)

    problem = RoutingProblem(grid, layers, owners, via_block, nets, connections, options,
                             hole_to_hole=hole_to_hole, max_via_drill=max_via_drill, safety=safety,
                             fanout=fanout, fanout_alt=fanout_alt, fanout_goals=fanout_goals, stitch=stitch, existing=existing, via_guard=via_guard,
                             lane_guard=lane_guard)
    problem.escape_sides = problem_escape_sides
    problem.stats = {"groups": dict(Counter(n.group for n in nets.values() if n.route)),
                     "net_groups": {n.name: n.group for n in nets.values() if n.route},
                     "extract_seconds": time.time() - t0, "cells": grid.size,
                     "nets": len(nets), "connections": len(connections), "movable_paths": len(existing),
                     "all_connections": all_connections, "owner_maps": len(owners),
                     "fanout_vias": len(fanout), "plane_nets": len(plane_codes), "stitch_spots": sum(len(v) for v in stitch.values()),
                     "escapes": dict(escape_stats)}
    return problem


# ---- shove mode: the board's tracks as movable pieces -------------------------

def _overlap(a, b, layer) -> bool:
    """Do two items' copper shapes overlap on this layer?"""
    try:
        return bool(a.GetEffectiveShape(layer).Collide(b.GetEffectiveShape(layer), 0))
    except Exception:
        return False


def _touches_pad(pad, item) -> bool:
    """Does the track or via's copper overlap this pad (a hand-drawn track
    often ends exactly on the pad's edge, so a point test is not enough)?"""
    if item.GetClass() == "PCB_VIA":
        return any(_overlap(pad, item, l) for l in pad.GetLayerSet().CuStack() if item.IsOnLayer(l))
    return pad.IsOnLayer(item.GetLayer()) and _overlap(pad, item, item.GetLayer())


def _touches_zone(zone, item, kicad_layers) -> bool:
    """Does the item sit on the zone's filled copper on any of its layers?"""
    for layer in kicad_layers:
        if item.GetClass() != "PCB_VIA" and item.GetLayer() != layer:
            continue
        try:
            filled = zone.GetFilledPolysList(layer)
            if filled.OutlineCount() and filled.Collide(item.GetEffectiveShape(layer), 0):
                return True
        except Exception:
            continue
    return False


def _link_items(items) -> List[int]:
    """Union-find over tracks and vias of one net by touching geometry; returns
    the root per item."""
    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    ends: Dict[Tuple[int, int], List[int]] = {}
    for i, item in enumerate(items):
        pts = [item.GetPosition()] if item.GetClass() == "PCB_VIA" else [item.GetStart(), item.GetEnd()]
        for pt in pts:
            ends.setdefault((pt.x, pt.y), []).append(i)
    for same in ends.values():                      # coincident ends (the common case)
        for i in same[1:]:
            parent[find(i)] = find(same[0])
    boxes = [item.GetBoundingBox() for item in items]
    for i, item in enumerate(items):                # T joints, ends inside a via, overlapping copper
        for j in range(i + 1, n):
            other = items[j]
            if find(i) == find(j) or not boxes[i].Intersects(boxes[j]):
                continue
            if item.GetClass() == "PCB_VIA" or other.GetClass() == "PCB_VIA":
                track = other if item.GetClass() == "PCB_VIA" else item
                via = item if item.GetClass() == "PCB_VIA" else other
                if track.GetClass() == "PCB_VIA":
                    layer = pcbnew.F_Cu          # two vias: through, same stack
                else:
                    layer = track.GetLayer()
                if via.IsOnLayer(layer) and _overlap(track, via, layer):
                    parent[find(i)] = find(j)
            elif other.GetLayer() == item.GetLayer() and _overlap(item, other, item.GetLayer()):
                parent[find(i)] = find(j)
    return [find(i) for i in range(n)]


def _fixed_links(board, pads, locked_items, zones) -> List[Tuple[int, int]]:
    """Pad index pairs joined by copper the router may not move: a zone fill
    both touch, or a chain of locked tracks."""
    links: List[Tuple[int, int]] = []
    for i in range(len(pads)):                  # overlapping pads (stacked USB-C pins, ...)
        for j in range(i + 1, len(pads)):
            shared = [l for l in pads[i].GetLayerSet().CuStack() if pads[j].IsOnLayer(l)]
            for layer in shared:
                try:
                    if pads[i].GetEffectiveShape(layer).Collide(pads[j].GetEffectiveShape(layer), 0):
                        links.append((i, j))
                        break
                except Exception:
                    continue
    for zone in zones:
        kl = list(zone.GetLayerSet().CuStack())
        on = []
        for i, pad in enumerate(pads):
            for layer in kl:
                if not pad.IsOnLayer(layer):
                    continue
                try:
                    filled = zone.GetFilledPolysList(layer)
                    if filled.OutlineCount() and filled.Collide(pad.GetEffectiveShape(layer), 0):
                        on.append(i)
                        break
                except Exception:
                    continue
        links += [(on[0], k) for k in on[1:]]
    if locked_items:
        roots = _link_items(locked_items)
        by_root: Dict[int, List[int]] = {}
        for item, root in zip(locked_items, roots):
            for i, pad in enumerate(pads):
                if _touches_pad(pad, item):
                    by_root.setdefault(root, []).append(i)
        for on in by_root.values():
            links += [(on[0], k) for k in on[1:]]
    return links


def _movable_pieces(pads, items, zones, layer_of: Dict[int, int]) -> List[dict]:
    """The unlocked tracks and vias of one net as connected pieces, each with
    the pads it touches (a piece on a zone fill counts the zone's pads too,
    since the fill joins them)."""
    if not items:
        return []
    roots = _link_items(items)
    pieces: Dict[int, dict] = {}
    zone_pads: Dict[int, List[int]] = {}
    for zi, zone in enumerate(zones):
        kl = list(zone.GetLayerSet().CuStack())
        for i, pad in enumerate(pads):
            for layer in kl:
                if pad.IsOnLayer(layer):
                    try:
                        filled = zone.GetFilledPolysList(layer)
                        if filled.OutlineCount() and filled.Collide(pad.GetEffectiveShape(layer), 0):
                            zone_pads.setdefault(zi, []).append(i)
                            break
                    except Exception:
                        continue
    for item, root in zip(items, roots):
        piece = pieces.setdefault(root, {"segments": [], "vias": [], "pads": set(), "items": [],
                                         "width": 0, "via_diameter": 0, "via_drill": 0})
        if item.GetClass() == "PCB_VIA":
            pos = item.GetPosition()
            piece["vias"].append((pos.x, pos.y))
            try:
                d = int(item.GetWidth(pcbnew.F_Cu))
            except TypeError:
                d = int(item.GetWidth())
            piece["via_diameter"] = max(piece["via_diameter"], d)
            piece["via_drill"] = max(piece["via_drill"], int(item.GetDrillValue()))
        else:
            li = layer_of.get(item.GetLayer())
            if li is None:
                continue
            a, b = item.GetStart(), item.GetEnd()
            w = int(item.GetWidth())
            piece["segments"].append((li, a.x, a.y, b.x, b.y, w))
            piece["width"] = max(piece["width"], w)
        piece["items"].append(item)
        for i, pad in enumerate(pads):
            if _touches_pad(pad, item):
                piece["pads"].add(i)
        for zi, zone in enumerate(zones):
            if zi in zone_pads and _touches_zone(zone, item, list(zone.GetLayerSet().CuStack())):
                piece["pads"].update(zone_pads[zi])
    out = [piece for piece in pieces.values() if piece["segments"] or piece["vias"]]
    for piece in out:
        piece["width"] = piece["width"] or (piece["via_diameter"] // 2)
    return out


def in_scope(board, nets: Dict[int, NetInfo], connections: List[Connection],
             options: NativeOptions) -> List[Connection]:
    """Keep the connections the scope asks for. A connection is in scope when
    either end touches a footprint in scope (selected, or on a chosen sheet),
    or its net is in scope (a selected track, via or zone)."""
    if options.pours_only:
        return []
    if options.scope == SCOPE_BOARD:
        return connections
    refs: Set[str] = set(options.scope_refs)
    net_names: Set[str] = set(options.scope_nets)
    if options.scope == SCOPE_SHEETS:
        wanted = set(options.scope_sheets)
        refs = {fp.GetReference() for fp in board.GetFootprints() if sheet_of(fp) in wanted}
        net_names = set()
    kept = []
    for c in connections:
        net = nets[c.net]
        if net.name in net_names:
            kept.append(c)
            continue
        a, b = net.clusters[c.a], net.clusters[c.b]
        if (a.refs | b.refs) & refs:
            kept.append(c)
    return kept
