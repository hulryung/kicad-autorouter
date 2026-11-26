# Open issue resolution

Objective: verify and resolve every issue open on 2026-11-25 and the follow-up
reports from the same review. An issue is closed
only after all its requested behaviors are implemented or disproved with
reproducible evidence, relevant tests pass, and the change reaches main.
Routing experiments use copies; private board inputs are not published.

| Issue | Acceptance requirements | State |
| --- | --- | --- |
| #1 | Other-net pour polygons incur a routing cost or block, including mixed-net layers; keep own-net pours usable | Merged in PR #25; native tests and all nine CI jobs passed |
| #2 | Distinguish blocked exits, exhausted search and time budget in failure details; review optional unchanged-pass stopping | Merged in PR #25; verdicts and no-progress stopping verified |
| #3 | Connect multi-pin fine-pitch supply nets with legal pin escapes and alternate-layer joins or a ring | Merged in PR #33; copied W5500 supply fully joined with unchanged DRC and other-net connectivity |
| #4 | Close short pad-to-track/via/pad gaps with clearance-checked direct/bent ties, including plane nets | Merged in PR #25; short-tie and obstacle regressions passed |
| #5 | Enforce a configurable intra-pair skew limit without requiring a user length group | Merged in PR #25; native skew/scope/lock tests passed |
| #6 | Preserve or recover fine-pitch plane-pad via sites through whole-board signal routing | Merged in PR #32; native/GUI suite and all nine CI jobs passed |
| #7 | Extend locked partial copper from free ends without duplicating the existing route | Merged in PR #25; endpoint/locked-copper regressions and CI passed |
| #8 | Net-group/layer-specific via restrictions; Kelvin joins only at a specified pad; repeat-channel template mapping | Merged in PRs #35, #41 and #42; native/private-board checks and all nine CI jobs passed |
| #13 | Reproduce isolated long-sense-line failures with full clearance rules; measure search/read time and preserve refill results on hard stop | PR #51; recovered historical input completes all nine sense nets with no new DRC identities |
| #16 | Replan a whole IC pin row's escapes jointly, preserving locked and unrelated copper | Merged in PR #48; reusable API/CLI, native regressions and private Cell8 joint reroute passed |
| #18 | Try inward, staggered via escapes when fine-pitch pins cannot escape outward | Merged in PR #48; inward staggered goals, QFP/rotation and outward-wall regressions passed |
| #19 | Profile custom-rule owner maps and rip-up; reduce claim memory/runtime without changing ownership, clearances or rollback | Merged in PR #34; reporter-board hashes match and five-run rip-up timing improved |
| #21 | Periodic meaningful progress; reuse safe raster work across passes; count board-read cost in budget | Merged in PR #36; bounded raster reuse, complete-job statistics and all nine CI jobs pass |
| #22 | Bound scoped raster work to the routing region while retaining relevant obstacles, anchors and legal detours | Merged in PR #30; native suite, private PIO memory comparison and all nine CI jobs passed |
| #24 | Route distributed multi-pad rails as a trunk/tree or prioritize them; widen legal plane-pad fanout search around fragmented pours | Merged in PR #31; both requested modes verified; private PIO improves four joins but still has physical opens |

Additional issue #27 (scoped shove breaking an existing net) was resolved in
PR #25 with protected-copper and rollback regressions. New reports from the
continuing audit are tracked below.

| Issue | Resolution |
| --- | --- |
| #45 | PR #49: suspend-aware job caps; controlled-clock and native hard-stop tests |
| #46 | PR #52: explicit local endpoint reachability; joint row replanning in PR #48 |
| #47 | PR #50: same-path via spacing retries and new-via hole audit |

The native editor Undo check remains unverified: the computer-control runtime
fails before attaching to KiCad. Headless/native test results do not substitute
for that manual acceptance check. No private source board was modified.

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

The guard also retained redundant contacts at fixed T junctions or pads (#39).
A native regression fails on the previous extraction and passes when endpoints
already supported by same-layer fixed copper or pads are excluded. Copper on
another layer is not a support, and the original unsupported-bridge rollback
regression still passes. This refinement adds no connections on the private
Cell8 snapshot; #16/#18 remain open. A separate prototype adding contact points
as routing targets also added no connections and was rejected.

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

### Multi-pin supply escape geometry (#3)

The neck width was already narrower than the supply trunk, but its escape
position and first owner-map check still used the trunk width. On a crowded
pin that could put both candidate exits into neighboring copper, even though
a shorter narrow exit was legal. Exit offsets and checks now use the actual
neck width; exact stub clearance checks and wide trunk routing remain active.

On a copied Base-board W5500 supply, `/USB_ETH/+3V3A_ETH` had four pad components.
The old default joined them into two; enabling both pad exits or a prototype
via-first pass still left two. Setting the entire net to 0.25 mm joined all
four, which identified the width-dependent failure. The corrected neck exits
join all four with the original net width. Every other net retains its pad
connectivity, and refilled DRC has exactly the same 14 violations (10 silk over
copper, 4 silk overlaps), with no new or removed violation reports. Unconnected
items fall from seven to four, all three gains on the selected supply.

`test_supply_pin_necks.py` exercises a synthetic 48-pin, 0.5 mm-pitch IC with six
supply pins separated by ground pins, verifying complete supply connectivity,
narrow emitted necks, wide trunks and adjacent-pad clearance. The decisive
before/after crowded-board regression is retained privately.

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
connection after refill. Private PIO +3V3 comparisons before and after this
second fallback gave 15 to 14 pad components for both baseline and candidate;
that experiment did not demonstrate an improvement on the reporter snapshot.

### Retaining plane-pad outlets (#6)

The early pour pass previously put all new plane vias/stubs in the movable job
list. A later signal pass could remove them and the final pour pass had no
guarantee of finding another site. `preserve_plane_fanout=True` now reserves
that copper with per-job UUID locks across the signal passes. Opting out
retains the prior rip-up/restore behavior. Native editor lock flags are not
changed.

The plane-job extraction branch also ignored existing editor/UUID locks when
an item appeared in the job's movable list. The regression reproduced this
before the fix. It now respects both forms of lock, including in legacy mode.
`test_plane_fanout_reservation.py` runs a fine-pitch MSOP board through early
fanout and actual signal routing, checks the extracted movable set and the
original fanout UUID/geometry, and verifies final connectivity. Both reservation
and legacy modes pass. This prevents a reserved site from being taken; it does
not create an outlet for a pad that could not fan out in the early pass.

### Safe raster reuse across passes (#21)

Pipeline passes share one bounded raw-geometry cache (128 MB maximum by default,
`raster_cache_mb=0` disables it). It retains compact immutable cells for board
outlines and pad/arc shapes. Keys include the grid, layer, complete geometry and
raster radius. Ownership, connectivity, custom-rule selection and isolation-ring
cuts are never cached. Empty results and cheap straight-track/box rasters are
not retained, avoiding eviction by work that is already cheap. KiCad formats
some rotated polygons only as `shape 4`; those incomplete representations are
explicitly excluded from caching.

Warm and uncached extraction agree after moving/rotating/resizing pads, changing
round-rectangle radii, assigning another net, changing clearance, adding/moving/
removing tracks, changing pitch, and adding a board-outline hole. A real pipeline
fixture verifies that separate passes share the cache without modifying the
caller's options. Existing progress, read-inclusive deadline and watchdog tests
pass. The full native/GUI suite passed, followed by the final cache/deadline
regressions after the bounded-admission adjustment.

On the private three-net PIO snapshot, a warm read reused 3,785 raw rasters while
retaining 12,619,904 estimated bytes. All owner/via-block hashes matched cold and
disabled-cache reads. Cold/warm/disabled elapsed times were 12.98/12.85/13.86 s in
one sequential comparison; this does not establish a general whole-job speedup.
A broad first cache candidate thrashed at its limit and was rejected. Raw-raster
reuse reduces repeated geometry work, while map writes and rule/connectivity
work still run on each pass.

A full-board-grid extraction of the same private PIO scope reused all 18,488
cacheable shapes on the second read, retaining about 67.1 MB. Cold, warm and
disabled reads took 49.58, 46.39 and 49.26 seconds respectively; all owner and
via-map hashes matched. This single comparison shows reuse and correctness,
not a general routing speed guarantee.

### Per-job routing restrictions (#8, partial)

`via_keepouts` supports net patterns or routing groups, named copper layers,
rectangles and additional clearance margins in nm. Through-via barrels are
excluded if they cross a restricted layer. Masks share storage for nets with
the same constraint and diameter and are combined with the live drill-obstacle
map without copying it, so rip-up and newly placed vias remain visible.
`layer_keepouts` blocks new copper only on selected layers. No temporary board
objects are created, and existing copper is retained.

Native tests route an unrestricted net across a layer barrier while a restricted
net correctly remains open, verify group selection and layer isolation, and
reject invalid layer names. Fanout and stitching use the same restrictions.
A portable coupled-pair case confirms that a member-specific mask also applies
to paired and single-ended fallback vias. Kelvin pad-only joins and channel
replication are still pending, so #8 is not closed by this change.

### Rip-up interval index (#19)

On the current scoped PIO model (143 owner maps, 117 movable paths), profiling
three removals showed 9.31 million binary searches inside claim intersections.
The compact claims saved memory but still scanned every raw segment row for
each surviving path/map pair. A lazy disjoint interval index now limits each
intersection to the touched range and searches the smaller side. Raw row spans
remain intact for ownership slices and geometric bounds; adding cells invalidates
the index.

The profiled removal fell from 1.512 s to 0.314 s, including index construction.
Owner-map, first-claimer and via-block hashes match before and after removal.
Peak RSS was 1.230 GB and 1.224 GB in these runs. These profile timings are not
an uninstrumented whole-job speed claim. `test_claim_intersections.py` compares
100 randomized overlapping claims against explicit sets, including sparse,
dense and empty intersections and mutation after indexing; exact rip-up versus
full-rebuild tests also pass.

Five alternating uninstrumented runs rebuilt the same 117-path router before
each removal (a cold index each time). Median removal time fell from 1.1913 s
to 0.2584 s (78% lower). All ten resulting ownership/first-claimer/via-block
hashes match. The full local native/GUI suite passed. This is a measured
three-net scoped case, not a timing claim for the original full 22-net job.

### UI error propagation (#37)

A native wx worker/main-thread regression reproduced `_on_main` swallowing a
board-read exception and returning `None`. The bridge now re-raises the original
exception on the waiting routing worker, so watchdog hard stops reach the
pipeline's overrun handler. A `finally` block releases the worker even when
error reporting fails. Successful results, including `None`, remain unchanged.
The regression exercises real wx dispatch, thread identity and both ordinary
errors and watchdog exceptions. This fixes reporting; the long-sense routing
case and manual native-editor Undo remain separate open verification work.

The UI regression also runs a routing worker through actual main-thread
extraction and zone filling, injecting an Overrun at the next extraction stage
after the final pour refill. The job reports `the pour pass` in its overrun
list and retains the exact filled polygon representation. This verifies the
completed-refill boundary; it does not interrupt KiCad's C++ fill mid-call.


### Kelvin pad-only joins (#8)

The headless Kelvin helper treats the source pad's existing connected component
as the sense side and routes it to a single designated pad on the same net.
Original force copper outside that pad is blocked with a separate spacing mask.
An independent polygon/shape check validates emitted copper and separation on
every layer, followed by per-net connectivity and KiCad DRC acceptance. Existing
copper, pad assignments and the input project are preserved; only a validated
new output project is published. This is a routing operation, not a permanent
KiCad net-class rule. Zone-bearing nets and per-layer widths are explicitly
unsupported; already connected sense/force inputs are rejected.

Native regressions cover the nearer same-net shortcut, existing partial sense
tracks, through-hole join pads, multilayer detours, blocked single-layer routes,
independent rejection of a deliberately faulty router result, spacing checks,
CLI invocation and unchanged input bytes.

On the private Cell8 copy, U506.1's C4 component joins J5.5 with six new items,
then U306.1's C2 component joins J5.3 with three. Both actual outputs pass the
independent 0.15 mm force-separation check. Per-net DRC open counts decrease
only on the requested nets, and existing copper is retained. These are the two
remaining sense connections in this snapshot, not the original nine-connection
input from #13. That issue's exact historical reproduction remains separate.

### Full power-job experiment (#24)

A 0.15 mm PIO +3V3 job with early/final fanout, stitching enabled and power-first
ordering reduces pad components from 15 to 11, adding six fanouts. All other
nets retain their pad-component counts. Refilled DRC has the exact same 411
violation reports; unconnected items decrease from 119 to 115. The run takes
285.8 seconds with 2.48 GB peak RSS, including a hard stop during the island
check. Settings differ from the earlier 0.2 mm single-pass experiment, so this
is not an isolated algorithm speed/completion comparison. The remaining ten
separated +3V3 groups are a limitation of this board result. A wider 12 mm
probe makes no additional joins. Both requested feature behaviors in #24
(power-first ordering and wider legal fanout search) are implemented and
verified; this is not a claim of fully routing the private PIO board.

### Short-budget timeout reporting (#43)

A 90 mm synthetic Kelvin connection with 1.5 mm domain clearance and
0.15 mm same-net force separation completed in about two seconds, yet was
reported as timed out with a 30 second budget. The optional refinement
threshold (60 seconds remaining) was incorrectly treated as an expired
job deadline, even with refinement disabled or no open connections.

The pipeline now distinguishes skipped refinement from an expired deadline.
Native tests cover successful short-budget routing, blocked connections with
refinement enabled/disabled, and actual expired budgets. Existing watchdog
hard-cap tests remain; the 100 ms clock check now uses a controlled clock to
avoid failure when a busy host deschedules the test before its first check.

### Joint row planning (#16 / #18)

On a copy of the Cell8 board after the two checked Kelvin joins, local
replanning restores 17 connections around U2 and creates inward staggered
escape vias. The remaining MUX routes are then completed globally. Existing
In1 connections require keeping all four copper layers available during
this experiment. A crossing Q11 gate route is included among the local
blockers to restore.

After removing one newly generated dangling SD5 tail, all four MUX nets
improve from two pad components to one, and no other net regresses. Full
refilled DRC retains the exact 18 baseline reports; airwires fall from five
to one. The input board is unchanged. PR #48 promotes the prototype to the public
`replan_row` API and `scripts/replan-row.py`, with native boundary/lock,
QFP/rotation and CLI publication tests. See `docs/row-replanning.md`.

### Consecutive new via holes (#47)

The existing-hole mask did not cover a second via created within the same A*
path. A two-wall regression routed overlapping holes 0.2 mm apart where
0.65 mm was required. Searches now check their own proposed via pairs and
retry with a private exclusion mask, sharing the original expansion budget
and cancellation callback. The regression still connects, with legal spacing.
A geometry-counted post-job audit reports new same-net or different-net via
hole violations without attributing pre-existing/restored pairs to this job.

The archived PIO intermediate confirms both reported vias were introduced in
one pass and joined by a short In2 path. A narrower +12V-only replay does not
reproduce the pair, so it is not claimed as a matched before/after fix of the
original 87-net run. Native existing-hole and new-pair audit regressions cover
the fabrication invariant independently of that private pass's ordering.

### Suspend-aware job cap (#45)

Routing and hard-stop deadlines now use the watchdog's epoch-based awake clock,
using the same wall/monotonic difference as its existing macOS sleep diagnostic.
System suspend therefore does not consume the job cap; awake CPU starvation and
slow work still do. Wall-clock progress/elapsed reporting remains available.

Controlled-clock tests advance wall time by 1000 seconds while awake time moves
by one second: the pipeline completes its route without a false wall cap. A
subsequent five awake seconds expires the cap. Native hard-stop tests still
interrupt overdue routing, and the full local native/GUI suite passes.

### Endpoint reachability diagnostics (#46)

Failure details now include a 2 mm planar flood at each reported endpoint
(capped at 64 cells of radius on very fine grids),
with the applied width/neck/clearance, probe radius, legal seed count and
reachable count. It distinguishes an enclosed endpoint from free pockets
nearby, a blocked seed, a disabled layer, and an exit reaching the probe boundary. This is
explicitly a local planar diagnostic: it does not claim a complete route or
infer via feasibility. The existing whole-search verdict remains separate.

The reporter corrected the original claim that all fixed-neighbour paths
exist: their independent search closes only two, and most other ends are
walled by adjacent escapes. Joint escape replanning is supplied by PR #48;
the smaller-via explanations in the original report remain unverified guesses.

### Original nine-net sense reproduction (#13)

The historical phase-F checkpoint and the original force/sense split were
recovered locally. Replaying the split moves 405 force items, matching the
recorded run. At 0.1 mm pitch and a 180 second search budget, the pre-fix
router adds 17 partial connections but leaves every C0–C8 net open.

Declared net-tie copper bridges were rasterized as netless obstacles across
their own pads' exits. Extraction now retains only previously pad-owned cells
within the attachment region of pads in a declared, touched net-tie group.
Undeclared graphics, foreign blockers and unrelated-net crossings remain
obstacles. Native tests cover these distinctions and compare DRC results.

With this fix the same reconstructed input completes all 40 required joins:
all nine C0–C8 nets become single pad components in 188.7 seconds including
board reading and pour checks. No other net's component count increases.
Full refilled DRC introduces zero new violation identities (143 → 141 reports),
and unconnected items fall from 81 to 41; the remaining opens are outside the
requested sense nets. A separate native nine-line Kelvin test covers lengths
30–90 mm, 1.5 mm domain clearance and a 0.15 mm force separation requirement.

The copied PIO replay selects 21 open non-plane signal nets. Fixed-neighbour
routing adds no connections; the exact 171 refilled DRC reports and all 43
unconnected items remain, with no split of an existing pad group. A 0.5 mm
probe was too short to distinguish these local pockets, so the diagnostic
radius was widened. Its boundary verdict still does not prove a full route.
