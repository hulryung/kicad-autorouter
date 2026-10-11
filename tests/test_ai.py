"""The AI assistant without a real model: a fake codex binary returns canned
action lists, and the test checks that the board context is built, replies
are validated, and actions change the board. Needs KiCad's Python."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from autorouter.engine.ai import actions as act_mod, codex

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake" / "codex"


def offline():
    # schema validation
    assert act_mod.validate({"summary": "", "done": True, "actions": []}) == []
    bad = act_mod.validate({"actions": [{"type": "fly"}, {"type": "route", "scope": "nets"},
                                        {"type": "set_options", "options": {"bogus": 1}}]})
    assert len(bad) == 3, bad
    # json extraction from fenced / chatty replies
    assert codex.extract_json('```json\n{"a": 1}\n```')["a"] == 1
    assert codex.extract_json('Sure! {"a": {"b": 2}} done')["a"]["b"] == 2
    # the fake binary is found through the environment
    os.environ[codex.ENV_BINARY] = str(FAKE)
    assert codex.find_codex() == str(FAKE)
    work = Path(tempfile.mkdtemp())
    os.environ["FAKE_CODEX_REPLY"] = json.dumps({"summary": "hi", "actions": [{"type": "note", "text": "x"}], "done": True})
    reply = codex.run(str(FAKE), "prompt text", work, schema=act_mod.REPLY_SCHEMA)
    assert reply["summary"] == "hi" and (work / "codex-prompt-seen.md").read_text() == "prompt text"
    del os.environ["FAKE_CODEX_REPLY"]
    print("test_ai: offline ok")


def with_kicad():
    try:
        import pcbnew
    except ImportError:
        print("test_ai: board part skipped (needs KiCad's Python)")
        return
    from autorouter.engine.ai import assistant, context
    board = pcbnew.LoadBoard(str(HERE / "fixtures" / "obd2-adapter.kicad_pcb"))
    work = Path(tempfile.mkdtemp())
    ctx = context.board_context(board, {"via_cost": 25}, None, work, with_drc=False)
    assert ctx["board"]["copper_layers"] == ["F.Cu", "B.Cu"]
    assert any(n["name"] == "/CANH" for n in ctx["nets"]) and ctx["board"]["unconnected_total"] == 5
    assert ctx["sheets"] and ctx["footprints"]

    routed = {}

    def route_fn(spec):
        # stand-in for the dialog's router: remember what was asked
        routed["spec"] = spec
        return {"summary": "routed 3", "routed": 3, "failed": [], "error": None}

    replies = [
        {"summary": "round one", "done": False, "actions": [
            {"type": "set_options", "options": {"via_cost": 40, "skip_nets": ["GND"]}},
            {"type": "add_track", "net": "/CANH", "layer": "F.Cu", "width_mm": 0.3,
             "points_mm": [[150.0, 100.0], [152.0, 100.0], [152.0, 101.0]]},
            {"type": "add_via", "net": "/CANH", "x_mm": 152.0, "y_mm": 101.0},
            {"type": "route", "scope": "nets", "nets": ["/CANL"]},
        ]},
        {"summary": "round two", "done": True, "actions": [
            {"type": "remove_tracks", "nets": ["/CANH"]},
            {"type": "note", "text": "all done"},
        ]},
    ]
    os.environ["FAKE_CODEX_REPLIES"] = json.dumps(replies)
    os.environ[codex.ENV_BINARY] = str(FAKE)
    a = assistant.Assistant(board, {"via_cost": 25}, route_fn, lambda: 0, work, log=lambda s: None,
                            max_rounds=4, with_drc=False)
    history = a.run("connect the CAN nets")
    del os.environ["FAKE_CODEX_REPLIES"]
    assert len(history) == 2, history
    r1 = history[0]["results"]
    assert [r["ok"] for r in r1] == [True, True, True, True], r1
    assert a.options["via_cost"] == 40 and a.options["skip_nets"] == ["GND"]
    assert routed["spec"]["scope"] == "nets" and routed["spec"]["nets"] == ["/CANL"]
    assert routed["spec"]["options"]["via_cost"] == 40
    # round two removed the two segments and the via that round one added
    assert "removed 3 items" in history[1]["results"][0]["detail"], history[1]
    assert len(list(board.GetTracks())) == 0
    seen = (work / "codex-prompt-seen.md").read_text()
    assert "connect the CAN nets" in seen and "board-context" in seen.lower()
    assert (work / "board-context.json").exists()
    print("test_ai: board ok")


if __name__ == "__main__":
    offline()
    with_kicad()
