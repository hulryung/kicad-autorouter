"""Transactional local escape replanning for perimeter pad rows.

Headless only: operates on a saved copy and publishes a candidate only after
full DRC and per-net connectivity validation. Caller chooses the local region
and affected nets, including crossing blockers that may be rerouted.
"""

import copy
import json
import math
import os
import tempfile
import time
from collections import Counter
from pathlib import Path


def _local(
    b, rect, names, footprint, opts, escape_depth, stagger, escape_layers, status
):
    import pcbnew
    from .native import extract, router, apply
    from .native.problem import Cluster, Connection
    from .native.grid import BLOCKED
    from .kelvin import _polygon, _subtract

    pads = list(footprint.Pads())
    positions = {(p.GetPosition().x, p.GetPosition().y) for p in pads}
    xs = sorted({x for x, y in positions})
    ys = sorted({y for x, y in positions})
    if len(xs) < 2 or len(ys) < 2:
        raise ValueError("inward escape requires opposing footprint pad rows")

    def inward_goal(x, y):
        row = sorted(px for px, py in positions if py == y)
        column = sorted(py for px, py in positions if px == x)
        if (
            y in (ys[0], ys[-1])
            and len(row) > 1
            and (x not in (xs[0], xs[-1]) or len(row) >= len(column))
        ):
            along, across = x, y
            along_positions = row
            center = (ys[0] + ys[-1]) / 2
            horizontal = True
        elif x in (xs[0], xs[-1]) and len(column) > 1:
            along, across = y, x
            along_positions = column
            center = (xs[0] + xs[-1]) / 2
            horizontal = False
        else:
            raise ValueError("isolated pad is not on an axis-aligned perimeter row")
        depth = escape_depth + stagger * (along_positions.index(along) % 2)
        if depth >= abs(center - across):
            raise ValueError("inward escape reaches beyond the footprint centre")
        direction = 1 if across < center else -1
        return (x, y + direction * depth) if horizontal else (x + direction * depth, y)

    pad_positions = {
        (p.GetPosition().x, p.GetPosition().y) for p in pads if p.GetNetname() in names
    }
    held = []
    removed = 0
    split = 0

    def inside(x, y):
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def clip(a, z):
        dx, dy = z.x - a.x, z.y - a.y
        lo, hi = 0.0, 1.0
        for p, q in [
            (-dx, a.x - rect[0]),
            (dx, rect[2] - a.x),
            (-dy, a.y - rect[1]),
            (dy, rect[3] - a.y),
        ]:
            if p == 0:
                if q < 0:
                    return None
            elif p < 0:
                lo = max(lo, q / p)
            else:
                hi = min(hi, q / p)
        if lo >= hi:
            return None
        return lo, hi

    for item in list(b.GetTracks()):
        if (
            item.GetNetname() not in names
            or item.IsLocked()
            or item.GetParentGroup() is not None
        ):
            continue
        cls = item.GetClass()
        if cls == "PCB_VIA":
            pos = item.GetPosition()
            r = max(item.GetWidth(layer) for layer in item.GetLayerSet().CuStack()) // 2
            if inside(pos.x - r, pos.y - r) and inside(pos.x + r, pos.y + r):
                b.Remove(item)
                held.append(item)
                removed += 1
        elif cls == "PCB_TRACK":
            a, z = item.GetStart(), item.GetEnd()
            cut = clip(a, z)
            if cut is None:
                continue
            lo, hi = cut
            for x, y in [(0, lo), (hi, 1)]:
                if y - x < 1e-9:
                    continue
                part = item.Duplicate()
                part.SetStart(
                    pcbnew.VECTOR2I(
                        round(a.x + (z.x - a.x) * x), round(a.y + (z.y - a.y) * x)
                    )
                )
                part.SetEnd(
                    pcbnew.VECTOR2I(
                        round(a.x + (z.x - a.x) * y), round(a.y + (z.y - a.y) * y)
                    )
                )
                b.Add(part)
                split += 1
            b.Remove(item)
            held.append(item)
            removed += 1
    b.BuildConnectivity()
    ports = {name: {} for name in names}
    for pad in b.GetPads():
        pos = pad.GetPosition()
        if pad.GetNetname() in names and inside(pos.x, pos.y):
            ports[pad.GetNetname()][(pos.x, pos.y)] = set(pad.GetLayerSet().CuStack())
    for item in b.GetTracks():
        if item.GetNetname() not in names:
            continue
        points = (
            [item.GetPosition()]
            if item.GetClass() == "PCB_VIA"
            else [item.GetStart(), item.GetEnd()]
        )
        for pt in points:
            if inside(pt.x, pt.y):
                ports[item.GetNetname()].setdefault((pt.x, pt.y), set()).update(
                    item.GetLayerSet().CuStack()
                )

    def read(board, o, status):
        p = extract.extract(board, o, status=status, bounds_nm=rect)
        g = p.grid
        p.connections = []
        for code, net in p.nets.items():
            if net.name not in names:
                continue
            if net.layer_widths:
                raise ValueError(
                    "local replanning does not support per-layer impedance widths"
                )
            points = ports[net.name]
            keys = {net.rules.key, (net.rules.via_diameter, net.rules.clearance)}
            for layer in p.layers:
                portals = pcbnew.SHAPE_POLY_SET()
                for (x, y), lids in points.items():
                    if layer.kicad_id not in lids:
                        continue
                    portals.NewOutline()
                    for dx, dy in [
                        (-500000, -500000),
                        (500000, -500000),
                        (500000, 500000),
                        (-500000, 500000),
                    ]:
                        portals.Append(x + dx, y + dy)
                for item in list(board.GetTracks()):
                    if item.GetNetCode() != code or not item.IsOnLayer(layer.kicad_id):
                        continue
                    shape = _polygon(item, layer.kicad_id)
                    if portals.OutlineCount():
                        _subtract(shape, portals)
                    if not shape.OutlineCount():
                        continue
                    x0, y0, x1, y1 = extract._box(shape)
                    for key in keys:
                        if key not in p.owners:
                            continue
                        r = key[0] // 2 + net.rules.clearance + p.safety
                        cells = g.cells_where(
                            layer.index,
                            (x0 - r, y0 - r, x1 + r, y1 + r),
                            lambda x, y: shape.Collide(pcbnew.VECTOR2I(x, y), r),
                        )
                        g.mark(p.owners[key], cells, BLOCKED)
            net.clusters = []
            net.endpoints = {}
            net.partner = 0
            net.route = True
            for (x, y), lids in sorted(points.items()):
                cx, cy = g.cell_x(x), g.cell_y(y)
                if not g.inside(cx, cy):
                    continue
                cells = {
                    g.index(l.index, cx, cy)
                    for l in p.layers
                    if l.kicad_id in lids
                    and p.owners[net.rules.key][g.index(l.index, cx, cy)] in (0, code)
                }
                if not cells:
                    raise ValueError("no legal local port cells for " + net.name)
                net.clusters.append(
                    Cluster(cells=cells, copper=set(cells), anchors=[(cx, cy)])
                )
                for cell in cells:
                    net.endpoints[cell] = (x, y, x, y, net.rules.width)
            if len(net.clusters) == 1:
                x, y = next(iter(points))
                if (x, y) not in pad_positions:
                    raise ValueError(
                        "single boundary port has no selected footprint pad"
                    )
                tx, ty = inward_goal(x, y)
                gx, gy = g.cell_x(tx), g.cell_y(ty)
                cells = set()
                anchors = []
                for cx in range(gx - 1, gx + 2):
                    for cy in range(gy - 1, gy + 2):
                        if not g.inside(cx, cy):
                            continue
                        for layer in p.layers:
                            if (
                                layer.kicad_id not in escape_layers
                                or not layer.routable
                            ):
                                continue
                            idx = g.index(layer.index, cx, cy)
                            if p.owners[net.rules.key][idx] in (0, code):
                                cells.add(idx)
                                anchors.append((cx, cy))
                if not cells:
                    raise ValueError("no inward via goals for " + net.name)
                if cells:
                    net.clusters.append(
                        Cluster(cells=cells, copper=set(cells), anchors=anchors)
                    )
            todo = set(range(1, len(net.clusters)))
            tree = {0}
            while todo:
                d, a, c = min(
                    (
                        max(
                            abs(
                                net.clusters[a].anchors[0][0]
                                - net.clusters[c].anchors[0][0]
                            ),
                            abs(
                                net.clusters[a].anchors[0][1]
                                - net.clusters[c].anchors[0][1]
                            ),
                        ),
                        a,
                        c,
                    )
                    for a in tree
                    for c in todo
                )
                p.connections.append(
                    Connection(
                        code,
                        a,
                        c,
                        d,
                        priority=0 if len(points) == 1 else 1,
                        width=net.rules.width,
                    )
                )
                todo.remove(c)
                tree.add(c)
        p.stats["connections"] = len(p.connections)
        return p

    from .ai.board_copy import uid, track_signature

    keep = {uid(t): track_signature(t) for t in b.GetTracks()}
    p = read(b, opts, status)
    local = router.run(p)
    if local.failed:
        raise ValueError(
            "local replan could not restore: "
            + ", ".join(p.nets[c.net].name for c in local.failed)
        )
    for path in local.paths:
        apply.add_path(b, p.layers, path)
    b.BuildConnectivity()
    return local.routed, removed, split, held, keep


def _pad_components(board):
    """Refill and record pad groups, so equal open counts cannot hide a swap."""
    from .compat import connected_items, zone_filler
    from .ai.board_copy import uid

    zone_filler(board).Fill(board.Zones())
    board.BuildConnectivity()
    connection = board.GetConnectivity()
    left = {uid(p): p for p in board.GetPads() if p.GetNetCode() > 0}
    groups = {}
    while left:
        key, pad = next(iter(left.items()))
        found = {key} | {uid(item) for item in connected_items(connection, pad)}
        members = frozenset(found & left.keys())
        for member in members:
            left.pop(member)
            groups[member] = members
    return groups


def replan_row(
    board,
    footprint_ref,
    net_names,
    bounds_mm,
    output_directory,
    *,
    options=None,
    local_pitch_mm=0.05,
    escape_depth_mm=1.3,
    stagger_mm=0.7,
    escape_layers=None,
    status=None
):
    """Replan selected nets in a rectangle around a footprint with perimeter pad rows.

    Include crossing blocker nets explicitly. Locked/grouped copper, arcs,
    zones and pads are preserved. Existing straight tracks crossing the
    rectangle are clipped at its boundary and their outer pieces retained.
    Inward goals are staggered for isolated pad ports; all boundary ports
    must be restored before the global routing pass. No input is mutated.
    """
    import pcbnew
    from .ai import board_copy, context
    from .native import pipeline
    from .native.problem import NativeOptions, SCOPE_SELECTION

    tell = status or (lambda message: None)
    values = list(bounds_mm)
    if len(values) != 4 or not all(
        isinstance(v, (int, float)) and math.isfinite(v) for v in values
    ):
        raise ValueError("bounds_mm must contain four finite coordinates")
    rect = tuple(round(v * 1e6) for v in values)
    if rect[0] >= rect[2] or rect[1] >= rect[3]:
        raise ValueError("bounds must have positive area")
    for name, value in [
        ("local_pitch_mm", local_pitch_mm),
        ("escape_depth_mm", escape_depth_mm),
        ("stagger_mm", stagger_mm),
    ]:
        if (
            not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(name + " must be finite and positive")
    names = set(net_names)
    if not names or any(not isinstance(n, str) or not n for n in names):
        raise ValueError("select affected net names")
    if any(
        board.FindNet(n) is None or board.FindNet(n).GetNetCode() <= 0 for n in names
    ):
        raise ValueError("unknown selected net")
    if any(z.GetNetname() in names and not z.GetIsRuleArea() for z in board.Zones()):
        raise ValueError(
            "selected plane nets are not supported by local escape replanning"
        )
    matches = [f for f in board.GetFootprints() if f.GetReference() == footprint_ref]
    if len(matches) != 1:
        raise ValueError("footprint reference must be unique")
    if any(
        not (
            rect[0] < p.GetPosition().x < rect[2]
            and rect[1] < p.GetPosition().y < rect[3]
        )
        for p in matches[0].Pads()
    ):
        raise ValueError(
            "region must contain all footprint pads strictly inside its boundary"
        )
    enabled = list(board.GetEnabledLayers().CuStack())
    layers = {board.GetLayerName(l): l for l in enabled}
    requested = list(escape_layers) if escape_layers is not None else ["B.Cu"]
    if not requested or any(n not in layers for n in requested):
        raise ValueError("unknown or disabled escape layer")
    target_layers = {layers[n] for n in requested}
    output = Path(output_directory).resolve()
    if output.exists():
        raise ValueError("output_directory must not already exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".row-replan-", dir=str(output.parent)
    ) as folder:
        work = Path(folder)
        path = board_copy.snapshot(board, work / "input" / "board.kicad_pcb")
        candidate = board_copy.load_copy(path, board)
        fp = next(
            f for f in candidate.GetFootprints() if f.GetReference() == footprint_ref
        )
        protected = board_copy.protected_state(candidate)
        tell("Checking baseline full DRC and connectivity...")
        before = context.drc_summary(candidate, work / "before", all_severities=True)
        if not before.get("available") or not before.get("unconnected_by_net_complete"):
            raise RuntimeError("baseline DRC/connectivity unavailable")
        baseline_groups = _pad_components(candidate)
        opts = copy.deepcopy(options) if options is not None else NativeOptions()
        opts.scope = SCOPE_SELECTION
        opts.scope_nets = sorted(names)
        opts.scope_refs = []
        opts.shove = False
        opts.fanout_vias = False
        opts.stitch_vias = False
        opts.diff_pairs = False
        opts.tune_lengths = False
        opts.pour_goals = False
        opts.power_patterns = []
        opts.direct_links = False
        opts.neck_down = False
        opts.pours_only = False
        local = copy.deepcopy(opts)
        local.pitch_mm = local_pitch_mm
        tell("Jointly replanning local escapes and boundary connections...")
        from .native import astar, watchdog

        if local.time_limit_s and local.time_limit_s > 0:
            local.pass_deadline = astar.clock() + local.time_limit_s
        cap = pipeline.wall_cap(local)
        if watchdog.current() is not None:
            raise RuntimeError(
                "row replanning requires an idle headless routing worker"
            )
        watch = watchdog.start(tell, hard_deadline=time.time() + cap if cap else 0)
        try:
            routed, removed, split, held, keep = _local(
                candidate,
                rect,
                names,
                fp,
                local,
                round(escape_depth_mm * 1e6),
                round(stagger_mm * 1e6),
                target_layers,
                tell,
            )
        finally:
            watchdog.stop(watch)
        tell("Joining local escapes to their remote pads...")
        result = pipeline.run_job(candidate, opts, escalate=False, status=tell)

        def inspect(label):
            report = context.drc_summary(candidate, work / label, all_severities=True)
            if not report.get("available") or not report.get(
                "unconnected_by_net_complete"
            ):
                raise RuntimeError("candidate full DRC/connectivity unavailable")
            return report

        after = inspect("after")
        # Remove only new dangling copper, never retained boundary pieces.
        # Removing a tail can expose an unused via, so repeat a bounded number
        # of times. Full DRC and per-net connectivity still gate publication.
        trimmed = 0
        label = "after"
        for iteration in range(4):
            data = json.loads((work / label / "drc.json").read_text())
            dangling = {
                item["uuid"]
                for v in data.get("violations", [])
                if v.get("type") in ("track_dangling", "via_dangling")
                for item in v.get("items", [])
                if item.get("uuid") not in keep
            }
            removed_now = 0
            for item in list(candidate.GetTracks()):
                if (
                    board_copy.uid(item) in dangling
                    and item.GetClass() in ("PCB_TRACK", "PCB_VIA")
                    and not item.IsLocked()
                    and item.GetParentGroup() is None
                ):
                    candidate.Remove(item)
                    held.append(item)
                    removed_now += 1
            if not removed_now:
                break
            trimmed += removed_now
            label = "trimmed-" + str(iteration)
            after = inspect(label)
        final_groups = _pad_components(candidate)
        if any(
            not members <= final_groups.get(pad, frozenset())
            for pad, members in baseline_groups.items()
        ):
            raise ValueError("candidate breaks an existing connected pad group")
        live = {
            board_copy.uid(t): board_copy.track_signature(t)
            for t in candidate.GetTracks()
        }
        if protected != board_copy.protected_state(candidate) or any(
            live.get(uid) != shape for uid, shape in keep.items()
        ):
            raise ValueError("candidate changed protected or retained boundary copper")
        new_reports = Counter(after["violation_identities"]) - Counter(
            before["violation_identities"]
        )
        if new_reports:
            raise ValueError(
                "candidate introduces new DRC reports: "
                + "; ".join(list(new_reports)[:3])
            )
        if any(
            v > before["unconnected_by_net"].get(n, 0)
            for n, v in after["unconnected_by_net"].items()
        ):
            raise ValueError("candidate regresses per-net connectivity")
        if not any(
            after["unconnected_by_net"].get(n, 0)
            < before["unconnected_by_net"].get(n, 0)
            for n in names
        ):
            raise ValueError("candidate does not improve any selected connection")
        report = {
            "footprint": footprint_ref,
            "nets": sorted(names),
            "bounds_mm": values,
            "local_connections": routed,
            "removed_local_items": removed,
            "retained_outer_pieces": split,
            "trimmed_new_tails": trimmed,
            "before_opens": before["unconnected_by_net"],
            "after_opens": after["unconnected_by_net"],
            "timed_out": result.timed_out,
            "board": str(output / "board.kicad_pcb"),
        }
        accepted = work / "accepted"
        board_copy.snapshot(candidate, accepted / "board.kicad_pcb")
        (accepted / "row-replan.json").write_text(json.dumps(report, indent=2) + "\n")
        os.rename(accepted, output)
    return report
