"""Nearby free pockets do not imply that a failed endpoint has an exit."""

from test_native_grid import make_problem, add_net, wall, RULES
from autorouter.engine.native import router
from autorouter.engine.native.grid import BLOCKED

p = make_problem(layers=1, w=60, h=40)
add_net(p, 1, (10, 20), (50, 20))
p.options.allow_vias = False
p.options.rip_rounds = 0
p.options.direct_links = False
for x in range(9, 12):
    for y in range(19, 22):
        if (x, y) != (10, 20):
            p.owners[RULES.key][p.grid.index(0, x, y)] = BLOCKED
result = router.run(p)
a, b = result.failed_detail[0]["ends"]
assert int(a["free_cells_around"].split("/")[0]) > 100
assert a["escape_probe"]["reason"] == "enclosed_on_reported_layer", a
assert a["escape_probe"]["reachable_cells"] == 1
assert b["escape_probe"]["reason"] == "reaches_probe_boundary", b
assert a["escape_probe"]["via_feasibility"] == "not_evaluated"
p.owners[RULES.key][p.grid.index(0, 10, 20)] = BLOCKED
probe = router.Router(p)._end_probe(p.connections[0], 0, p.grid.index(0, 10, 20))
assert probe["reason"] == "no_legal_seed_at_end", probe
print(
    "test_end_diagnostics: disconnected free pockets, open end and blocked seed distinguished"
)
# Geometry alone must not imply an exit on a disabled routing layer.
r = router.Router(p)
r.routable = [False]
probe = r._end_probe(p.connections[0], 1, p.grid.index(0, 50, 20))
assert probe["reason"] == "layer_disabled" and not probe["layer_enabled"], probe
# A 0.5 mm neighbourhood can be free while the surrounding exit is walled.
p = make_problem(layers=1, w=60, h=40)
add_net(p, 1, (10, 20), (50, 20))
p.options.allow_vias = False
p.options.rip_rounds = 0
p.options.direct_links = False
for x in range(3, 18):
    for y in range(13, 28):
        if x in (3, 17) or y in (13, 27):
            p.owners[RULES.key][p.grid.index(0, x, y)] = BLOCKED
result = router.run(p)
a = result.failed_detail[0]["ends"][0]
assert a["free_cells_around"] == "121/121", a
assert a["escape_probe"]["reason"] == "enclosed_on_reported_layer", a
assert a["escape_probe"]["radius_mm"] == 2.0
