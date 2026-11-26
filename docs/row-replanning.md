# Joint local row replanning

The headless `replan_row` operation jointly removes and restores selected local
escapes around a perimeter-row IC. Isolated pad ports receive staggered goals under
the package body on another copper layer; the router then joins those escapes
to remote pads. This is useful when neighbouring escapes and crossing tracks
occupy the available via sites.

Run with KiCad's Python (the same interpreter as the other headless scripts):

```sh
python scripts/replan-row.py input.kicad_pcb U1 output-directory \
  --net MUX_A --net MUX_B --net CROSSING_SIGNAL \
  --bounds 10 15 25 30 --escape-layer In2.Cu --escape-layer B.Cu
```

The coordinates are an example; choose a rectangle containing every pad of the
selected footprint. Repeat `--net` for the row nets **and crossing blockers**
that may be replanned. Net names are exact, not patterns. The command enables
all copper layers for routing; the escape-layer arguments restrict the inward
goals, not the complete route. The default goal layer is B.Cu.

The output is a new directory with `board.kicad_pcb`, matching project/rule
files, and `row-replan.json`. The input is unchanged. No output is published
unless full refilled DRC introduces no new reports, existing connected pad
groups stay connected, per-net opens do not increase, and at least one selected
net improves. A failed local restoration is rejected before global routing.
Only newly created dangling tracks/vias may be trimmed before final validation.

Locked and grouped copper, arcs, zones and pads are preserved. Straight tracks
crossing the rectangle are split at its boundary; their outer pieces are kept.
Original items inside the rectangle may therefore receive new UUIDs. Caller
selection of the affected nets and region is intentional; this is not an
automatic whole-board repair mode or an editor Undo operation.

The implementation currently supports axis-aligned perimeter rows (SOIC/TSSOP/QFP), including
90-degree rotations. It rejects selected nets that have copper zones. Default
local/global pitches are 0.05/0.15 mm; inward depth is 1.3 mm with a 0.7 mm
stagger. These are adjustable and must fit inside the package's two rows.
Per-layer impedance widths are not supported. Routing, copper/via clearances, rule areas and layer restrictions are checked.
A chosen goal need not remain if a legal shorter connection makes it redundant.

For Python callers:

```python
from autorouter.engine.row_replan import replan_row
report = replan_row(board, "U1", ["MUX_A", "MUX_B", "CROSSING_SIGNAL"],
                    (10, 15, 25, 30), "output-directory",
                    escape_layers=["In2.Cu", "B.Cu"])
```

`options=NativeOptions(...)` controls routing layers and stage budgets. The CLI
`--time-limit` is per local/global routing stage; DRC/refill validation adds time.
Native C++ refill calls cannot be interrupted midway. Run this operation in an
idle headless worker, not inside the PCB editor process.

## Validation

Native tests cover joint inward escapes past an outward obstacle, locked-copper
preservation, 90-degree rotation, CLI input preservation, invalid selections,
and rejection when alternate-layer goals cannot be reached without vias.

On the private Cell8 reproduction, the operation restores 17 local connections
and joins all four previously open MUX nets. Full refilled DRC retains the exact
18 baseline reports; opens fall from five to one, with no existing connected
pad group broken. Private board files are not included in the repository.
