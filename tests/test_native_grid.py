"""The built-in router's pure-Python core on synthetic boards: an obstacle to
go around, a wall that forces a via, and clearance that is respected."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from autorouter.engine.native import router
from autorouter.engine.native.grid import BLOCKED, Grid
from autorouter.engine.native.problem import (Cluster, Connection, LayerInfo, NativeOptions, NetInfo,
                                              NetRules, RoutingProblem)

PITCH = 100_000           # 0.1 mm
RULES = NetRules(width=200_000, clearance=200_000, via_diameter=600_000, via_drill=300_000)


def make_problem(layers=2, w=60, h=40, via_cost=20):
    grid = Grid(0, 0, PITCH, w, h, layers)
    infos = [LayerInfo(i, i, f"L{i}", i % 2 == 0) for i in range(layers)]
    owners = {RULES.key: grid.new_owner_map()}
    return RoutingProblem(grid, infos, owners, bytearray(grid.plane), {}, [],
                          NativeOptions(via_cost=via_cost, retry_failed=False))


def add_net(problem, code, a, b, layer=0):
    """Two one-cell 'pads' at cells a and b on a layer."""
    grid = problem.grid
    net = NetInfo(code, f"N{code}", RULES)
    for (cx, cy) in (a, b):
        cl = Cluster(cells={grid.index(layer, cx, cy)}, anchors=[(cx, cy)])
        net.clusters.append(cl)
        grid.mark(problem.owners[RULES.key], [grid.index(layer, cx, cy)], code)
    problem.nets[code] = net
    problem.connections.append(Connection(code, 0, 1, abs(a[0] - b[0]) + abs(a[1] - b[1])))
    return net


def wall(problem, layer, x, y0, y1, owner=BLOCKED):
    grid = problem.grid
    grid.mark(problem.owners[RULES.key], [grid.index(layer, x, y) for y in range(y0, y1 + 1)], owner)


def main():
    # 1. Straight shot on one layer, no vias needed.
    p = make_problem(layers=1)
    add_net(p, 1, (5, 20), (50, 20))
    r = router.run(p)
    assert r.routed == 1 and not r.failed, r.failed
    path = r.paths[0]
    assert not path.vias and len(path.segments) == 1, path
    assert path.segments[0] == (0, 5 * PITCH, 20 * PITCH, 50 * PITCH, 20 * PITCH)

    # 2. A wall with a gap: the route goes around through the gap.
    p = make_problem(layers=1)
    add_net(p, 1, (5, 20), (50, 20))
    wall(p, 0, 30, 0, 10)
    wall(p, 0, 30, 14, 39)
    r = router.run(p)
    assert r.routed == 1, "no path through the gap"
    xs = {(x1, y1, x2, y2) for _, x1, y1, x2, y2 in r.paths[0].segments}
    crossing_rows = [y1 for x1, y1, x2, y2 in xs if x1 <= 30 * PITCH <= x2 or x2 <= 30 * PITCH <= x1]
    assert crossing_rows and all(11 * PITCH <= y <= 13 * PITCH for y in crossing_rows), crossing_rows

    # 3. A full wall on layer 0, free layer 1: a via pair is used.
    p = make_problem(layers=2)
    add_net(p, 1, (5, 20), (50, 20))
    wall(p, 0, 30, 0, 39)
    r = router.run(p)
    assert r.routed == 1, "no path with a via"
    assert len(r.paths[0].vias) == 2, r.paths[0].vias
    assert {s[0] for s in r.paths[0].segments} == {0, 1}

    # 4. A full wall and vias forbidden: the connection fails and is reported.
    p = make_problem(layers=2)
    p.options.allow_vias = False
    add_net(p, 1, (5, 20), (50, 20))
    wall(p, 0, 30, 0, 39)
    r = router.run(p)
    assert r.routed == 0 and len(r.failed) == 1

    # 5. A routed net blocks a later one: the second must go around it, and
    #    the first net's cells now belong to it in the owner map.
    p = make_problem(layers=1, via_cost=20)
    add_net(p, 1, (5, 10), (50, 10))
    add_net(p, 2, (5, 30), (50, 30))
    r = router.run(p)
    assert r.routed == 2
    own = p.owners[RULES.key]
    assert own[p.grid.index(0, 25, 10)] == 1 and own[p.grid.index(0, 25, 30)] == 2
    # own half width + clearance + routing half width = 0.4 mm = 4 cells either side
    assert own[p.grid.index(0, 25, 14)] == 1 and own[p.grid.index(0, 25, 15)] == 0

    # 6. Cells claimed by two nets become blocked for everyone.
    g = Grid(0, 0, PITCH, 4, 1, 1)
    m = g.new_owner_map()
    g.mark(m, [0, 1], 7)
    g.mark(m, [1, 2], 8)
    assert list(m) == [7, BLOCKED, 8, 0]

    # 7. Polygon fill respects holes and the outside band.
    g = Grid(0, 0, PITCH, 20, 20, 1)
    square = [(2 * PITCH, 2 * PITCH), (17 * PITCH, 2 * PITCH), (17 * PITCH, 17 * PITCH), (2 * PITCH, 17 * PITCH)]
    hole = [(8 * PITCH, 8 * PITCH), (11 * PITCH, 8 * PITCH), (11 * PITCH, 11 * PITCH), (8 * PITCH, 11 * PITCH)]
    inside = set(g.polygon_cells(0, square, [hole]))
    assert g.index(0, 5, 5) in inside and g.index(0, 9, 9) not in inside and g.index(0, 0, 0) not in inside
    outside = set(g.outside_polygon_cells(0, square, [hole], PITCH))
    assert g.index(0, 0, 0) in outside and g.index(0, 3, 3) in outside and g.index(0, 5, 5) not in outside
    print("test_native_grid: ok")


if __name__ == "__main__":
    main()


def pair_case():
    """Two nets side by side, 0.5 mm apart, from x=5 to x=50 on one layer:
    routed as a coupled pair they must end up as two parallel tracks a
    pair-gap apart, each starting and ending at its own exit."""
    from autorouter.engine.native.problem import NetInfo as NI
    p = make_problem(layers=1, w=60, h=40, via_cost=20)
    grid = p.grid
    gap = 250_000
    width = 200_000
    rules = NetRules(width=width, clearance=200_000, via_diameter=600_000, via_drill=300_000)
    p.owners[(2 * width + gap, rules.clearance)] = grid.new_owner_map()
    p.owners[rules.key] = p.owners.pop(RULES.key)
    for code, y in ((1, 18), (2, 23)):
        net = NI(code, f"N{code}", rules, partner=3 - code, pair_gap=gap)
        for cx in (5, 50):
            idx = grid.index(0, cx, y)
            cl = Cluster(cells={idx}, anchors=[(cx, y)], refs={f"J{cx}"})
            net.clusters.append(cl)
            # exits point outward along +x / -x from a pad two cells further out
            px = grid.nm_x(cx - 2) if cx == 5 else grid.nm_x(cx + 2)
            net.endpoints[idx] = (grid.nm_x(cx), grid.nm_y(y), px, grid.nm_y(y), width)
            for own in p.owners.values():
                grid.mark(own, [idx], code)
        p.nets[code] = net
        p.connections.append(Connection(code, 0, 1, 45, 0, width, pair=1))
    r = router.run(p)
    assert r.pairs == 1, (r.pairs, getattr(r, "pair_log", None))
    assert r.routed == 2 and not r.failed
    # the coupled run: horizontal segments of both nets exactly a pair-gap apart
    runs = {path.net: [s for s in path.segments if s[2] == s[4] and abs(s[3] - s[1]) >= 10 * PITCH]
            for path in r.paths}
    coupled = [(a, b) for a in runs[1] for b in runs[2] if abs(a[2] - b[2]) == width + gap]
    assert coupled, runs
    a, b = coupled[0]
    assert abs(a[3] - a[1]) >= 25 * PITCH, ("coupling should start close to the pins", a)
    print("test_native_grid: pair ok")

    # N. Time limit: with none left, the plain pass tries nothing, every
    # connection is reported open and timed_out is set; cancelled is not.
    p = make_problem(layers=1)
    add_net(p, 1, (5, 20), (50, 20))
    add_net(p, 2, (5, 10), (50, 10))
    p.options.time_limit_s = 1e-9
    r = router.run(p)
    assert r.timed_out and not r.cancelled, (r.timed_out, r.cancelled)
    assert r.routed == 0 and len(r.failed) == 2, (r.routed, r.failed)
    # with a real limit the same board routes in full
    p = make_problem(layers=1)
    add_net(p, 1, (5, 20), (50, 20))
    p.options.time_limit_s = 60
    r = router.run(p)
    assert r.routed == 1 and not r.timed_out
    print("test_native_grid: time limit ok")


def swap_case():
    """A pair whose pins are in opposite order at the two ends: coupled with
    a via swap, one line crossing under the other on the second layer
    through two vias; with the swap turned off the same pair is an exception."""
    from autorouter.engine.native.problem import NetInfo as NI

    def build(swap):
        p = make_problem(layers=2, w=80, h=40, via_cost=20)
        p.options.pair_via_swap = swap
        grid = p.grid
        gap, width = 150_000, 150_000
        rules = NetRules(width=width, clearance=150_000, via_diameter=450_000, via_drill=200_000)
        p.owners = {rules.key: grid.new_owner_map(), (2 * width + gap, rules.clearance): grid.new_owner_map()}
        for code, ys in ((1, (18, 21)), (2, (21, 18))):
            net = NI(code, f"N{code}", rules, partner=3 - code, pair_gap=gap)
            for cx, y in zip((5, 70), ys):
                idx = grid.index(0, cx, y)
                net.clusters.append(Cluster(cells={idx}, anchors=[(cx, y)], refs={f"J{cx}"}))
                px = grid.nm_x(cx - 2) if cx == 5 else grid.nm_x(cx + 2)
                net.endpoints[idx] = (grid.nm_x(cx), grid.nm_y(y), px, grid.nm_y(y), width)
                for own in p.owners.values():
                    grid.mark(own, [idx], code)
            p.nets[code] = net
            p.connections.append(Connection(code, 0, 1, 65, 0, width, pair=1))
        return p

    r = router.run(build(True))
    assert r.pairs == 1, (r.pairs, getattr(r, "pair_log", None))
    assert r.routed == 2 and not r.failed
    hops = [path for path in r.paths if path.vias]
    assert len(hops) == 1 and len(hops[0].vias) == 2, [(p.net, p.vias) for p in r.paths]
    assert any(s[0] == 1 for s in hops[0].segments), hops[0].segments
    r = router.run(build(False))
    assert r.pairs == 0, r.pairs                 # routed separately (here only one fits)
    print("test_native_grid: via swap ok")


if __name__ == "__main__":
    pair_case()
    swap_case()
