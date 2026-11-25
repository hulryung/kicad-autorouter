# Open issue resolution

Objective: verify and resolve every issue open on 2026-11-25. An issue is closed
only after all its requested behaviors are implemented or disproved with
reproducible evidence, relevant tests pass, and the change reaches main.
Routing experiments use copies; private board inputs are not published.

| Issue | Acceptance requirements | State |
| --- | --- | --- |
| #1 | Other-net pour polygons incur a routing cost or block, including mixed-net layers; keep own-net pours usable | Pending reproduction |
| #2 | Distinguish blocked exits, exhausted search and time budget in failure details; stop unchanged escalation when appropriate | Pending reproduction |
| #3 | Connect multi-pin fine-pitch supply nets with legal pin escapes and alternate-layer joins or a ring | Pending reproduction |
| #4 | Close short pad-to-track/via/pad gaps with clearance-checked direct/bent ties, including plane nets | Pending reproduction |
| #5 | Enforce a configurable intra-pair skew limit without requiring a user length group | Automatic 1 mm matching implemented; native skew/scope/lock tests passed; CI pending |
| #6 | Preserve or recover fine-pitch plane-pad via sites through whole-board signal routing | Pending reproduction |
| #7 | Extend locked partial copper from free ends without duplicating the existing route | Reproduced missing inner-layer endpoint in shove mode; fixed and native regression passed; cross-platform verification pending |
| #8 | Net-group/layer-specific via restrictions; Kelvin joins only at a specified pad; repeat-channel template mapping | Pending design and reproduction |
| #13 | Reproduce isolated long-sense-line failures with full clearance rules; measure search/read time and preserve refill results on hard stop | Pending reproduction |
| #16 | Replan a whole IC pin row's escapes jointly, preserving locked and unrelated copper | Pending reproduction |
| #18 | Try inward, staggered via escapes when fine-pitch pins cannot escape outward | Pending reproduction |
| #19 | Profile custom-rule owner maps and rip-up; reduce claim memory/runtime without changing ownership, clearances or rollback | Compressed claims implemented; synthetic differential/performance tests passed; reporter-board validation pending |
| #21 | Periodic meaningful progress; reuse safe raster work across passes; count board-read cost in budget | Main dc2bb2a implements budget/progress changes; cache and complete acceptance still pending |
| #22 | Bound scoped raster work to the routing region while retaining relevant obstacles, anchors and legal detours | Pending implementation |
| #24 | Route distributed multi-pad rails as a trunk/tree or prioritize them; widen legal plane-pad fanout search around fragmented pours | Pending reproduction |

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
