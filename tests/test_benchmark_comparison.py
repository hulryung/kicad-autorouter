"""A good average cannot conceal one board losing connectivity or a DRC type."""
import copy
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('comparison', Path(__file__).resolve().parents[1] / 'scripts/compare-benchmarks.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
base = dict(board='b', unconnected_after=2, drc={'clearance': 1}, drc_errors=1,
            rule_errors=0, length_mm=100, vias=5, route_seconds=10, open_nets=['A'])
assert module.compare(base, base)['passed']
changed = copy.deepcopy(base)
changed['drc'] = {'short': 1}
assert not module.compare(base, changed)['passed']
changed = {**base, 'unconnected_after': 1, 'open_nets': ['B']}
assert not module.compare(base, changed)['passed']
try:
    module.compare(base, {})
except ValueError:
    pass
else:
    raise AssertionError('Incomplete evidence accepted')
print('test_benchmark_comparison: per-board connectivity and DRC-type regressions rejected')
