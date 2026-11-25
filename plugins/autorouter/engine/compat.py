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
        return _legacy_connected_items(connectivity, item)


def resolve_item(board, identifier):
    if hasattr(board, 'ResolveItem'):
        return board.ResolveItem(identifier)
    return board.GetItem(identifier)


def _legacy_connected_items(connectivity, item):
    """KiCad 9 does not wrap the vector<KICAD_T> input of GetConnectedItems.

    Walk its supported direct-neighbour API, then join through individual
    filled islands. Never treat a whole multi-island zone as one conductor.
    Keep this uncached: callers can move/remove copper between queries.
    """
    import pcbnew
    board, code = item.GetBoard(), item.GetNetCode()
    copper = [p for p in board.GetPads() if p.GetNetCode() == code]
    copper += [t for t in board.GetTracks() if t.GetNetCode() == code]
    islands = []
    for zone in board.Zones():
        if zone.GetNetCode() != code or zone.GetIsRuleArea():
            continue
        for layer in zone.GetLayerSet().CuStack():
            filled = zone.GetFilledPolysList(layer)
            for index in range(filled.OutlineCount()):
                poly = pcbnew.SHAPE_POLY_SET(filled.Outline(index))
                for hole in range(filled.HoleCount(index)):
                    poly.AddHole(filled.Hole(index, hole), 0)
                islands.append((zone, layer, poly))
    result, seen_islands = {}, set()
    pending = [item]
    def uid(obj):
        return str(obj.m_Uuid.AsString())
    def touches(obj, layer, poly):
        return obj.IsOnLayer(layer) and poly.Collide(obj.GetEffectiveShape(layer), 0)
    while pending:
        current = pending.pop()
        if uid(current) in result:
            continue
        result[uid(current)] = current
        if current.GetClass() != 'ZONE':
            pending += [other for other in list(connectivity.GetConnectedTracks(current))
                        + list(connectivity.GetConnectedPads(current))
                        if other.GetNetCode() == code and uid(other) not in result]
        queue = [i for i, (_, layer, poly) in enumerate(islands)
                 if i not in seen_islands and current.GetClass() != 'ZONE'
                 and touches(current, layer, poly)]
        while queue:
            index = queue.pop()
            if index in seen_islands:
                continue
            seen_islands.add(index)
            zone, layer, poly = islands[index]
            result[uid(zone)] = zone
            pending += [other for other in copper if uid(other) not in result
                        and touches(other, layer, poly)]
            queue += [i for i, (_, other_layer, other_poly) in enumerate(islands)
                      if i not in seen_islands and other_layer == layer
                      and poly.Collide(other_poly, 0)]
    return list(result.values())


_native_app = None


def native_app():
    """Keep wx alive in Windows board workers (the editor already owns one)."""
    import sys
    global _native_app
    if sys.platform == 'win32':
        import wx
        _native_app = wx.GetApp() or wx.App(False)


def copy_design_settings(board, template):
    if hasattr(board, 'SetDesignSettings'):
        board.SetDesignSettings(template.GetDesignSettings())
        return
    # KiCad 9 exposes the live settings fields but not BOARD's copy setter.
    # Do not SetProject: its ClearProject can unlink the editor's settings.
    source, target = template.GetDesignSettings(), board.GetDesignSettings()
    for name in dir(source):
        if name.startswith('m_'):
            value = getattr(source, name)
            if isinstance(value, (bool, int, float, str)):
                setattr(target, name, value)
    for name in ('m_NetSettings', 'm_TrackWidthList', 'm_ViasDimensionsList',
                 'm_DiffPairDimensionsList', 'm_DRCSeverities', 'm_DrcExclusions'):
        if hasattr(source, name):
            setattr(target, name, getattr(source, name))


def refill_file(path):
    """Worker-only refill for KiCad 9, whose DRC CLI has no --refill-zones."""
    import pcbnew
    native_app()
    board = pcbnew.LoadBoard(path)
    if board is None:
        raise RuntimeError('Could not load DRC copy for refill')
    if list(board.Zones()):
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    if not pcbnew.SaveBoard(path, board):
        raise RuntimeError('Could not save refilled DRC copy')


def zone_filler(board):
    """KiCad 9 requires an initialized DRC engine before any native refill."""
    import pcbnew
    if pcbnew.Version().startswith('9.'):
        import tempfile
        from pathlib import Path
        if board.GetProject() is None:
            # Own a separate settings manager; never load into the editor's.
            manager = pcbnew.SETTINGS_MANAGER(True)
            if board.GetFileName():
                project_path = Path(board.GetFileName()).with_suffix('.kicad_pro')
            else:
                board._autorouter_project_dir = tempfile.TemporaryDirectory(prefix='autorouter-project-')
                project_path = Path(board._autorouter_project_dir.name) / 'board.kicad_pro'
                project_path.write_text('{}', encoding='utf-8')
            manager.LoadProject(str(project_path), False)
            project = manager.GetProject(str(project_path))
            if project is None:
                raise RuntimeError('Could not initialize an isolated refill project')
            board.SetProject(project, True)
            board._autorouter_settings_manager = manager
        with tempfile.TemporaryDirectory(prefix='autorouter-drc-init-') as work:
            # The scripting helper constructs and initializes the board's DRC
            # engine. Calling ZONE_FILLER on a raw BOARD otherwise segfaults.
            if not pcbnew.WriteDRCReport(board, str(Path(work) / 'init.rpt'), pcbnew.EDA_UNITS_MM, False):
                raise RuntimeError('Could not initialize DRC for zone filling')
    return pcbnew.ZONE_FILLER(board)
