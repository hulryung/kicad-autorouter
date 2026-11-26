# Join a sense connection only at a specified pad

The Kelvin helper routes an isolated sense component to a named pad of the same
net. Existing force copper is an obstacle outside that pad, even though KiCad
normally permits same-net copper to join anywhere. Net names and pad assignments
are preserved; no temporary force-net aliases are written into the result.

Run with KiCad's Python and `kicad-cli` installed:

```sh
python3 scripts/route-kelvin.py input.kicad_pcb U1.3 J1.2 output-kelvin \
  --pitch 0.1 --width 0.25 --separation 0.15 --time-limit 60
```

The source `U1.3` selects its entire currently connected sense component,
including existing partial tracks and vias. `J1.2` is the only permitted join
pad. Both must have the same assigned net and must initially be disconnected.
The output directory must be new. Success writes `board.kicad_pcb`, matching
project/rule sidecars and `kelvin.json`; failure publishes no accepted output.
The input file and existing copper remain unchanged.

- Width and separation are in mm. Without `--width`, the net's routing width is
  used; without `--separation`, force/sense spacing follows the net clearance.
  Custom rules against other nets still apply and are checked by KiCad DRC.
- `--no-vias` restricts the connection to existing copper layers without adding
  layer transitions. Through-hole join pads and multilayer routes are supported.
- The job uses a fixed-copper search. It does not shove existing routes, move
  components, widen power tracks or tune lengths.
- The source net must not have copper zones. A single routing width is required;
  per-layer impedance widths are currently rejected. These restrictions prevent
  ambiguous treatment of shared plane copper and width-dependent join geometry.

The Python API accepts `NativeOptions` for other search settings:

```python
from autorouter.engine.kelvin import route_kelvin
report = route_kelvin(board, "U1.3", "J1.2", "output-kelvin",
                      options=options, separation_mm=0.15, status=print)
```

Acceptance independently checks the emitted copper against the original force
geometry outside the join pad, including the requested separation on every
copper layer. The source must actually reach the join pad, original copper and
protected board state must remain unchanged, and KiCad DRC must show no new
error-severity violations or per-net connectivity regression. A same-net
shortcut can pass ordinary DRC, so the separate geometric check is mandatory.

This validates the current routing operation. It does not install a permanent
KiCad net-class rule: later unrestricted edits or rerouting can change Kelvin
topology. Already connected inputs are rejected rather than silently assuming
that their existing join topology is correct.
