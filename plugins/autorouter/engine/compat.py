"""Small adapters for differences in the KiCad 9/10 SWIG API."""


def board_outline(board, polygon, infer=True):
    try:
        # KiCad 10 added infer-outline before the error-handler argument.
        return board.GetBoardPolygonOutlines(polygon, infer)
    except TypeError:
        # KiCad 9 takes an optional OUTLINE_ERROR_HANDLER* instead.
        return board.GetBoardPolygonOutlines(polygon)


def connected_items(connectivity, item):
    """Return the whole connected copper component on KiCad 9 and 10."""
    try:
        return connectivity.GetConnectedItems(item)
    except TypeError:
        import pcbnew
        return connectivity.GetConnectedItems(item, [pcbnew.PCB_PAD_T,
            pcbnew.PCB_TRACE_T, pcbnew.PCB_ARC_T, pcbnew.PCB_VIA_T, pcbnew.PCB_ZONE_T])
