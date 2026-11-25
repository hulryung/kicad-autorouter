"""Wide clearance claims keep exact copper ownership through rip-up/rebuild."""
import sys
from pathlib import Path
from array import array
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.native import router
from autorouter.engine.native.grid import Grid
from autorouter.engine.native.problem import (Connection, LayerInfo, NativeOptions, NetInfo,
                                             NetRules, RoutedPath, RoutingProblem)


def exercise(module=router, count=12):
    g = Grid(0, 0, 100000, 300, max(160, count*7+50), 2)
    rules = [NetRules(200000, clearance, 600000, 300000)
             for clearance in (150000, 1000000, 2000000, 3000000)]
    p = RoutingProblem(g, [LayerInfo(i, i, str(i), bool(i)) for i in range(2)],
                       {r.key: g.new_owner_map() for r in rules}, bytearray(g.plane),
                       {i: NetInfo(i, str(i), rules[(i-1) % len(rules)]) for i in range(1, count+1)},
                       [], NativeOptions())
    p.pair_clearance = lambda a, b, layer=None: max(a, b) + (1000000 if layer == 1 else 0)
    p.ring_free = lambda *args: lambda cells: [c for c in cells if c % g.w != 140]
    r = module.Router(p)
    for i in range(1, count+1):
        y = (20+i*7) * g.pitch
        rp = RoutedPath(i, 200000, [(i % 2, 2000000, y, 27000000, y),
                                  (i % 2, 27000000, y, 26000000, y+300000)],
                        [(26000000, y)], 600000, 300000, pid=i)
        path = module._Path(i, Connection(i, 0, 0, 1), rp, [])
        r.paths[i] = path
        r._claim(path)
        for key, cells in path.claims.items():
            explicit = list(cells)
            assert path.boxes[key] == module.Router._cell_box(r, explicit)
    before = [array('i', x) for x in p.owners.values()]
    removed = [r.paths.pop(i) for i in range(2, count+1, 3)]
    r._unclaim(removed)
    after = [array('i', x) for x in p.owners.values()]
    routed = [array('i', x) for x in r.routed.values()]
    via = bytes(p.via_block)
    r._rebuild_maps()
    assert after == list(p.owners.values())
    assert routed == list(r.routed.values())
    assert via == bytes(p.via_block)
    assert before != after
    return before, after, routed, via


def main():
    exercise()
    from autorouter.engine.native.grid import SpanCells
    cells = router._ClaimCells()
    wide = SpanCells()
    for y in range(100):
        wide.add_span(y*1000+10, y*1000+990)
    cells.add(wide)
    assert list(cells) == list(wide)
    retained = sys.getsizeof(cells.spans) + sum(sys.getsizeof(s) for s in cells.spans) + sys.getsizeof(cells.loose)
    expanded = sys.getsizeof(wide) + sum(sys.getsizeof(i) for i in wide)
    assert retained < expanded / 20, (retained, expanded)
    print(f'test_compact_claims: exact rip-up maps; wide claim storage {expanded} -> {retained} bytes')


if __name__ == '__main__':
    main()
