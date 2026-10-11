# KiCad Autorouter

Autoroute the open board with [Freerouting](https://www.freerouting.org/) without
leaving KiCad's PCB editor.

Until now the Freerouting workflow was: *File → Export → Specctra DSN*, start the
Java application, open the file, route, export a `.ses`, switch back to KiCad,
*File → Import → Specctra Session*. This plugin folds all of that into one
toolbar button:

1. The board is exported to Specctra DSN behind the scenes.
2. Freerouting 2.5 runs **headless, in the background**, and the dialog shows
   pass-by-pass progress (unrouted connections, violations, optimizer passes)
   with a live log. The run can be cancelled at any time.
3. The routed session is imported into the open board as **one undoable step**
   (Ctrl+Z puts the board back exactly as it was).

The routing engine stays Freerouting's Java implementation. It is **not bundled**:
the plugin downloads the pinned release and, if no suitable Java is installed, a
Temurin JRE from adoptium.net into its own data folder on first use. Nothing is
installed system-wide and no admin rights are needed.

## Requirements

- KiCad 9.0 or 10.0 (the SWIG action-plugin API). KiCad 11 drops that API; an
  IPC-API build is on the roadmap, see below.
- Internet access on first use (about 120 MB: engine 62 MB, JRE 55 MB), or a
  Java 25+ runtime already on the machine and a copy of `freerouting-2.5.0.jar`.
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

Open a board, click **Autorouter**.

| Tab | What you set |
|-----|--------------|
| **Routing** | Max passes, optimizer threads and stop threshold, via / plane-via / rip-up costs, optimizer strategies, JVM heap, extra engine arguments, SMD fanout, and whether to remove the existing tracks first. By default existing routing is kept and only unrouted connections are routed. |
| **Layers & net classes** | Which copper layers Freerouting may route on, an optional preferred-direction override per layer, and net classes to leave unrouted (power nets you want to pour or route by hand). |
| **Engine** | Where Java and the Freerouting jar come from. Leave both empty for auto-detection and the managed download. |

Click **Route**. On the first run the dialog asks before downloading the engine.
When the engine finishes, the routing is already in the board. Options are
remembered between runs.

Each run's DSN and SES files are kept in the data folder
(`jobs/<timestamp>-<board>/`, last 10 runs) for inspection; **Open job folder**
takes you there. Net class and design rules (clearance, track width, via size)
come from the board's net classes through KiCad's DSN export, as they always have.

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
  dialog.py          wx dialog: options, engine setup, progress, log
  settings.py        settings.json in the data folder
  engine/            no pcbnew or wx here, unit-testable with plain Python
    freerouting.py   pinned release, jar lookup/download, RouteOptions → command line
    java.py          find a Java ≥ 25 (settings, JAVA_HOME, PATH, managed, vendor dirs) or install a Temurin JRE
    runner.py        the engine subprocess on a worker thread, cancel support
    progress.py      Freerouting log lines → stage / pass / unrouted / violations
    session.py       the pcbnew side: DSN export, SES import, layers, net classes, job folders
    download.py      download with progress, sha256 check, safe archive extraction
    paths.py         data folder and platform names
```

The engine release is pinned in `engine/freerouting.py` (`RELEASE`: version,
jar sha256, required Java). Freerouting 2.5.0 also ships native CLI binaries;
they are not used because 2.5.0's native build crashes at the first routing
pass (a GraalVM serialization error) — the jar works.

## Tests

```bash
python3 scripts/run-tests.py        # offline: command line, progress parser, settings
# with KiCad's Python the end-to-end test runs too (needs Java 25+ and the jar;
# KICAD_AUTOROUTER_E2E_DOWNLOAD=1 lets it download the jar):
/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 scripts/run-tests.py
```

The end-to-end test takes `tests/fixtures/obd2-adapter.kicad_pcb`, strips its
tracks, exports DSN, routes it through the real engine, imports the SES and
checks that tracks came back with nothing left unrouted.

Packaging: `python3 scripts/build-package.py` writes the PCM zip to `release/`.

## Roadmap

- **KiCad 11 / IPC API build.** KiCad 11 removes the SWIG plugin API that
  provides the DSN export and SES import used here. The IPC build needs its own
  Specctra DSN writer and SES reader on top of `kicad-python`; the engine,
  runner, progress and dialog code is runtime-independent and carries over.
- Route only the selected nets.
- Pass KiCad's custom DRC rules through to the engine as a `.rules` file.

## License

MIT for the plugin. Freerouting is GPL-3.0 and is downloaded from its own
releases at run time; it is not part of this package.
