#!/usr/bin/env python3
"""Print native API/fixture diagnostics without swallowing pcbnew load errors."""
import sys
from pathlib import Path

import pcbnew

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
sys.path.insert(0, str(ROOT / 'tests'))
from autorouter.engine.compat import connected_items
from test_wired_pieces import new_board, add_pad, add_track

print('KiCad:', pcbnew.Version(), flush=True)
print('Connected API:', pcbnew.CONNECTIVITY_DATA.GetConnectedItems.__doc__, flush=True)
print('Board version:', getattr(pcbnew, 'SEXPR_BOARD_FILE_VERSION', 'not exported'), flush=True)
try:
    board = pcbnew.PCB_IO_MGR.Load(pcbnew.PCB_IO_MGR.KICAD_SEXP,
                                  str(ROOT / 'tests/fixtures/obd2-adapter.kicad_pcb'))
    print('Fixture:', board, flush=True)
except Exception as exc:
    print('Fixture load error:', repr(exc), flush=True)
board, nets = new_board(['SIG'])
fp = pcbnew.FOOTPRINT(board)
board.Add(fp)
add_pad(fp, 1, 5, 10, nets['SIG'])
add_pad(fp, 2, 15, 10, nets['SIG'])
add_track(board, nets['SIG'], (5, 10), (10, 10))
add_track(board, nets['SIG'], (10, 10), (15, 10))
board.BuildConnectivity()
try:
    print('Connected component:', [item.GetClass() for item in connected_items(
        board.GetConnectivity(), next(iter(board.GetPads())))], flush=True)
except Exception as exc:
    print('Connectivity error:', repr(exc), flush=True)
