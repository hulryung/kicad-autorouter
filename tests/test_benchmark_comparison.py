"""A good average cannot conceal one board losing connectivity or a DRC type."""
import copy
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('comparison', Path(__file__).resolve().parents[1] / 'scripts/compare-benchmarks.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
base = dict(board='b', unconnected_after=2, drc={'clearance': 1}, drc_errors=1,
            rule_errors=0, length_mm=100, vias=5, route_seconds=10, open_nets=['A'],
            unconnected_by_net={'A': 2}, unconnected_by_net_complete=True)
assert module.compare(base, base)['passed']
changed = copy.deepcopy(base)
changed['drc'] = {'short': 1}
assert not module.compare(base, changed)['passed']
changed = {**base, 'unconnected_after': 1, 'open_nets': ['A'], 'unconnected_by_net': {'B': 1}}
assert not module.compare(base, changed)['passed']
try:
    module.compare(base, {})
except ValueError:
    pass
else:
    raise AssertionError('Incomplete evidence accepted')
print('test_benchmark_comparison: per-board connectivity and DRC-type regressions rejected')

for invalid in ({k: v for k, v in base.items() if k != 'unconnected_by_net'},
                {**base, 'unconnected_by_net_complete': False},
                {**base, 'unconnected_by_net': {'A': 1}}):
    try:
        module.compare(base, invalid)
    except ValueError:
        pass
    else:
        raise AssertionError('Missing or partial per-net evidence accepted')
