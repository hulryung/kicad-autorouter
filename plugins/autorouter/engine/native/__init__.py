"""
The built-in router: routes on KiCad's own board model through the PCB API.

  extract.py   reads the board (pads, tracks, keepouts, outline, net classes)
               into a RoutingProblem                       - pcbnew, main thread
  grid.py      the multi-layer occupancy grid and rasterizers - pure Python
  astar.py     one connection: multi-source, multi-layer A*   - pure Python
  router.py    all connections, ordering, retries, progress   - pure Python, worker thread
  apply.py     turns routed paths into PCB_TRACK / PCB_VIA     - pcbnew, main thread
"""
