"""The assistant against the real Codex CLI on the fixture board. Opt-in
(set KICAD_AUTOROUTER_AI_LIVE=1): it costs model time. Needs KiCad's Python
and a working `codex` login."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

HERE = Path(__file__).resolve().parent


def main():
    if os.environ.get("KICAD_AUTOROUTER_AI_LIVE") != "1":
        print("test_ai_live: skipped (set KICAD_AUTOROUTER_AI_LIVE=1)")
        return
    try:
        import pcbnew
    except ImportError:
        print("test_ai_live: skipped (needs KiCad's Python)")
        return
    from autorouter.engine.ai import assistant, codex, headless
    binary = codex.find_codex()
    if not binary:
        print("test_ai_live: skipped (no codex CLI)")
        return
    board_file = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "fixtures" / "obd2-adapter.kicad_pcb"
    request = sys.argv[2] if len(sys.argv) > 2 else "Route this board completely. Leave GND to the pours."
    board = pcbnew.LoadBoard(str(board_file))
    work = Path(tempfile.mkdtemp(prefix="ai-live-"))
    print("workdir", work, "codex", codex.codex_version(binary))
    route_fn = headless.make_route_fn(board, log=print)
    a = assistant.Assistant(board, {}, route_fn, lambda: 0, work, log=print, max_rounds=3, with_drc=True)
    history = a.run(request)
    for h in history:
        print(f"round {h['round']}: {h['summary']}")
        for r in h["results"]:
            print("   ", r["type"], "ok" if r["ok"] else "FAILED", r.get("detail", "")[:160])
    print("unconnected now:", board.GetConnectivity().GetUnconnectedCount(True))
    out = work / board_file.name
    pcbnew.SaveBoard(str(out), board)
    print("saved", out)
    assert history, "no rounds happened"
    print("test_ai_live: ok")


if __name__ == "__main__":
    main()
