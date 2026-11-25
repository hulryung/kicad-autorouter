"""Small adapters for differences in the KiCad 9/10 SWIG API."""


def board_outline(board, polygon, infer=True):
    try:
        # KiCad 10 added infer-outline before the error-handler argument.
        return board.GetBoardPolygonOutlines(polygon, infer)
    except TypeError:
        # KiCad 9 takes an optional OUTLINE_ERROR_HANDLER* instead.
        return board.GetBoardPolygonOutlines(polygon)
