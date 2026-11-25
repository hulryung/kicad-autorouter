#!/usr/bin/env python3
"""Compare matched improve_bench.py runs without averaging away regressions.

python3 scripts/compare-benchmarks.py --case obd=before/metrics.json,after/metrics.json \
    --case cm5=before/metrics.json,after/metrics.json --out comparison.json
"""
import argparse
import json
from pathlib import Path


def compare(before, after):
    required = ('unconnected_after', 'drc', 'drc_errors', 'rule_errors', 'length_mm', 'vias', 'route_seconds', 'board',
                'unconnected_by_net', 'unconnected_by_net_complete')
    for item in (before, after):
        if any(k not in item for k in required):
            raise ValueError('Incomplete benchmark metrics')
        counts = item['unconnected_by_net']
        if (item['unconnected_by_net_complete'] is not True or not isinstance(counts, dict)
                or any(type(n) is not int or n < 0 for n in counts.values())
                or sum(counts.values()) != item['unconnected_after']):
            raise ValueError('Incomplete per-net DRC connectivity evidence')
    if before.get('board') != after.get('board'):
        raise ValueError('Benchmark boards differ')
    failures = []
    for key in ('unconnected_after', 'drc_errors', 'rule_errors'):
        if after[key] > before[key]:
            failures.append(f'{key}: {before[key]} -> {after[key]}')
    for kind, count in after['drc'].items():
        if count > before['drc'].get(kind, 0):
            failures.append(f'DRC {kind}: {before["drc"].get(kind, 0)} -> {count}')
    for net, count in after['unconnected_by_net'].items():
        previous = before['unconnected_by_net'].get(net, 0)
        if count > previous:
            failures.append(f'Net {net} opens: {previous} -> {count}')
    if after.get('overrun') or after.get('pass_errors'):
        failures.append('Routing pass failed or overran its deadline')
    fields = ('unconnected_after', 'drc_errors', 'rule_errors', 'length_mm', 'vias', 'route_seconds')
    return {'passed': not failures, 'failures': failures,
            'before': {k: before[k] for k in fields}, 'after': {k: after[k] for k in fields},
            'delta': {k: round(after[k]-before[k], 3) for k in fields},
            'stages': after.get('stages', []), 'slow_stages': after.get('slow_stages', []),
            'intent': after.get('intent'),
            'note': 'Runtime is observational; use the same input, rules, routing options and deterministic work budget.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', action='append', required=True, help='name=baseline.json,candidate.json')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    cases = {}
    for spec in args.case:
        name, files = spec.split('=', 1)
        before, after = map(Path, files.split(',', 1))
        if name in cases:
            parser.error(f'duplicate case: {name}')
        cases[name] = compare(json.loads(before.read_text()), json.loads(after.read_text()))
    report = {'passed': all(c['passed'] for c in cases.values()), 'cases': cases}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({name: {'passed': c['passed'], 'failures': c['failures'], 'delta': c['delta']}
                      for name, c in cases.items()}, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
