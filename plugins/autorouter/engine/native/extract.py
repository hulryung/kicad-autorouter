"""Reading the board into a RoutingProblem through pcbnew (main thread only)."""
import fnmatch
import math
import time
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

import pcbnew

from .grid import BLOCKED, Grid
from .problem import (Cluster, Connection, LayerInfo, NativeOptions, NetInfo, NetRules,
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
    1. a net class other than Default is an explicit choice: its width;
    2. tracks already on the board for this net: their usual width;
    3. a power net (by name): the widest pre-defined width that still fits the
       narrowest pad it lands on, never below the net class;
    4. otherwise the net class width."""
    code = net.GetNetCode()
    if net.GetNetClassName() != "Default":
        return nc.GetTrackWidth()
    existing = hints.existing_width(code)
    if existing:
        return existing
    name = net.GetNetname()
    if any(fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(name.upper(), pat.upper())
           for pat in options.power_patterns):
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
               towards: Optional[Tuple[int, int]] = None) -> Optional[List[Tuple[int, int]]]:
    """Where a track may leave an SMD pad: points on the pad's long axis, just
    far enough past either pad end that the track is clear of the neighbouring
    pads (clearance + half width + one cell). The side facing `towards` (the
    nearest other pad of the net) comes first, else the side pointing away
    from the footprint centre. None for through-hole pads, which can be
    entered from any side."""
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
    options = []
    for sign in (1, -1):
        ex = pos.x + sign * axis[0] * reach
        ey = pos.y + sign * axis[1] * reach
        if towards is not None:
            score = -((ex - towards[0]) ** 2 + (ey - towards[1]) ** 2)   # nearer the target first
        else:
            score = (ex - centre.x) ** 2 + (ey - centre.y) ** 2          # away from the centre first
        options.append((score, int(round(ex)), int(round(ey))))
    options.sort(reverse=True)
    return [(ex, ey) for _, ex, ey in options]


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


def _skip_net(net, options: NativeOptions) -> bool:
    name = net.GetNetname()
    if net.GetNetClassName() in options.ignore_net_classes:
        return True
    return any(fnmatch.fnmatchcase(name, pat) or name == pat for pat in options.skip_nets)


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
    for code, net in board.GetNetsByNetcode().items():
        if code <= 0:
            continue
        skipped = _skip_net(net, options)
        if skipped and not (options.fanout_vias and code in zone_layers):
            continue
        nets[code] = NetInfo(code, net.GetNetname(), net_rules(board, net, options, hints), route=not skipped)
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
            for a, b in ((n, m), (m, n)):
                a.partner = b.code
                a.pair_gap = gap
                a.rules = NetRules(max(width, ds.m_TrackMinWidth), a.rules.clearance,
                                   a.rules.via_diameter, a.rules.via_drill)
    # Connections that end on fine-pitch pins are routed at the board's
    # narrowest pre-defined width (the neck width), as a designer would, so
    # the escapes and the tracks between the pins have room. Those widths
    # need owner maps of their own.
    keys = {n.rules.key for n in nets.values()}
    for n in nets.values():
        if n.partner:
            keys.add((2 * n.rules.width + n.pair_gap, n.rules.clearance))
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
    safety = max(pitch // 10, 5_000)

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
                      max(clr, hole_clr))
            if code in nets:
                inside = grid.cells_where(li, box, lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), 0))
                own_cells.update(inside)
                if grid.inside(*centre):
                    own_cells.add(grid.index(li, *centre))
        if hole_radius:
            block_vias_near_hole(pos.x, pos.y, hole_radius)
        if code in nets:
            pending.append((pad, code, pl, pos, own_cells, centre))

    # Existing tracks and vias.
    say("Rasterizing existing tracks...")
    for item in board.GetTracks():
        code = item.GetNetCode()
        cls = item.GetClass()
        if cls == "PCB_VIA":
            vl = [layer_of[l] for l in item.GetLayerSet().CuStack() if l in layer_of]
            pos = item.GetPosition()
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
                claim(li, code, lambda r, li=li, radius=radius: grid.disc_cells(li, pos.x, pos.y, radius + r), clr)
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
    exit_owner: Dict[int, int] = {}            # exit cell -> net, to keep lanes apart
    positions_by_net: Dict[int, List[Tuple[int, int]]] = {}
    for pad, code, pl, pos, own_cells, centre in pending:
        positions_by_net.setdefault(code, []).append((pos.x, pos.y))
    for pad, code, pl, pos, own_cells, centre in pending:
        fp = footprint_of(pad)
        net = nets[code]
        own = owners[net.rules.key]
        stub_w = net.rules.width
        others = [q for q in positions_by_net[code] if q != (pos.x, pos.y)]
        towards = min(others, key=lambda q: (q[0] - pos.x) ** 2 + (q[1] - pos.y) ** 2) if others else None
        escapes = pad_escape(pad, fp, net.rules, pitch, towards)
        if escapes and fp is not None and options.neck_down:
            key = fp.GetReference()
            if key not in tight_by_fp:
                # neighbours closer than width + 2 * clearance + a cell of slack
                tight_by_fp[key] = tight_pads(fp, net.rules.width + 2 * net.rules.clearance + pitch + 60_000)
            if pad.GetNumber() in tight_by_fp[key]:
                stub_w = neck_width(board, net.rules)
        exit_cells: Set[int] = set()
        chosen = None
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
                        return False
                return True

            single = [pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(pos.x, pos.y), pcbnew.VECTOR2I(gx, gy), stub_w)]
            if lane_clear(single):
                stub_shapes = single
                ex, ey = gx, gy                      # exit point is the cell itself: one piece
            else:
                stub_shapes = [pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(pos.x, pos.y), pcbnew.VECTOR2I(ex, ey), stub_w),
                               pcbnew.SHAPE_SEGMENT(pcbnew.VECTOR2I(ex, ey), pcbnew.VECTOR2I(gx, gy), stub_w)]
                if not lane_clear(stub_shapes):
                    continue
            # and this lane's clearance zone must not cover another net's exit
            width, clearance = net.rules.key
            zone_r = max(clearance, ds.m_MinClearance) + width // 2 + safety + stub_w // 2
            zone = []
            for li in pl:
                zone += grid.segment_cells(li, pos.x, pos.y, ex, ey, zone_r)
                zone += grid.segment_cells(li, ex, ey, gx, gy, zone_r)
            if any(exit_owner.get(idx, code) != code for idx in zone):
                continue
            chosen = (ex, ey, ecx, ecy, gx, gy)
            break
        if chosen is not None:
            ex, ey, ecx, ecy, gx, gy = chosen
            half = stub_w // 2
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
            target_cells = exit_cells
            centre = (ecx, ecy)
        else:
            target_cells = own_cells
            for li in pl:
                if grid.inside(*centre):
                    net.endpoints[grid.index(li, *centre)] = (pos.x, pos.y, pos.x, pos.y, net.rules.width)
        pad_cells.setdefault(code, []).append((pad, target_cells, centre, fp.GetReference() if fp else "",
                                               chosen is not None and stub_w < net.rules.width))

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
    if options.fanout_vias and zone_layers:
        for pad, code, pl, pos, own_cells, centre in pending:
            if code not in zone_layers or code not in nets:
                continue
            if nets[code].route and not (options.power_pours and len(zone_layers[code]) >= 1):
                continue                          # a routed net without a usable pour
            if pad.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH):
                continue                          # through every layer already
            if not (zone_layers[code] - set(pl)):
                continue                          # no plane on another layer to tie to
            if options.fanout_only_unconnected and _pour_reaches(pad):
                continue                          # the pour on its own layer already connects it
            net = nets[code]
            exits = [i for i, e in net.endpoints.items() if e[2] == pos.x and e[3] == pos.y and (e[0], e[1]) != (e[2], e[3])]
            if exits:
                fanout.append((code, exits[0]))

    # Via stitching candidates: for unrouted pour nets whose zones span two or
    # more layers (ground), a grid of points inside the zone outlines on at
    # least two of those layers. The router drops vias there after everything
    # else, where they still fit, to shorten return paths.
    stitch: Dict[int, List[Tuple[int, int]]] = {}
    if options.stitch_vias:
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
            for cy in range(cells_here // 2, grid.h, cells_here):
                for cx in range(cells_here // 2, grid.w, cells_here):
                    if cover.get(cy * grid.w + cx, 0) >= 2:
                        spots.append((cx, cy))
            if spots:
                stitch[code] = spots

    # Clusters (pads already joined by copper) and the connections between them.
    say("Building the connection list...")
    connections: List[Connection] = []
    for code, entries in pad_cells.items():
        net = nets[code]
        index_of = {id(pad): i for i, (pad, _, _, _, _) in enumerate(entries)}
        parent = list(range(len(entries)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

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
        for i, (pad, cells, centre, ref, tight) in enumerate(entries):
            cl = groups.setdefault(find(i), Cluster())
            cl.cells.update(cells)
            cl.anchors.append(centre)
            if ref:
                cl.refs.add(ref)
            if tight:
                cl.tight = True
        net.clusters = list(groups.values())
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
            width = neck_width(board, net.rules) if (tight and options.neck_down and not net.partner) else net.rules.width
            connections.append(Connection(code, i, j, d, 0 if tight else 1, width))
            in_tree.add(j)
            for m in list(best_edge):
                dm = dist(net.clusters[j], net.clusters[m])
                if dm < best_edge[m][0]:
                    best_edge[m] = (dm, j)

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
                             fanout=fanout, stitch=stitch)
    problem.stats = {"extract_seconds": time.time() - t0, "cells": grid.size,
                     "nets": len(nets), "connections": len(connections),
                     "all_connections": all_connections, "owner_maps": len(owners),
                     "fanout_vias": len(fanout), "stitch_spots": sum(len(v) for v in stitch.values())}
    return problem


def in_scope(board, nets: Dict[int, NetInfo], connections: List[Connection],
             options: NativeOptions) -> List[Connection]:
    """Keep the connections the scope asks for. A connection is in scope when
    either end touches a footprint in scope (selected, or on a chosen sheet),
    or its net is in scope (a selected track, via or zone)."""
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
