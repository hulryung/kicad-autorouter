"""Deterministic SI regression checks, separate from the model's score.

Coupling is a geometric estimate sampled every 0.1 mm, not a field solver.
Track lengths include arcs and the via spans used by the net.
"""
import json
import math
from collections import defaultdict


def _segments(track):
    start, end = track.GetStart(), track.GetEnd()
    if track.GetClass() != 'PCB_ARC':
        return [(start.x, start.y, end.x, end.y)]
    center, mid = track.GetCenter(), track.GetMid()
    a = math.atan2(start.y-center.y, start.x-center.x)
    b = math.atan2(end.y-center.y, end.x-center.x)
    m = math.atan2(mid.y-center.y, mid.x-center.x)
    span = (b-a) % (2*math.pi)
    if (m-a) % (2*math.pi) > span:
        span -= 2*math.pi
    radius = math.hypot(start.x-center.x, start.y-center.y)
    count = max(1, math.ceil(track.GetLength()/100000))
    points = [(center.x+radius*math.cos(a+span*i/count),
               center.y+radius*math.sin(a+span*i/count)) for i in range(count+1)]
    return [(x, y, u, v) for (x, y), (u, v) in zip(points, points[1:])]


def _uncoupled(a, b, gaps, default_gap):
    other = [(t.GetLayer(), t.GetWidth(), seg) for t in b for seg in _segments(t)]
    missed = total = 0.0
    for track in a:
        for ax, ay, bx, by in _segments(track):
            dx, dy = bx-ax, by-ay
            length = math.hypot(dx, dy)
            if not length:
                continue
            count = max(1, math.ceil(length/100000))
            for i in range(count):
                x, y = ax+dx*(i+0.5)/count, ay+dy*(i+0.5)/count
                matched = False
                for layer, width, (sx, sy, ex, ey) in other:
                    if layer != track.GetLayer():
                        continue
                    ux, uy = ex-sx, ey-sy
                    ll = ux*ux+uy*uy
                    if not ll or abs(dx*ux+dy*uy)/(length*math.sqrt(ll)) < 0.966:
                        continue
                    q = max(0.0, min(1.0, ((x-sx)*ux+(y-sy)*uy)/ll))
                    distance = math.hypot(x-sx-q*ux, y-sy-q*uy)
                    pitch = (track.GetWidth()+width)/2 + gaps.get(layer, default_gap)
                    if abs(distance-pitch) <= 30000:
                        matched = True
                        break
                total += length/count
                if not matched:
                    missed += length/count
    return missed, total


def measure(board):
    from .. import si
    from ..native import tuning
    from ..native.extract import pair_partner
    path = si.config_path(board)
    cfg = si.SiConfig.from_dict(json.loads(path.read_text(encoding='utf-8'))) if path and path.exists() else si.SiConfig()
    plan = si.plan(board, cfg)
    tracks = defaultdict(list)
    for t in board.GetTracks():
        if t.GetClass() in ('PCB_TRACK', 'PCB_ARC') and t.GetNetCode() > 0:
            tracks[t.GetNetname()].append(t)
    nets = {n.GetNetname(): n for code, n in board.GetNetsByNetcode().items() if code > 0}
    lengths = {board.FindNet(code).GetNetname(): length/1e6 for code, length in tuning.track_lengths(board).items()}
    checks = {}
    def add(kind, members, error=None, reason=None):
        key = json.dumps([kind, sorted(members)])
        checks[key] = {'nets': sorted(members), 'available': error is not None,
                       'error': error, 'reason': reason}
    for group in plan.length_groups:
        members = group['nets']
        if not members or any(n not in lengths for n in members):
            add('length:'+group['name'], members, reason='unrouted')
            continue
        target = group['target']/1e6 or max(lengths[n] for n in members)
        tolerance = group['tolerance']/1e6
        for name in members:
            add('length:'+group['name']+':'+name, members,
                max(0.0, abs(lengths[name]-target)-tolerance))
    for zc in cfg.impedance:
        for name in sorted(n for n in nets if si.matches(n, zc.nets)):
            spec = plan.impedance.get(name)
            if not spec:
                add('width', [name], reason='stackup unavailable')
            elif not tracks[name]:
                add('width', [name], reason='unrouted')
            else:
                wrong = sum(t.GetLength()/1e6 for t in tracks[name]
                            if t.GetLayer() not in spec['widths'] or abs(t.GetWidth()-spec['widths'][t.GetLayer()]) > 5000)
                add('width', [name], wrong)
    seen = set()
    for name in sorted(nets):
        partner = pair_partner(name, set(nets))
        if not partner or name in seen:
            continue
        seen.update((name, partner))
        if not tracks[name] or not tracks[partner]:
            add('uncoupled', [name, partner], reason='unrouted')
            continue
        nc = board.GetDesignSettings().m_NetSettings.GetEffectiveNetClass(name)
        gap = nc.GetDiffPairGap()
        gaps = plan.impedance.get(name, {}).get('gaps', {})
        missing_a, total_a = _uncoupled(tracks[name], tracks[partner], gaps, gap)
        missing_b, total_b = _uncoupled(tracks[partner], tracks[name], gaps, gap)
        # Sum of both halves, allowing the configured uncoupled budget per half.
        error = max(0.0, (missing_a+missing_b)/1e6 - 2*cfg.pair_uncoupled_max_mm)
        add('uncoupled', [name, partner], error)
    return {'available': True, 'checks': checks, 'warnings': plan.warnings}


def regression(before, after, before_opens):
    if not before.get('available') or not after.get('available'):
        return 'SI metrics unavailable'
    old, new = before['checks'], after['checks']
    for key, check in old.items():
        if check.get('reason') == 'unrouted' or any(before_opens.get(n, 0) for n in check['nets']):
            continue  # New connectivity establishes the first comparable SI baseline.
        candidate = new.get(key)
        if not check['available'] or not candidate or not candidate['available']:
            return f'SI constraint unverified: {key}'
        if candidate['error'] > check['error'] + 0.01:
            return f'SI constraint regressed: {key}'
    return None
