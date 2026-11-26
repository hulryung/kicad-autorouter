# Repeat a routed channel

The headless channel copier maps source footprints to destination footprints,
then derives net assignments from matching pad numbers. It writes a new project
copy after KiCad DRC passes; the input board and its existing copper are preserved.

Create `channel-map.json`, for example:

```json
{"U101": "U201", "R101": "R201", "C101": "C201"}
```

Run with a Python interpreter that provides `pcbnew`, with `kicad-cli` installed:

```sh
python3 scripts/replicate-channel.py input.kicad_pcb channel-map.json output-channel-2
```

`output-channel-2` must not already exist. Success creates `board.kicad_pcb`,
its project/rule sidecars, and `replication.json` containing the applied net
mapping, added item count, skipped duplicates, excluded shared nets, and before/
after per-net open counts. On failure, no accepted output directory is published.

The same operation is available to headless Python callers:

```python
from autorouter.engine.channel_template import replicate_channel
report = replicate_channel(board, {"U101": "U201", "R101": "R201"}, "channel-2")
```

Requirements and limits:

- Every mapped footprint must exist once. Source and destination reference sets
  must be disjoint, and destination references must be unique.
- Pad numbers must match uniquely. All pads must have matching size, shape,
  attribute and copper layers and fit one translation/rotation within 1 µm.
  Mirrored/flipped placements are rejected.
- Only nets whose pads are entirely contained in the source reference set are
  copied. Include all source-channel components in the mapping. Shared supply
  and ground buses are excluded and reported, preventing accidental replication
  of board-wide copper.
- Tracks, arcs and vias retain their geometry, layer configuration and sizes.
  New items receive new UUIDs and are unlocked. Grouped/generated source copper
  is rejected because its group behavior is not replicated.
- Existing destination copper stays in place. Exact geometry duplicates are
  skipped, so repeating the operation is idempotent.
- Baseline and candidate DRC must be available, with no new **error-severity**
  violations and no increase in any net's unconnected count. Target net classes
  and custom rules therefore participate in acceptance. This is not a guarantee
  of warning-free silkscreen or fabrication readiness.

The copier does not move components or create Kelvin join restrictions. Inspect
the accepted output project before incorporating it into a larger design.
