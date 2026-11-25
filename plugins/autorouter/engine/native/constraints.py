"""Per-job geometric constraints; never add temporary objects to the PCB."""
import fnmatch
import math
from ..si import DEFAULT_ORDER


def rectangles(specs, layers):
    """Validate nm rectangles and resolve copper-layer names (empty means all)."""
    known = {layer.name: layer.index for layer in layers}
    for spec in specs:
        if not isinstance(spec, dict):
            raise ValueError('routing keepouts must be objects')
        rect = spec.get('rect_nm')
        if not isinstance(rect, (list, tuple)) or len(rect) != 4 or not all(
                isinstance(x, (int, float)) and math.isfinite(x) for x in rect):
            raise ValueError('routing keepout rect_nm needs four finite coordinates')
        names = spec.get('layers', [])
        if not isinstance(names, (list, tuple)) or any(n not in known for n in names):
            raise ValueError('routing keepout layers must name existing copper layers')
        margin = spec.get('margin_nm', 0)
        if not isinstance(margin, (int, float)) or not math.isfinite(margin) or margin < 0:
            raise ValueError('routing keepout margin_nm must be nonnegative')
        for field in ('nets', 'groups'):
            value = spec.get(field, [])
            if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) for x in value):
                raise ValueError('routing keepout nets/groups must be lists of names or patterns')
        if any(group not in DEFAULT_ORDER for group in spec.get("groups", [])):
            raise ValueError("routing keepout groups must name existing routing groups")
        x0,y0,x1,y1 = map(int,rect)
        yield spec, (min(x0,x1), min(y0,y1), max(x0,x1), max(y0,y1)), int(margin), [known[n] for n in names] if names else list(known.values())


def applies(spec, net):
    patterns, groups = spec.get('nets', []), spec.get('groups', [])
    return (not patterns and not groups) or net.group in groups or any(
        fnmatch.fnmatchcase(net.name, pattern) or fnmatch.fnmatchcase(net.name.rsplit('/', 1)[-1], pattern)
        for pattern in patterns)


class ViaBlocks:
    """Read-through union: newly placed/ripped-up drill obstacles stay live."""
    def __init__(self, base, masks):
        self.base, self.masks = base, masks

    def __getitem__(self, index):
        return self.base[index] or any(mask[index] for mask in self.masks)


def via_blocks(problem, code, also=0):
    masks = [problem.net_via_blocks[c] for c in (code, also) if c in problem.net_via_blocks]
    return ViaBlocks(problem.via_block, masks) if masks else problem.via_block
