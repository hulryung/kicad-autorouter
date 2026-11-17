"""A designer's routing style, learnt from a finished board and applied to
the router's run on a board laid out the same way (the next revision, a
sister board, or the same board stripped of its tracks).

What is learnt is what the net classes do not say but the copper does:

  layers         an inner layer the designer left to pours (under PLANE_SHARE
                 of the track length on it) is not routed
  class widths   the width a net class is really drawn at (length-weighted
                 median) when it is wider than the class says: power out at
                 1.5 mm where the class says 0.6
  class vias     the via a net class really uses, when it is not the class via

The style is plain JSON, so it can be read, edited and kept beside the board:

  {"layers": {"In1.Cu": "plane", ...}, "class_width_mm": {"PWR_OUT": 1.5},
   "class_via_mm": {"PWR": [0.6, 0.3]}}
"""
import json
from collections import Counter, defaultdict
from typing import Dict, List, Optional

MM = 1_000_000
PLANE_SHARE = 0.05        # an inner layer with less than this share of the track length is a plane layer
MIN_CLASS_MM = 20.0       # a class drawn shorter than this tells nothing about its width


def _class_of(board, item) -> str:
    try:
        name = item.GetNetClassName()
    except Exception:
        name = ""
    return str(name) or "Default"


def _weighted_median(counter: Counter) -> float:
    total = sum(counter.values())
    acc = 0.0
    for value, weight in sorted(counter.items()):
        acc += weight
        if acc >= total / 2:
            return value
    return 0.0


def learn(board) -> dict:
    """The style of a routed board (tracks and vias as the designer left them)."""
    import pcbnew
    stack = list(board.GetEnabledLayers().CuStack())
    names = {lid: board.GetLayerName(lid) for lid in stack}
    layer_len: Counter = Counter()
    width_len: Dict[str, Counter] = defaultdict(Counter)
    vias: Dict[str, Counter] = defaultdict(Counter)
    for t in board.GetTracks():
        if t.GetClass() == "PCB_VIA":
            vias[_class_of(board, t)][(t.GetWidth(pcbnew.F_Cu), t.GetDrillValue())] += 1
            continue
        layer_len[t.GetLayer()] += t.GetLength()
        width_len[_class_of(board, t)][t.GetWidth()] += t.GetLength()
    total = sum(layer_len.values()) or 1

    poured = set()
    for z in board.Zones():
        if not z.GetIsRuleArea() and z.GetNetCode() > 0:
            poured.update(lid for lid in stack if z.IsOnLayer(lid))
    layers = {}
    for i, lid in enumerate(stack):
        inner = 0 < i < len(stack) - 1
        share = layer_len[lid] / total
        layers[names[lid]] = "plane" if inner and lid in poured and share < PLANE_SHARE else "signal"

    ns = board.GetDesignSettings().m_NetSettings
    classes = {str(n): c for n, c in ns.GetNetclasses().items()}
    classes["Default"] = ns.GetDefaultNetclass()
    widths, via_sizes = {}, {}
    for name, counter in width_len.items():
        nc = classes.get(name)
        if nc is None or sum(counter.values()) < MIN_CLASS_MM * MM:
            continue
        w = _weighted_median(counter)
        if w > nc.GetTrackWidth():
            widths[name] = round(w / MM, 3)
    for name, counter in vias.items():
        nc = classes.get(name)
        if nc is None or sum(counter.values()) < 3:
            continue
        (d, h), _ = counter.most_common(1)[0]
        if (d, h) != (nc.GetViaDiameter(), nc.GetViaDrill()):
            via_sizes[name] = [round(d / MM, 3), round(h / MM, 3)]
    return {"layers": layers, "class_width_mm": widths, "class_via_mm": via_sizes}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def routable_layers(board, style: dict) -> Optional[List[bool]]:
    """Top to bottom: False on the layers the style keeps for pours (None: no
    say). Roles are given per layer name ("In1.Cu"), or for "inner" / "outer"
    layers as a whole (a guide written for any board); a name wins."""
    roles = style.get("layers") or {}
    if not roles:
        return None
    stack = list(board.GetEnabledLayers().CuStack())
    out = []
    for i, lid in enumerate(stack):
        group = "outer" if i in (0, len(stack) - 1) else "inner"
        role = roles.get(board.GetLayerName(lid), roles.get(group, "signal"))
        out.append(role != "plane")
    return out if any(out) else None


def apply_to_options(board, style: dict, options) -> List[str]:
    """Put a style into a run's NativeOptions (the board itself is not
    changed): plane layers, per-class widths and vias. Returns what it set."""
    lines = []
    mask = routable_layers(board, style)
    if mask is not None:
        current = options.routable_layers or [True] * len(mask)
        options.routable_layers = [a and b for a, b in zip(mask, current)]
        names = [board.GetLayerName(l) for l in board.GetEnabledLayers().CuStack()]
        off = [n for n, ok in zip(names, options.routable_layers) if not ok]
        if off:
            lines.append("left to pours, not routed: " + ", ".join(off))
    for name, w in (style.get("class_width_mm") or {}).items():
        options.class_widths[name] = int(round(float(w) * MM))
        lines.append(f"{name}: track {w:g} mm")
    for name, (d, h) in (style.get("class_via_mm") or {}).items():
        options.class_vias[name] = (int(round(float(d) * MM)), int(round(float(h) * MM)))
        lines.append(f"{name}: via {d:g}/{h:g} mm")
    return lines


def apply_net_classes(board, style: dict) -> List[str]:
    """Write the style's widths and vias into the board's net classes, so the
    router and KiCad's DRC both see them. Returns what changed, for the log."""
    ns = board.GetDesignSettings().m_NetSettings
    classes = {str(n): c for n, c in ns.GetNetclasses().items()}
    classes["Default"] = ns.GetDefaultNetclass()
    lines = []
    for name, w in (style.get("class_width_mm") or {}).items():
        nc = classes.get(name)
        if nc is None:
            continue
        lines.append(f"{name}: track {nc.GetTrackWidth() / MM:g} -> {w:g} mm")
        nc.SetTrackWidth(int(round(w * MM)))
    for name, (d, h) in (style.get("class_via_mm") or {}).items():
        nc = classes.get(name)
        if nc is None:
            continue
        lines.append(f"{name}: via {nc.GetViaDiameter() / MM:g}/{nc.GetViaDrill() / MM:g} -> {d:g}/{h:g} mm")
        nc.SetViaDiameter(int(round(d * MM)))
        nc.SetViaDrill(int(round(h * MM)))
    if lines:
        from .board_setup import _refresh_classes
        _refresh_classes(board)
    return lines
