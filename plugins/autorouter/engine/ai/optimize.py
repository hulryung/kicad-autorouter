"""Codex-guided routing search that retains only measured improvements.

Each proposal runs on a separate project copy. The original and the best
accepted board are never edited by a rejected or interrupted attempt.
"""
import copy
import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

from . import actions, context, headless, review


def decision(best, candidate, min_gain=1):
    """Gate a reviewer score with independent electrical/geometry evidence."""
    before, after = best['drc'], candidate['drc']
    if not before.get('available') or not after.get('available'):
        return False, 'DRC unavailable; improvement is unverified'
    if 'violation_identities' not in before or 'violation_identities' not in after:
        return False, 'DRC identities missing'
    if Counter(after['violation_identities']) - Counter(before['violation_identities']):
        return False, 'new DRC violations'
    old_open, new_open = before['unconnected_total'], after['unconnected_total']
    if new_open > old_open:
        return False, 'connectivity regressed'
    if candidate['score'] < best['score'] + min_gain:
        return False, 'reviewer score did not improve enough'
    b, c = best['metrics'], candidate['metrics']
    # Connectivity / DRC improvements can legitimately require more copper.
    electrical = (new_open < old_open or
                  len(after['violation_identities']) < len(before['violation_identities']))
    # Otherwise require a real geometry improvement, not just score fluctuation.
    # A 2 mm via penalty allows reasonable length/layer-change trade-offs.
    cost_before = b['length_mm'] + 2 * b['vias']
    cost_after = c['length_mm'] + 2 * c['vias']
    geometry = (cost_after < cost_before - 0.1 and
                c.get('angles', {}).get('other', 0) <= b.get('angles', {}).get('other', 0))
    if not electrical and not geometry:
        return False, 'higher score without measured connectivity, DRC or copper-cost improvement'
    return True, 'score and measured routing improved'


def proposal_key(best_path, acts):
    # Notes and observations do not make the same copper experiment new.
    meaningful = [a for a in acts if a.get('type') in actions.EDITS or a.get('type') == 'set_options']
    return str(best_path), json.dumps(meaningful, sort_keys=True)


def copy_project(source, destination):
    """Copy the PCB and its rule/intent sidecars without changing the input."""
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    for suffix in ('.kicad_pro', '.kicad_dru', '.autorouter.json'):
        src, dst = source.with_suffix(suffix), destination.with_suffix(suffix)
        if src.exists():
            shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()
    return destination


class Optimizer:
    def __init__(self, source, workdir, *, binary='', model='', attempts=8,
                 patience=4, min_gain=1, options=None, log=print, cancelled=None, poll=None):
        self.source, self.workdir = Path(source).resolve(), Path(workdir).resolve()
        if self.workdir == self.source.parent or self.workdir in self.source.parents:
            raise ValueError('Use a separate output directory')
        if attempts < 1 or patience < 1 or min_gain < 1:
            raise ValueError('attempts, patience and min_gain must be positive')
        self.binary, self.model = binary, model
        self.attempts, self.patience, self.min_gain = attempts, patience, min_gain
        self.options, self.log = dict(options or {}), log
        self.history = []
        self.cancelled, self.poll = cancelled, poll
        self.best_state = None

    def _assess(self, board, folder, request, options):
        reviewer = review.Reviewer(board, options, None, None, folder, self.log,
                                   binary=self.binary, model=self.model, max_rounds=1,
                                   cancelled=self.cancelled, poll=self.poll,
                                   previous_issues=self.best_state[1].get("issues", []) if self.best_state else [])
        instructions = (
            '\nPropose one coherent, executable routing experiment. Do not move footprints. '
            'Ground nets without zones still need traces. Never invent a board outline. '
            'Treat existing fixture limitations as fixed; improve routing within them. '
            'The score describes the CURRENT board, not your proposed result. '
            'A candidate is retained only if its re-score rises and KiCad DRC/connectivity '
            'do not regress, with a measured connectivity, DRC or copper-cost improvement. '
            'Rejected experiments are rolled back. Try a different strategy after rejection. '
            'Use ask only when required information prevents any useful routing.\n'
            'Earlier experiments:\n' + json.dumps(self.history[-8:], default=str))
        record = reviewer.run(request + instructions, apply=False)[0]
        if not isinstance(record['score'], int) or not 0 <= record['score'] <= 100:
            raise ValueError('Reviewer score outside 0..100')
        if not record['drc'].get('available'):
            raise RuntimeError('KiCad DRC unavailable: ' + str(record['drc']))
        return record

    def _write(self, best_path, best, status):
        self.best_state = (best_path, best)
        data = {'status': status, 'source': str(self.source),
                'source_sha256': self.source_hash, 'best_board': str(best_path),
                'initial_score': self.initial_score, 'best_score': best['score'],
                'best_metrics': best['metrics'], 'best_drc': best['drc'],
                'attempts': self.history}
        target = self.workdir / 'optimization.json'
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(data, indent=2, default=str) + '\n')
        temp.replace(target)
        return data

    def run(self, request='Route this board and improve its routing quality.'):
        try:
            return self._run(request)
        except BaseException as exc:
            if self.best_state is not None:
                status = 'cancelled' if isinstance(exc, KeyboardInterrupt) or (self.cancelled and self.cancelled()) else 'failed'
                self._write(*self.best_state, status)
            raise

    def _run(self, request):
        import pcbnew
        from .. import session, si
        if (self.workdir / 'optimization.json').exists():
            raise FileExistsError('Output already contains a run; choose a new directory')
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        best_path = copy_project(self.source, self.workdir / 'initial' / self.source.name)
        board = pcbnew.LoadBoard(str(best_path))
        session.refill_zones(board)
        board.BuildConnectivity()
        best = self._assess(board, self.workdir / 'initial' / 'review', request, self.options)
        self.initial_score = best['score']
        self._write(best_path, best, 'running')
        proposal = best
        stalled = 0
        status = 'attempt_limit'
        tried = set()
        for number in range(1, self.attempts + 1):
            if self.cancelled and self.cancelled():
                status = 'cancelled'
                break
            acts = proposal['actions']
            errors = actions.validate({'actions': acts})
            key = proposal_key(best_path, acts)
            if key in tried:
                errors.append('duplicate experiment on the same best board; choose different actions')
            tried.add(key)
            if any(a.get('type') == 'ask' for a in acts):
                self.history.append({'attempt': number, 'accepted': False, 'reason': 'question', 'actions': acts})
                status = 'needs_input'
                break
            if any(a.get('type') == 'route' and a.get('scope') == 'selection' for a in acts):
                errors.append('selection is unavailable; specify explicit nets or sheets')
            if any(a.get('type') == 'move_footprint' for a in acts):
                errors.append('footprint movement is outside this routing search')
            if not any(a.get('type') in actions.EDITS for a in acts):
                status = 'no_proposal'
                break
            record = {'attempt': number, 'actions': acts, 'before_score': best['score']}
            folder = self.workdir / f'attempt-{number:03d}'
            try:
                if errors:
                    raise ValueError('; '.join(errors))
                candidate_path = copy_project(best_path, folder / self.source.name)
                candidate = pcbnew.LoadBoard(str(candidate_path))
                candidate_options = copy.deepcopy(self.options)
                cfg = si.load(candidate)
                base = si.apply_to_options(headless.native_options_from(candidate_options), cfg, si.plan(candidate, cfg))
                route = headless.make_route_fn(candidate, base=base, log=self.log, cancelled=self.cancelled)
                applier = actions.BoardActions(candidate, candidate_options, route,
                                              lambda: session.refill_zones(candidate), self.log)
                # Atomic acceptance happens after the entire batch, so a temporary
                # disconnect during remove+reroute does not prematurely reject it.
                results = [r.as_dict() for r in applier.apply(acts)]
                record['results'] = results
                if self.cancelled and self.cancelled():
                    raise RuntimeError('Cancelled before candidate validation')
                if any(not r['ok'] for r in results):
                    raise RuntimeError('one or more actions failed')
                session.refill_zones(candidate)
                candidate.BuildConnectivity()
                if not pcbnew.SaveBoard(str(candidate_path), candidate):
                    raise RuntimeError('could not save candidate')
                measured = self._assess(candidate, folder / 'review', request, candidate_options)
                accepted, reason = decision(best, measured, self.min_gain)
                record.update(score=measured['score'], metrics=measured['metrics'],
                              drc=measured['drc'], accepted=accepted, reason=reason)
                if accepted:
                    best_path, best, self.options = candidate_path, measured, candidate_options
                    stalled = 0
                else:
                    stalled += 1
            except Exception as exc:
                record.update(accepted=False, reason=f'{type(exc).__name__}: {exc}')
                stalled += 1
            self.history.append(record)
            self.log(f"[optimize] attempt {number}: {record['reason']}; best {best['score']}/100")
            self._write(best_path, best, 'running')
            if self.cancelled and self.cancelled():
                status = 'cancelled'
                break
            if stalled >= self.patience:
                status = 'plateau'
                break
            if number < self.attempts:
                # Re-propose from the accepted board, with rejection evidence.
                board = pcbnew.LoadBoard(str(best_path))
                proposal = self._assess(board, folder / 'next-proposal', request, self.options)
        assert hashlib.sha256(self.source.read_bytes()).hexdigest() == self.source_hash, 'Input PCB changed'
        return self._write(best_path, best, status)
