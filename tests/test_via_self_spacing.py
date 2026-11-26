"""A path must not drill two adjacent holes before its claims are installed."""

import sys, math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from test_native_grid import make_problem, add_net, wall, RULES
from autorouter.engine.native.problem import NetRules
from autorouter.engine.native import router

p = make_problem(layers=2, w=30, h=20)
net = add_net(p, 1, (3, 10), (26, 10))
wall(p, 0, 12, 0, 19)
wall(p, 1, 14, 0, 19)
rules = NetRules(800000, 200000, 800000, 400000)
p.owners[rules.key] = p.owners.pop(RULES.key)
net.rules = rules
p.hole_to_hole = 250000
p.max_via_drill = 400000
p.options.flat_first = False
p.options.direct_links = False
result = router.run(p)
assert result.routed == 1, result.failed_detail
vias = result.paths[0].vias
assert len(vias) >= 2, vias
assert all(
    math.hypot(x - a, y - b) >= 650000
    for i, (x, y) in enumerate(vias)
    for a, b in vias[i + 1 :]
), vias
print(
    "test_via_self_spacing: close consecutive holes rerouted within shared search budget"
)
