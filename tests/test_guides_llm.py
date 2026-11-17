"""Routing guides and the model backends: the built-in guides load and apply
to a run's options, a guide's text reaches the assistant, and the Claude
Code CLI backend returns the reply (structured or in words) through a fake
CLI. The parts that need pcbnew skip without KiCad's Python.

  python3 tests/test_guides_llm.py
"""
import functools
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "plugins"))
print = functools.partial(print, flush=True)

from autorouter.engine.ai import llm  # noqa: E402

try:
    import pcbnew
except ImportError:
    pcbnew = None


def test_claude_cli(tmp: Path):
    os.environ[llm.ENV_CLAUDE] = str(HERE / "fake" / "claude")
    os.environ["FAKE_CLAUDE_REPLY"] = json.dumps({"summary": "routed", "actions": [], "done": True})
    p = llm.Provider(llm.CLAUDE_CLI, model="claude-opus-5-5")
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
    got = llm.run(p, "route it", tmp / "a", schema=schema)
    assert got["summary"] == "routed", got
    args = json.loads((tmp / "a" / "claude-args.json").read_text())
    assert "--json-schema" in args and args[args.index("--model") + 1] == "claude-opus-5-5", args
    got = llm.run(p, "route it", tmp / "b")             # no schema: the JSON is read out of the words
    assert got["done"] is True
    img = tmp / "board.png"
    img.write_bytes(b"\x89PNG\r\n")
    llm.run(p, "look", tmp / "c", images=[img])
    assert str(img) in (tmp / "c" / "claude-prompt-seen.md").read_text()
    print("ok   Claude Code CLI: structured reply, reply in words, images named for the Read tool")


def test_status_and_compat(tmp: Path):
    ok, line = llm.status(llm.Provider(llm.CLAUDE_CLI))
    assert ok and "claude" in line, line
    p = llm.as_provider("/usr/bin/codex")
    assert p.kind == llm.CODEX_CLI and p.binary == "/usr/bin/codex"
    for kind in (llm.ANTHROPIC_API, llm.OPENAI_API):
        ok, line = llm.status(llm.Provider(kind))
        assert ok or "pip install" in line, line             # without the SDK: how to install it
    print("ok   provider status lines; a codex path still works as a backend")


def test_guides():
    from autorouter.engine import guides
    all_ = guides.load_all()
    ids = [g["id"] for g in all_]
    for want in ("ai-decides", "router-default", "outer-signals-planes", "learn-from-board", "describe"):
        assert want in ids, ids
    assert ids[0] == "ai-decides"
    for g in all_:
        if g.get("preview"):
            assert guides.preview_path(g) is not None, g["id"]
    text = guides.as_text(guides.find("outer-signals-planes"))
    assert "inner" in text and "keep to them" in text
    assert "choose it yourself" in guides.as_text(guides.find("ai-decides"))
    print("ok   built-in guides, previews present, guide text for the assistant")


def test_guide_on_board(tmp: Path):
    from autorouter.engine import guides
    from autorouter.engine.native.problem import NativeOptions
    board = pcbnew.CreateEmptyBoard() if hasattr(pcbnew, "CreateEmptyBoard") else pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    o = NativeOptions()
    g = {"style": {"layers": {"inner": "plane"}, "class_width_mm": {"PWR": 0.8}, "class_via_mm": {"PWR": [0.6, 0.3]}},
         "options": {"via_cost": 40, "route_order": ["power", "signal"], "not_an_option": 1}}
    lines = guides.apply(board, g, o)
    assert o.routable_layers == [True, False, False, True], o.routable_layers
    assert o.class_widths == {"PWR": 800_000} and o.class_vias == {"PWR": (600_000, 300_000)}
    assert o.via_cost == 40 and o.route_order == ["power", "signal"]
    assert not any("not_an_option" in l for l in lines)
    o2 = NativeOptions()
    guides.apply(board, {"style": {"layers": {"inner": "plane", "In2.Cu": "signal"}}}, o2)
    assert o2.routable_layers == [True, False, True, True], o2.routable_layers   # a layer name wins
    print("ok   a guide on a 4-layer board: plane layers, class sizes, options")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        os.environ["KICAD_AUTOROUTER_HOME"] = d
        test_claude_cli(Path(d))
        test_status_and_compat(Path(d))
        test_guides()
        if pcbnew is None:
            print("test_guides_llm: board parts skipped (needs KiCad's Python)")
        else:
            test_guide_on_board(Path(d))
    print("test_guides_llm: all passed")
