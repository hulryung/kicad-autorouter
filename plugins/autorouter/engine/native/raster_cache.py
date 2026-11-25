"""Bounded, per-job cache of geometry cells, never ownership or connectivity."""
from array import array
from collections import OrderedDict
from itertools import chain
import sys
import re


class FrozenCells:
    def __init__(self, cells):
        spans = getattr(cells, 'spans', None)
        self.spans = tuple(spans) if spans is not None else None
        self.loose = array('Q', cells) if spans is None else None
        self.count = len(cells)
        self.bytes = sys.getsizeof(self)
        if self.spans is not None:
            self.bytes += sys.getsizeof(self.spans) + sum(sys.getsizeof(s) + sum(sys.getsizeof(i) for i in s) for s in self.spans)
        else:
            self.bytes += sys.getsizeof(self.loose)

    def __len__(self):
        return self.count

    def __iter__(self):
        return chain.from_iterable(range(a,b) for a,b in self.spans) if self.spans is not None else iter(self.loose)


class RasterCache:
    def __init__(self, max_bytes):
        self.max_bytes = max(0, int(max_bytes))
        self.bytes = self.hits = self.misses = 0
        self.entries = OrderedDict()

    def __deepcopy__(self, memo):
        # Pipeline passes copy their options; their geometry cache is shared.
        return self

    def get(self, key, build):
        if key in self.entries:
            self.hits += 1
            value = self.entries.pop(key)
            self.entries[key] = value
            return value[0]
        self.misses += 1
        cells = build()
        if not self.max_bytes or not cells:
            return cells
        frozen = FrozenCells(cells)
        size = frozen.bytes + len(repr(key)) * 2 + 512
        if size > self.max_bytes:
            return cells
        while self.entries and self.bytes + size > self.max_bytes:
            _, (_, old_size) = self.entries.popitem(last=False)
            self.bytes -= old_size
        self.entries[key] = frozen, size
        self.bytes += size
        return frozen


def shape_key(shape):
    try:
        value = shape.Format()
        # Some KiCad wrappers format polygons only as 'shape 4', without
        # their vertices. Such a string is not a geometry fingerprint.
        return ('shape', value) if isinstance(value, str) and value and not re.search(r'\bshape\s+\d', value) else None
    except Exception:
        return None                 # old/unsupported shape wrappers remain uncached
