"""End to end without the GUI: a real board goes out as DSN, through the
Freerouting engine and back in as tracks. Needs KiCad's Python (pcbnew), a
Java runtime recent enough for the pinned release, and the jar (downloaded
on demand: set KICAD_AUTOROUTER_E2E_DOWNLOAD=1 to allow that). Skips
otherwise, so the offline suite still passes on a machine without KiCad.

Run it with KiCad's interpreter, e.g. on macOS
  /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 tests/test_e2e_headless.py [board.kicad_pcb]
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

try:
    import pcbnew
except ImportError:
    print("test_e2e_headless: skipped (no pcbnew; run with KiCad's Python)")
    sys.exit(0)

from autorouter.engine import freerouting, java, session
from autorouter.engine.freerouting import RELEASE, RouteOptions
from autorouter.engine.progress import ProgressParser
from autorouter.engine.runner import EngineRun

HERE = Path(__file__).resolve().parent


def main():
    board_file = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "fixtures" / "obd2-adapter.kicad_pcb"
    if not board_file.is_file():
        print(f"test_e2e_headless: skipped (no board at {board_file})")
        return
    found = java.find_java(RELEASE.min_java)
    if not found:
        print(f"test_e2e_headless: skipped (no Java {RELEASE.min_java}+)")
        return
    jar = freerouting.find_jar()
    if not jar:
        if os.environ.get("KICAD_AUTOROUTER_E2E_DOWNLOAD") != "1":
            print("test_e2e_headless: skipped (jar not downloaded; set KICAD_AUTOROUTER_E2E_DOWNLOAD=1)")
            return
        jar = freerouting.install_jar(lambda label, done, total: None)

    work = Path(tempfile.mkdtemp())
    board = pcbnew.LoadBoard(str(board_file))
    removed = session.remove_tracks(board)
    assert len(board.GetTracks()) == 0, "remove_tracks left tracks behind"
    layers = session.copper_layers(board)
    assert layers and layers[0][1] == "F.Cu" and layers[-1][1] == "B.Cu", layers
    classes = session.net_class_names(board)
    assert classes and classes[0] == "Default", classes

    job = session.JobFiles(work, work / "board.dsn", work / "board.ses")
    session.export_dsn(board, job.dsn)
    assert job.dsn.stat().st_size > 0

    options = RouteOptions(max_passes=5, improvement_threshold=5.0)
    cmd = freerouting.build_command(found.path, jar, options, job.dsn, job.ses,
                                    host_version="test", user_data=work / "data")
    parser = ProgressParser()
    done = threading.Event()
    result = {}

    def on_done(rc):
        result["rc"] = rc
        done.set()

    run = EngineRun(cmd, on_line=parser.feed, on_done=on_done, cwd=str(work))
    run.start()
    assert done.wait(600), "engine did not finish in 10 minutes"
    status = parser.status
    assert result["rc"] == 0, (result, status.errors)
    assert status.finished_ok, status
    assert status.unrouted == 0, f"{status.unrouted} connections left unrouted"
    assert status.violations == 0, f"{status.violations} violations"

    session.import_ses(board, job.ses)
    tracks = len(board.GetTracks())
    assert tracks > 0, "no tracks after import"
    print(f"test_e2e_headless: ok ({removed} tracks removed, {tracks} tracks after routing, "
          f"{status.pass_number} passes, {found})")


if __name__ == "__main__":
    main()
