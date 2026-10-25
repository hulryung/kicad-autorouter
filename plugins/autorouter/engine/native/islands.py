"""Find vias that join isolated filled copper to a connected layer of its net."""
import pcbnew


def _inside(poly, index, x, y, inset):
    return all(poly.Contains(pcbnew.VECTOR2I(x + dx, y + dy), index)
               for dx, dy in ((0, 0), (inset, 0), (-inset, 0), (0, inset), (0, -inset)))


def stitch_candidates(board, grid, nets, main):
    """One ordered set of landing positions per unanchored filled island.

    The router checks every layer's copper and drill clearance. This only
    establishes that both ends of the via touch filled copper of its net.
    """
    conn = board.GetConnectivity()
    candidates = []
    for code, positions in sorted(main.items()):
        if code not in nets:
            continue
        pads = [pad for pad in board.GetPads() if pad.GetNetCode() == code
                and (pad.GetPosition().x, pad.GetPosition().y) in positions]
        if not pads:
            continue
        anchors = {}
        for item in [pads[0]] + list(conn.GetConnectedItems(pads[0])):
            kind = item.GetClass()
            if kind not in ("PAD", "PCB_VIA", "PCB_TRACK", "PCB_ARC"):
                continue
            point_item = kind in ("PAD", "PCB_VIA")
            lids = list(item.GetLayerSet().CuStack()) if point_item else [item.GetLayer()]
            points = [item.GetPosition()] if point_item else [item.GetStart(), item.GetEnd()]
            for lid in lids:
                anchors.setdefault(lid, []).extend(points)
        fills = []
        for zone in board.Zones():
            if zone.GetNetCode() != code or zone.GetIsRuleArea():
                continue
            for lid in zone.GetLayerSet().CuStack():
                poly = zone.GetFilledPolysList(lid)
                for index in range(poly.OutlineCount()):
                    attached = any(poly.Contains(pt, index) for pt in anchors.get(lid, []))
                    fills.append((lid, poly, index, attached))
        # Keep the entire via annulus inside both pours, with clearance to
        # their edges. Physical copper and hole checks still happen later.
        rules = nets[code].rules
        inset = rules.via_diameter // 2 + rules.clearance
        for lid, poly, index, attached in fills:
            targets = [(pp, ii) for ll, pp, ii, at in fills if ll != lid and at]
            if attached or not targets:
                continue
            box = poly.Outline(index).BBox()
            spots = []
            for cy in range(max(0, grid.cell_y(box.GetY())),
                            min(grid.h - 1, grid.cell_y(box.GetBottom())) + 1):
                for cx in range(max(0, grid.cell_x(box.GetX())),
                                min(grid.w - 1, grid.cell_x(box.GetRight())) + 1):
                    x, y = grid.nm_x(cx), grid.nm_y(cy)
                    if (_inside(poly, index, x, y, inset)
                            and any(_inside(pp, ii, x, y, inset) for pp, ii in targets)):
                        spots.append((cx, cy))
            if spots:
                mx = sum(x for x, y in spots) / len(spots)
                my = sum(y for x, y in spots) / len(spots)
                spots.sort(key=lambda xy: (xy[0] - mx) ** 2 + (xy[1] - my) ** 2)
                candidates.append((code, spots))
    return candidates
