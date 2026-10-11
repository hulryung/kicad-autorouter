"""Reading the board into a RoutingProblem through pcbnew (main thread only)."""
import fnmatch
import math
import time
from typing import Dict, List, Optional, Set, Tuple

import pcbnew

from .grid import BLOCKED, Grid
from .problem import (Cluster, Connection, LayerInfo, NativeOptions, NetInfo, NetRules,
                      RoutingProblem, SCOPE_BOARD, SCOPE_SELECTION, SCOPE_SHEETS, SIZE_CURRENT,
                      SIZE_FIXED)

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


def net_rules(board, net, options: Optional[NativeOptions] = None) -> NetRules:
    """Width, clearance and via of a net: the effective net class (which is
    how KiCad resolves netclass assignments and patterns), overridden by the
    editor's current sizes or a fixed size when the options say so, and never
    below the board's minimums."""
    ds = board.GetDesignSettings()
    nc = ds.m_NetSettings.GetEffectiveNetClass(net.GetNetname())
    width, via_d, via_drill = nc.GetTrackWidth(), nc.GetViaDiameter(), nc.GetViaDrill()
    if options is not None:
        if options.track_mode == SIZE_CURRENT:
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
    stack = list(board.GetEnabledLayers().CuStack())
    layers = []
    for i, lid in enumerate(stack):
        if options.routable_layers and i < len(options.routable_layers) and not options.routable_layers[i]:
            continue
        if options.horizontal_layers and i < len(options.horizontal_layers):
            horizontal = bool(options.horizontal_layers[i])
        else:
            horizontal = (i % 2 == 0)
        layers.append(LayerInfo(len(layers), lid, board.GetLayerName(lid), horizontal))
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

    # Nets, their rules and the owner maps those rules need.
    say("Reading nets and design rules...")
    nets: Dict[int, NetInfo] = {}
    for code, net in board.GetNetsByNetcode().items():
        if code <= 0 or _skip_net(net, options):
            continue
        nets[code] = NetInfo(code, net.GetNetname(), net_rules(board, net, options))
    keys = sorted({n.rules.key for n in nets.values()}) or [(board.GetDesignSettings().m_TrackMinWidth,
                                                              board.GetDesignSettings().m_MinClearance)]
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

    def claim(layer: int, code: int, cells_for, own_clr: Optional[int] = None) -> None:
        """cells_for(r) -> cells; one radius per owner map."""
        if own_clr is None:
            own_clr = clearance_of(code)
        for (width, clearance), own in owners.items():
            r = max(clearance, own_clr) + width // 2
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
            fp = footprint_of(pad)
            pad_cells.setdefault(code, []).append((pad, own_cells, centre, fp.GetReference() if fp else ""))

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
            claim(layer.index, 0, lambda r, shape=shape, box=box, li=layer.index:
                  grid.cells_where(li, (box[0] - r, box[1] - r, box[2] + r, box[3] + r),
                                   lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), r)))

    # Clusters (pads already joined by copper) and the connections between them.
    say("Building the connection list...")
    conn = board.GetConnectivity()
    connections: List[Connection] = []
    for code, entries in pad_cells.items():
        net = nets[code]
        index_of = {id(pad): i for i, (pad, _, _, _) in enumerate(entries)}
        parent = list(range(len(entries)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, (pad, _, _, _) in enumerate(entries):
            try:
                linked = conn.GetConnectedItems(pad)
            except Exception:
                linked = []
            for other in linked:
                j = index_of.get(id(other))
                if j is None and other.GetClass() == "PAD":
                    # a different wrapper object for the same pad: match by position/footprint
                    for k, (cand, _, _, _) in enumerate(entries):
                        if cand.GetPosition() == other.GetPosition() and cand.GetNumber() == other.GetNumber():
                            j = k
                            break
                if j is not None:
                    parent[find(i)] = find(j)
        groups: Dict[int, Cluster] = {}
        for i, (pad, cells, centre, ref) in enumerate(entries):
            cl = groups.setdefault(find(i), Cluster())
            cl.cells.update(cells)
            cl.anchors.append(centre)
            if ref:
                cl.refs.add(ref)
        net.clusters = list(groups.values())
        if len(net.clusters) < 2:
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
            connections.append(Connection(code, i, j, d))
            in_tree.add(j)
            for m in list(best_edge):
                dm = dist(net.clusters[j], net.clusters[m])
                if dm < best_edge[m][0]:
                    best_edge[m] = (dm, j)

    all_connections = len(connections)
    connections = in_scope(board, nets, connections, options)

    problem = RoutingProblem(grid, layers, owners, via_block, nets, connections, options,
                             hole_to_hole=hole_to_hole, max_via_drill=max_via_drill)
    problem.stats = {"extract_seconds": time.time() - t0, "cells": grid.size,
                     "nets": len(nets), "connections": len(connections),
                     "all_connections": all_connections, "owner_maps": len(owners)}
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
