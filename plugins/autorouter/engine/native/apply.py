"""Putting routed paths on the board as tracks and vias (main thread only)."""
from typing import Iterable, List

import pcbnew

from .problem import LayerInfo, RoutedPath


def add_path(board, layers: List[LayerInfo], path: RoutedPath) -> List[object]:
    """Create the items for one path and add them to the board; returns them."""
    added = []
    for layer, x1, y1, x2, y2 in path.segments:
        if (x1, y1) == (x2, y2):
            continue
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(x1, y1))
        track.SetEnd(pcbnew.VECTOR2I(x2, y2))
        track.SetWidth(path.width)
        track.SetLayer(layers[layer].kicad_id)
        track.SetNetCode(path.net)
        board.Add(track)
        added.append(track)
    top, bottom = pcbnew.F_Cu, pcbnew.B_Cu
    for x, y in path.vias:
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(x, y))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetWidth(path.via_diameter)
        via.SetDrill(path.via_drill)
        via.SetLayerPair(top, bottom)
        via.SetNetCode(path.net)
        board.Add(via)
        added.append(via)
    return added


def add_paths(board, layers: List[LayerInfo], paths: Iterable[RoutedPath]) -> int:
    count = 0
    for path in paths:
        count += len(add_path(board, layers, path))
    return count
