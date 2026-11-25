"""Indexed wide claims retain exact intersections across overlaps and mutation."""
import random
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))
from autorouter.engine.native.router import _ClaimCells
from autorouter.engine.native.grid import SpanCells

rng=random.Random(7421)
for case in range(100):
    cells=_ClaimCells();expected=set()
    for _ in range(rng.randrange(1,30)):
        piece=SpanCells()
        for _ in range(rng.randrange(1,20)):
            a=rng.randrange(3000);b=a+rng.randrange(1,80)
            piece.add_span(a,b);expected.update(range(a,b))
        cells.add(piece)
    loose=[rng.randrange(3100) for _ in range(30)]
    cells.add(loose);expected.update(loose)
    for count in (0,1,8,200,3100):
        touched=set(rng.sample(range(3100),count))
        assert cells.intersection(touched,sorted(touched))==expected & touched
    # Claims may be extended after the lazy index has already been queried.
    extra=SpanCells();extra.add_span(4000,4100);cells.add(extra)
    assert cells.intersection({3999,4000,4050,4100},[3999,4000,4050,4100])=={4000,4050}
print('test_claim_intersections: sparse/dense/empty overlaps and index invalidation passed')
