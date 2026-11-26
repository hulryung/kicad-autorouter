"""Replay preserves checkpoint truth, local file boundaries and atomic publication."""

import json
import struct
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from autorouter.engine.ai import replay


class Box:
    def GetX(self):
        return 0

    def GetY(self):
        return 0

    def GetWidth(self):
        return 100

    def GetHeight(self):
        return 100


class Board:
    def GetBoardEdgesBoundingBox(self):
        return Box()


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    run = root / "moved-run"
    run.mkdir()
    for folder in ["initial", "attempt-001", "attempt-002"]:
        (run / folder).mkdir()
        (run / folder / "board.kicad_pcb").write_text(folder)
    data = {
        "source": "C:\\old\\board.kicad_pcb",
        "initial_score": 20,
        "status": "cancelled",
        "attempts": [
            {
                "attempt": 1,
                "accepted": True,
                "score": 80,
                "metrics": {"vias": 2},
                "drc": {"unconnected_total": 0, "violation_identities": []},
            },
            {
                "attempt": 2,
                "accepted": False,
                "score": 99,
                "reason": '</script><script>alert("not markup")</script>',
            },
        ],
    }
    manifest = run / "optimization.json"
    manifest.write_text(json.dumps(data))
    frames, rejected, status = replay.checkpoints(run)
    assert len(frames) == 2 and frames[-1]["board"].parent.name == "attempt-001"
    assert status == "cancelled" and len(rejected) == 1
    loaded = []

    def load(path):
        loaded.append(path)
        return Board()

    def render(board, folder, **kwargs):
        assert kwargs["allow_3d"] is False
        folder.mkdir(exist_ok=True)
        p = folder / "board.png"
        replay.render._write_rgba_png(p, 8, 8, b"\0\0\0\xff" * 64)
        return p

    with patch.object(replay.render, "render_board", side_effect=render):
        page = replay.export_replay(
            run, root / "export", load_board=load, log=lambda _: None
        )
    assert len(loaded) == 2 and not any("attempt-002" in p for p in loaded)
    text = page.read_text()
    assert "</script><script>alert" not in text
    assert "\\u003c/script>" in text
    exported = json.loads((page.parent / "replay.json").read_text())
    assert exported["frames"][0]["opens"] is None
    assert len(list((page.parent / "frames").glob("*.png"))) == 2
    assert not (page.parent / "render").exists()
    try:
        replay.export_replay(run, root / "export")
    except FileExistsError:
        pass
    else:
        raise AssertionError("overwrote an existing replay")
    with patch.object(replay.render, "render_board", return_value=None):
        try:
            replay.export_replay(run, root / "failed", load_board=load)
        except RuntimeError:
            pass
        else:
            raise AssertionError("published missing images")
    assert not (root / "failed").exists()
    with patch.object(replay.render, "render_board") as renderer:
        try:
            replay.export_replay(
                run, root / "cancelled", load_board=load, cancelled=lambda: True
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("ignored export cancellation")
        renderer.assert_not_called()
    assert not (root / "cancelled").exists()
    data["initial"] = {"board": "../outside.kicad_pcb"}
    (root / "outside.kicad_pcb").write_text("outside")
    manifest.write_text(json.dumps(data))
    try:
        replay.checkpoints(run)
    except ValueError:
        pass
    else:
        raise AssertionError("followed a checkpoint outside the run")
print(
    "test_improvement_replay: accepted-only frames, relocation, escaping and atomic exports passed"
)
