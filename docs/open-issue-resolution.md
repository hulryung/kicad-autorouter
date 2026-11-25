# Open issue resolution

Objective: verify and resolve every issue open on 2026-11-25. An issue is closed
only after all its requested behaviors are implemented or disproved with
reproducible evidence, relevant tests pass, and the change reaches main.
Routing experiments use copies; private board inputs are not published.

| Issue | Acceptance requirements | State |
| --- | --- | --- |
| #1 | Other-net pour polygons incur a routing cost or block, including mixed-net layers; keep own-net pours usable | Merged in PR #25; native tests and all nine CI jobs passed |
| #2 | Distinguish blocked exits, exhausted search and time budget in failure details; review optional unchanged-pass stopping | Merged in PR #25; verdicts and no-progress stopping verified |
| #3 | Connect multi-pin fine-pitch supply nets with legal pin escapes and alternate-layer joins or a ring | Pending reproduction |
| #4 | Close short pad-to-track/via/pad gaps with clearance-checked direct/bent ties, including plane nets | Merged in PR #25; short-tie and obstacle regressions passed |
| #5 | Enforce a configurable intra-pair skew limit without requiring a user length group | Merged in PR #25; native skew/scope/lock tests passed |
| #6 | Preserve or recover fine-pitch plane-pad via sites through whole-board signal routing | Pending reproduction |
| #7 | Extend locked partial copper from free ends without duplicating the existing route | Merged in PR #25; endpoint/locked-copper regressions and CI passed |
| #8 | Net-group/layer-specific via restrictions; Kelvin joins only at a specified pad; repeat-channel template mapping | Pending design and reproduction |
| #13 | Reproduce isolated long-sense-line failures with full clearance rules; measure search/read time and preserve refill results on hard stop | Pending reproduction |
| #16 | Replan a whole IC pin row's escapes jointly, preserving locked and unrelated copper | Pending reproduction |
| #18 | Try inward, staggered via escapes when fine-pitch pins cannot escape outward | Pending reproduction |
| #19 | Profile custom-rule owner maps and rip-up; reduce claim memory/runtime without changing ownership, clearances or rollback | Compressed claims implemented; synthetic differential/performance tests passed; reporter-board validation pending |
| #21 | Periodic meaningful progress; reuse safe raster work across passes; count board-read cost in budget | Main dc2bb2a implements budget/progress changes; cache and complete acceptance still pending |
| #22 | Bound scoped raster work to the routing region while retaining relevant obstacles, anchors and legal detours | Scoped windows implemented; native suite and private PIO memory comparison passed; CI pending |
| #24 | Route distributed multi-pad rails as a trunk/tree or prioritize them; widen legal plane-pad fanout search around fragmented pours | Power ordering and expanded plane-goal regressions pass; full validation in progress |

Additional issue discovered during the real-board audit: #27, scoped shove
trading a requested connection for a broken existing net. Synthetic rollback
regression passes; reporter-snapshot validation is in progress.

## Evidence

- The first #19 candidate stores wide clearance claims as row intervals and
  narrow/irregular claims as packed integer arrays. Copper rasterization and
  route decisions are unchanged.
- `tests/test_compact_claims.py` verifies owner maps, first-claimer maps and via
  blocking after removals against a full rebuild, with overlapping paths,
  multiple clearance classes, layer-dependent clearances and cut-back rings.
- A matched experiment compares the candidate with the original router module;
  local evidence is kept under `.improve/issues-20261125/`. This does not yet
  establish the performance result on the reporter's full board.

### Claim storage experiment (#19)

On a 36-path, 4-rule-map, 2-layer synthetic case with layer-dependent
clearances and cut-back isolation rings, peak Python allocations fell from
165,179,243 to 51,805,213 bytes (69%). Five uninstrumented repetitions gave
median elapsed times of 2.223 s and 1.735 s (22% lower). Instrumented timing
was slower and is not used as a speed claim. All owner maps, first-claimer
maps and via-block maps matched the original implementation before and after
rip-up. These are synthetic measurements, not the reporter's 44.8 GB board.

The full local KiCad 10.0.8 native/GUI suite passed during this change; the
final bounding-box optimization also passed the focused exact-map tests.

### Locked partial route experiment (#7)

A locked F.Cu stub and via feed an In2.Cu lane ending 10 mm short of the
next pad. Before the fix, shove extraction omitted that endpoint from its
connection clusters. Normal mode exposed it. After the fix both modes close
the connection with 9.9 mm of new track and retain every original locked
item unchanged. `test_locked_route_ends.py`, existing partial-route tests and
scoped-shove tests pass. Shove grouping uses fixed-only connectivity rather
than connectivity through copper that may subsequently move.

### Automatic pair skew (#5)

With differential-pair routing and length tuning enabled, connected pairs
without explicit length groups now receive a default 1 mm skew tolerance.
`NativeOptions.pair_skew_tolerance_mm` configures it; zero disables the implicit
groups. Explicit groups retain precedence, both nets must be in scope, and
skip patterns and short-pair waivers remain effective. Disconnected copper
is not automatically lengthened. Locked tracks remain unchanged and an
unmet target is reported rather than treated as successful.

`test_auto_pair_skew.py` reduces a 20/26 mm pair to 26/26 mm and verifies the
scope, disabled-option, explicit-group, disconnected-copper and locked-track
cases. Native end-to-end and SI tests pass locally.

### Search failure evidence (#2)

Each failed connection now records a `verdict` and the number of search
attempts/expansions. Outcomes distinguish expansion limits, enclosed goals,
exhausted search at the current rules, connection/job/wall time limits,
cancellation, missing exit cells and candidates not retained. A connection
never attempted due to a job limit is identified separately. These verdicts
do not claim that no route could exist with different rules or placement.

`test_search_verdicts.py` verifies capped/exhausted searches, per-connection
timeouts, untried timeouts/cancellation and reset of prior search state. It
also confirms the existing rip-up loop stops after a round with no progress:
allowing 16 rounds performs the same number of attempts as allowing one for
a fixed-wall case. Repeated separate jobs remain controlled by the caller;
the structured verdict and failed set make that decision explicit.

### Reporter-board claim comparison (#19)

A private copy of the reporter's PIO `route_main` snapshot was tested with
three selected nets, 0.15 mm pitch, 143 owner maps and 117 movable paths.
All owner/routed/via-block hashes match before and after removing three paths.
Peak process RSS fell from 2,248,032,256 to 1,952,415,744 bytes (13%).
Read/claim/removal times were 84.22/14.69/1.90 s before and
100.41/15.85/2.53 s after. This single sequential run does **not** demonstrate
a speed improvement; it is also not the original 22-net, 0.1 mm report.
The claim representation reduces retained memory, but dense owner maps and
large-board runtime remain follow-up work. Inputs and raw measurements stay
private under `.improve/issues-20261125/`.

### Mixed-net pour protection (#1)

`NativeOptions.protect_pour_regions=True` reserves each zone's polygon for
its own net on that zone's layers. Higher-priority foreign zones and fill
keepouts are subtracted, polygon holes are preserved, and clearance/track
radius is included in the obstacles. This opt-in restriction also applies
to path simplification through the same owner maps; it does not depend on
one `plane_net` per layer or on stale filled copper.

`test_protected_pours.py` checks the two overlapping nets, a hole exposing
the underlying zone, the disabled mode, and actual emitted tracks with only
In2 routable. The route detours around the foreign corridor.

### Short ties to existing copper (#4)

A 2 mm pad-to-fixed-track gap already closes without new vias. The plane-net
case instead added a longer fanout and a new via despite an existing plane
via 2 mm away. Fanout now first tries a clearance-checked tie of at most
3 mm to fixed vias confirmed on the main filled plane (or already tied
large pads). Movable vias and unfilled/foreign pour outlines are not targets.
The existing fallback remains when no short tie fits.

`test_short_copper_ties.py` verifies the 2 mm track and plane-via ties,
preserves locked copper, and rejects the tie when a foreign track walls it
off. The blocked case uses a new plane via without crossing the wall.
Plane/fanout/island regression tests pass locally.

### Cell8 snapshot and restored-via audit

A copied Cell8 `work7/start` snapshot reproduces incomplete MUX_SD5/SD6/SC6/SC7
connections at U2. A scoped 0.15 mm shove pass joins all three pads of SD5,
SD6 and SC6; SC7 remains open. This snapshot is not assumed to be identical
to the original issue's final repair state. Comparing dual escapes is pending.

The pass also reported a small-pad via violation at JP3.2. Before/after
inspection found the via at the exact same position, diameter and drill,
recreated with a new UUID. The audit now compares geometry multiplicities:
restored copper is exempt, while duplicates and moved originals are checked.
`test_via_audit_restore.py` verifies these distinctions; the saved Cell8
before/after pair produces no new violation with the corrected audit.

Manual native-editor Undo remains unverified and is retained in the overall
verification backlog; no automated test or goal-state change substitutes for it.

### Scoped repair must preserve existing connectivity (#27)

On the copied Cell8 snapshot, default escapes join SD5/SD6/SC6 but split
MUX_SC3; dual escapes join SD5/SD6/SC7 but split MUX_SC0. Neither candidate
is accepted as an improvement. A scoped rip-up now rolls back a one-for-one
exchange when the displaced connection is outside the requested set.
`test_scoped_shove_preserves_connections.py` reproduces the impossible
single-layer crossing and preserves the original path after rollback.
Existing scoped-shove and ownership regressions pass. On the same Cell8
snapshot the protected rerun joins SC6 and SC7, with zero nets regressing
in connected pad-component count. The remaining SD5/SD6 work stays open.
Refilled custom-rule DRC finds no new clearance violations, but dangling
track reports rise from 8 to 15 (18 to 25 reports overall). Six of the seven
newly dangling tracks are original copper. This separate remnant problem
is tracked in #28; the experimental board is not accepted as a final result.

### Local clearance overrides (#26, newly reported)

Custom-rule status now warns when an explicit pad override or inherited
footprint override is below the winning custom rule for another actual board
net/item profile on that pad's copper layer. It names the footprint, source,
local/rule values, rule name, pad count/numbers and layers. This is an intent
warning; geometry and clearance settings are not changed. Later rule
exceptions and pad overrides take precedence; unset is distinct from zero.

`test_local_clearance_warning.py` covers those cases and emitted extraction
status. The private PIO snapshot produces the expected 0.2 mm vs 0.6 mm
warnings for the 14 SO-4 relay footprints, four pads each. KiCad 9's
[documented optional local-clearance API](https://docs.kicad.org/doxygen-python-9.0/classpcbnew_1_1PAD.html)
is used to distinguish an unset override from an explicit zero.


### Fixed copper remnants (#28)

Movable pieces now retain the layer and exact position of fixed track/via
endpoints they originally touched. After a rip-up attempt, those contacts
must still touch the net's remaining routed copper; otherwise the entire
attempt is restored. The requirement survives subsequent rip-up rounds.
Failure details retain `candidate_rejected` so callers can distinguish this
rollback from a search that found no path.

The portable fixed-bridge regression reproduces the original failure: two
pad groups stay connected after rerouting while a fixed intermediate bridge
is abandoned. The native extraction test verifies both bridge contacts;
existing shove and locked-route tests still pass.

On the same private Cell8 snapshot, the guard preserves all net pad
components and the exact 18 baseline refilled DRC reports (matching type,
description and positions, excluding UUIDs). Without the guard there were
seven new dangling tracks. The guarded run adds no new connections in this
120-second configuration: it is a regression-prevention fix, not evidence
that the remaining difficult row routes are solved. Those issues remain open.

PR #25 is merged into main at d5dd4fb; issues #1, #2, #4, #5, #7, #26 and #27
are closed. The root checkout is main; continuing experiments use an isolated
worktree. Manual editor Undo remains unverified.


### Scoped raster windows (#22)

Selected-net/reference/sheet jobs now rasterize the scoped nets' pads, their
footprint pin rings and all existing copper of those nets, plus
`shove_margin_mm`. Grid origins remain aligned with the whole-board grid.
All board obstacles are considered, but their cells are clipped to the
window. Foreign tracks crossing its boundary stay fixed, preventing removal
of copper that cannot be represented in the local grid. Fixed copper entering
the window still supplies visible connection anchors.

`scoped_grid=False` or `shove_margin_mm=0` restores full-board rasterization.
A longer detour can require that mode or a larger margin: exhausted/enclosed
window searches explicitly report `no_path_in_scope_window` or
`enclosed_in_scope_window`, and include the window bounds. There is no silent
full-board fine-grid allocation after a local failure.

The native regression compares complete ownership samples to the full grid,
checks boundary-crossing copper, selected locked-lane extent, reference
selection and a detour that succeeds only in full-grid mode. The local full
KiCad suite passes. Existing movement-radius/pair-lock tests explicitly use
the full grid to test those controls independently of the window boundary.

On a private PIO copy, a single-net scope at 0.15 mm with 143 owner maps used
180,456 rather than 3,974,724 cells. Peak RSS fell from 2,415,132,672 to
504,741,888 bytes (79%). Read plus claim preparation took 71.97 s versus
43.03 s in one sequential comparison; claim preparation alone was slower.
Movable paths changed from 61 to 56 because boundary-crossing pieces stay
fixed. This is not the original 22-net report and does not establish a
universal speedup. At 0.075 mm the same single-net local job used 715,040
cells and peaked at 1,196,834,816 bytes (1.20 GB); read/claim preparation was
16.14/12.32 s. Timing varies with concurrent machine load; the memory result
is the main evidence. No full-board 0.075 mm allocation was needed.

### Distributed power and remote plane goals (#24)

`power_trunks_first=True` gives routed power nets with four or more disconnected
clusters priority over single-ended signals, retaining coupled-pair priority.
A five-pad native fixture verifies the emitted route order and complete rail
and signal connectivity with the option both enabled and disabled.

A second fixture places a plane pad over a higher-priority foreign zone whose
edge is 4 mm away. Previously the pad was omitted from fanout. Ordinary fanout
now tries outer-ring goals up to `plane_fanout_reach_mm` (default 6 mm) when
local goals are absent or unreachable. Outer candidates use approximately
0.3 mm spacing to bound storage rather than quadrupling every fine-grid set. Goals subtract foreign priority regions and keepouts;
both pad exits must satisfy the same plane-landing restriction. The fixture
finishes with zero unconnected items after refill and no new via in the foreign
zone. This does not yet establish the result on every reporter-board rail.

The outer-ring regression also retains valid local goals while blocking every
nearby via site; local-only search fails and the outer fallback completes the
connection after refill. An initial private PIO +3V3 comparison, before this
second fallback, gave 15 to 14 pad components for both baseline and candidate;
that experiment did not demonstrate an improvement on the reporter snapshot.
