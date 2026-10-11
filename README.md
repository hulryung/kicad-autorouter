# KiCad Autorouter

Autoroute the open board without leaving KiCad's PCB editor. Two engines:

- **Built-in router** (default). Reads pads, existing copper, keepouts, the
  board outline and the net-class rules through KiCad's PCB API, routes on a
  multi-layer grid with A* (vias, preferred directions per layer, clearance and
  track width per net class) and puts each connection's tracks and vias on
  the board **as soon as it is found**, so you watch the board fill in. No
  files, no external programs, one undo step.
- **Freerouting**. The classic Java autorouter, driven headless: Specctra DSN
  out, session back in, with pass-by-pass progress in the dialog. The jar and,
  if needed, a Java runtime are downloaded into the plugin's data folder on
  first use.

Either way the dialog shows live progress, can be cancelled, and Ctrl+Z puts
the board back exactly as it was.

## Requirements

- KiCad 9.0 or 10.0 (the SWIG action-plugin API). KiCad 11 drops that API; an
  IPC-API build is on the roadmap, see below.
- The built-in router needs nothing else.
- Freerouting needs internet access on first use (about 120 MB: engine 62 MB,
  JRE 55 MB), or a Java 25+ runtime already on the machine and a copy of
  `freerouting-2.5.0.jar`.
- macOS (Apple Silicon and Intel), Windows x64, Linux x64/arm64.

## Installation

### From a zip, with the Plugin and Content Manager

1. Download `kicad-autorouter-<version>.zip` from the releases page.
2. In KiCad: **Plugin and Content Manager → Install from File…**, pick the zip,
   **Apply Pending Changes**.
3. The *Autorouter* button appears in the PCB editor's toolbar
   (**Tools → External Plugins** lists it too).

### For development

```bash
git clone https://github.com/hulryung/kicad-autorouter
cd kicad-autorouter
./scripts/install-dev.sh      # symlinks plugins/autorouter into KiCad 9/10's scripting/plugins
```

Then **Tools → External Plugins → Refresh Plugins** in the PCB editor.

## Using it

Open a board, click **Autorouter**, pick an engine, click **Route**.

| Tab | What you set |
|-----|--------------|
| **Routing** | The engine. For the built-in router: routing grid pitch, via cost, search budget per connection, retry pass, nets to leave unrouted (name patterns such as `GND,*_PLANE`, for nets that get a copper pour), **track width and via size** (per-net net class sizes, the editor's current track/via choice, or any of the board's pre-defined sizes from Board Setup), and **what to route**: the whole board, only the items selected in the editor, or only the footprints of chosen schematic sheets. Whether to remove the existing tracks first; by default existing routing is kept and only unrouted connections are routed. |
| **Layers & nets** | Which copper layers may be routed, an optional preferred-direction override per layer, and net classes to leave unrouted. Both engines honour these. |
| **Freerouting** | Passes, optimizer threads and threshold, via / plane-via / rip-up costs, strategies, JVM heap, extra arguments, SMD fanout, and where Java and the jar come from (leave empty for auto-detection and the managed download). |

Options are remembered between runs.

### KiCad's routing settings

The built-in router takes its sizes from the board the way KiCad's own tools
do:

- **Net classes**: track width, clearance, via diameter and drill come from
  each net's effective net class (assignments and patterns from Board Setup
  are resolved by KiCad). Board minimums (track width, via size, clearance,
  hole clearance, hole-to-hole) are enforced on top.
- **Per-item clearances**: pads and tracks with local clearance overrides are
  kept at that distance.
- **Pre-defined sizes and the editor's selection**: the *Track width* and
  *Via size* choices in the dialog list the board's pre-defined sizes and the
  editor's current toolbar choice, so a run can use "0.2 mm everywhere" or
  "what I have selected right now" instead of the net classes.

Not read yet: custom DRC rules (`.kicad_dru`) that set track width or via size
per condition (their clearances are honoured through the per-item clearance),
and differential-pair widths/gaps.

### Routing part of a board

*What to route* on the Routing tab:

- **Selected items**: select footprints, pads, tracks or vias in the editor
  before opening the dialog. Connections that touch a selected footprint, and
  all connections of a selected track's or via's net, are routed; everything
  else stays as it is.
- **Schematic sheets**: tick one or more sheets (the list comes from the
  footprints' sheet names). Connections touching a footprint from those
  sheets are routed.

Only the chosen connections are searched; the rest of the board is still an
obstacle, and existing tracks of other nets are respected.

### How the built-in router works

1. **Read** (PCB API, a few seconds): every pad, track, via, keepout and copper
   graphic is rasterized onto one occupancy grid per distinct (track width,
   clearance) pair, already inflated by the clearance plus half the track
   width, so the search only asks "may this net's centreline pass here?".
   Pads of the same net that are already joined by copper form clusters; the
   connections to route are a minimum spanning tree over each net's clusters,
   much like KiCad's ratsnest.
2. **Route** (worker thread): connections are routed shortest first with a
   multi-source, multi-layer A* (8 directions, no corner cutting, preferred
   direction per layer, via cost, turn penalty). A routed connection becomes
   an obstacle for the ones after it. A connection that cannot be routed gets
   a **rip-up and reroute** pass: a second search may cross paths this run
   created (never pads, existing copper or keepouts) at a penalty; the paths
   it crosses are removed from the board, the connection is routed, and the
   removed ones are rerouted. Whatever is still open gets a last try with a
   larger budget and cheaper vias.
   - **Pad escapes**: SMD pads are left along the pad's long axis, from the
     pad centre to an exit point just past the pad end, the way a hand-routed
     escape does; the grid search starts and ends at that exit. Each escape
     lane is checked with KiCad's own shape collision tests against the
     copper around it, and lanes are kept clear of each other.
   - **Fine pitch**: connections that end on fine-pitch pins (neighbours
     closer than width + 2 x clearance) are routed first, while there is
     room, and at the board's narrowest pre-defined track width for their
     whole length, as designers do; everything else keeps its net-class width.
   - **Straightening**: zig-zag runs the grid search leaves behind become one
     straight and one diagonal piece when the cells are free.
   - Obstacles carry a 10 µm safety margin so diagonal cell steps never land
     inside the clearance.
3. **Apply** (PCB API, main thread): each path becomes `PCB_TRACK` segments and
   through `PCB_VIA`s with the net class's sizes, added to the open board at
   once; ripped-up paths are removed again. Copper zones are refilled at the
   end (optional) so pours clear the new copper. KiCad records the whole run as
   one undo step.

Limits: through vias only, filled zones are not obstacles (they are refilled
around the new tracks, so pour nets are best left to the pour: list them under
*Nets to leave unrouted*), and the grid is uniform, so very fine-pitch parts
want a smaller pitch (slower).

Benchmark: a finished 4-layer board (71 footprints, 75 nets, DRC clean) with
all tracks removed and only the GND pours left to the zones. The built-in
router reconnects 102 of 104 connections with 0 DRC violations. Against the
original artwork it uses fewer vias (31 vs 46) but about 28% more track length
and many more short segments, so the result is correct rather than pretty;
`tests/test_native_e2e.py` prints these figures (`artwork:` line) for any
board, which is how the router is tuned.

### Data folder

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/kicad-autorouter` |
| Windows | `%LOCALAPPDATA%\kicad-autorouter` |
| Linux | `$XDG_DATA_HOME/kicad-autorouter` (default `~/.local/share/kicad-autorouter`) |

It holds `freerouting/` (the jar and Freerouting's own `freerouting.json`),
`jre/` (a managed Java runtime, if one was downloaded), `jobs/`, `logs/` and
`settings.json`. Set `KICAD_AUTOROUTER_HOME` to move it. Deleting it removes
everything the plugin ever downloaded.

## How it is built

```
plugins/autorouter/
  __init__.py        registers the action plugin (skipped outside the PCB editor)
  plugin.py          pcbnew.ActionPlugin: the toolbar button
  dialog.py          wx dialog: engine choice, options, progress, log; drives both engines
  settings.py        settings.json in the data folder
  engine/
    native/          the built-in router
      extract.py     board -> RoutingProblem through pcbnew (pads, copper, keepouts, outline, rules)
      grid.py        multi-layer owner grid and rasterizers (discs, thick segments, polygons)
      astar.py       one connection: multi-source, multi-layer A*
      router.py      all connections: ordering, retries, obstacle updates, progress
      apply.py       RoutedPath -> PCB_TRACK / PCB_VIA on the board
      problem.py     the data model shared by the above
    freerouting.py   Freerouting release pin, jar lookup/download, options -> command line
    java.py          find a Java >= 25 or install a Temurin JRE into the data folder
    runner.py        the engine subprocess on a worker thread, cancel support
    progress.py      Freerouting log lines -> stage / pass / unrouted / violations
    session.py       the pcbnew side of Freerouting: DSN export, SES import, job folders
    download.py      download with progress, sha256 check, safe archive extraction
    paths.py         data folder and platform names
```

Only `extract.py`, `apply.py`, `session.py`, `plugin.py` and `dialog.py` import
pcbnew or wx; everything else runs and is tested with a plain Python.

The Freerouting release is pinned in `engine/freerouting.py` (`RELEASE`: version,
jar sha256, required Java). Freerouting 2.5.0 also ships native CLI binaries;
they are not used because 2.5.0's native build crashes at the first routing
pass (a GraalVM serialization error) while the jar works.

## Tests

```bash
python3 scripts/run-tests.py        # offline: router core on synthetic grids, command line, progress parser, settings
# with KiCad's Python the end-to-end tests run too:
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 scripts/run-tests.py
# the built-in router on any board, with KiCad's DRC on the result:
<KiCad python3> tests/test_native_e2e.py path/to/board.kicad_pcb --skip GND --out /tmp/routed
```

`test_native_e2e.py` reads a board through pcbnew, routes it with the built-in
router, saves a copy and runs `kicad-cli pcb drc` on it; `test_e2e_headless.py`
does the same round trip through Freerouting (needs Java 25+ and the jar;
`KICAD_AUTOROUTER_E2E_DOWNLOAD=1` lets it download the jar). Both use
`tests/fixtures/obd2-adapter.kicad_pcb` by default.

Packaging: `python3 scripts/build-package.py` writes the PCM zip to `release/`.

## Roadmap

- Built-in router: rip-up and reroute for failed connections, blind/buried
  vias, zone-aware routing, routing only the selected nets, a progress preview
  of the search front.
- **KiCad 11 / IPC API build.** KiCad 11 removes the SWIG plugin API. The
  built-in router's board reading and track writing move to `kicad-python`;
  the Freerouting path additionally needs its own DSN writer and SES reader.
- Pass KiCad's custom DRC rules through to Freerouting as a `.rules` file.

## License

MIT for the plugin. Freerouting is GPL-3.0 and is downloaded from its own
releases at run time; it is not part of this package.
