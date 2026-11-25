# Reliability review — 2026-11-25

Reviewed routing/geometry, AI editing and acceptance, subprocesses, editor project
ownership, settings and job storage, SI/fabrication rules, guides, packaging,
tests and documentation against baseline `ab45284` (0.5.13).

## Changes

| Priority | Finding | Implemented change | Regression coverage |
| --- | --- | --- | --- |
| High | AI actions could modify locked copper or footprints | Reject affected actions before mutation, including remove-existing routing | `test_ai_locked.py` |
| High | A failed Claude CLI exit could still supply valid-looking actions | Reject failed exits; reap children on cancellation, timeout and callback failure; retain logs | `test_ai_claude_process.py` |
| High | Loading a reference board could replace the editor's active project | Learn guides in a separate KiCad process with the reference's own net classes | `test_guide_project_isolation.py` |
| High | Retention could delete a job another editor was still using | Cross-process OS leases protect active jobs; crashed processes release leases | `test_job_retention.py` |
| High | PCM publishing could overwrite a version or damage a catalogue | Validate destination, branch, catalogue and immutable version hashes before writing | `test_publish_guards.py` |
| Medium | Total open count could conceal a newly broken net; model score omitted SI regressions | Require complete per-net connectivity and deterministic length, width and pair-coupling checks | `test_ai_optimize.py`, `test_ai_quality.py` |
| Medium | API cancellation left its request thread running | Run SDK requests in a bounded, killable process; disable SDK retries | `test_ai_api_process.py` |
| Medium | Invalid settings and interrupted sidecar writes could break subsequent runs | Validate values and replace settings/SI/fab/guide files atomically | `test_runtime_reliability.py`, `test_config_validation.py` |
| Medium | Same-second jobs collided; mistyped test filters passed without tests | Allocate unique directories; reject empty test selections | `test_runtime_reliability.py` |
| Medium | Cross-platform behavior had no automated coverage | Portable OS/Python matrix, Linux KiCad 9/10 GUI/native tests and Windows KiCad integration | `.github/workflows/tests.yml` |
| Medium | KiCad 9 API differences broke connectivity, copying, DRC and zone filling | Adapt connected-item, outline, item lookup, settings and DRC APIs; initialize private refill projects and DRC engines | Native routing, SI, board-copy and filled-island tests on both KiCad versions |
| Medium | Via edit rollback flattened different layer diameters | Preserve and compare each copper layer diameter | `test_ai_locked.py` |
| Medium | Search deadlines used an adjustable wall clock with platform-dependent precision | Use a monotonic high-resolution clock while retaining deterministic benchmark work budgets | `test_native_grid.py` |

## Matched board comparison

Baseline `ab45284` and the initial reliability changes were run with identical
settings: a one-minute deterministic search budget (85,000 expansions/second),
a five-minute wall cap and no image generation. OBD2 used the public fixture;
CM5 used a local, partially routed board with GND/GND1/GND2 excluded.

| Board | Open connections, before → after | New physical/rule errors, before → after | Length | Vias |
| --- | --- | --- | --- | --- |
| OBD2 | 0 → 0 | 0 → 0 | 77.4 mm, unchanged | 0, unchanged |
| CM5 | 140 → 140 | 8 → 8 (`hole_clearance`) | 1,920.9 mm, unchanged | 197, unchanged |

These results establish non-regression for these inputs and budgets. CM5 remains
incomplete and is not DRC-clean. The benchmark excludes pre-existing DRC findings;
its error columns are not total board DRC counts. Runtime varied (OBD2 0.5/0.7 s;
CM5 99.8/116.2 s) while other local checks were running, so no speed improvement
is claimed. `scripts/compare-benchmarks.py` rejects regressions separately for
each board and previously connected net, including changes in DRC error types.

## Verification boundaries

- The full macOS native and opt-in GUI suite passed after the compatibility
  adapters. The installed KiCad version reports 10.0.8.
- All six portable OS/Python combinations passed in GitHub Actions. Linux
  KiCad 10 and Windows KiCad 10 board/dialog flows passed. KiCad 9 compatibility
  fixes remain under CI verification on the review branch.
- API tests use fake SDK transports in real child processes. No paid AI request
  or remote-service cancellation guarantee is implied.
- Differential-pair coupling is a geometric estimate sampled at 0.1 mm; it is
  not electromagnetic simulation. Existing SI violations may remain if they
  do not worsen.
- Board-copy and dialog tests validate transfer, placement protection, groups
  and tuning patterns. Native editor Undo still requires manual observation;
  the computer-control tool could not initialize in this environment.
- No release or PCM catalogue was published by this review.
