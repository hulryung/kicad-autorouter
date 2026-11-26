"""Joint inward escapes are accepted only with full DRC and preserved inputs."""

import sys
import tempfile
import copy
import hashlib
import subprocess
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
try:
    import pcbnew
except ImportError:
    print("test_row_replan: skipped (needs KiCad Python)")
    raise SystemExit(0)
from test_shove_scope import outline, add_pad, add_track
from autorouter.engine.row_replan import replan_row
from autorouter.engine.ai.board_copy import protected_state, track_signature
from autorouter.engine.native.problem import NativeOptions


def fixture():
    b = pcbnew.BOARD()
    outline(b, 35, 35)
    f = pcbnew.FOOTPRINT(b)
    f.SetReference("U1")
    b.Add(f)
    dest = pcbnew.FOOTPRINT(b)
    dest.SetReference("J1")
    b.Add(dest)
    a = pcbnew.NETINFO_ITEM(b, "A")
    b.Add(a)
    c = pcbnew.NETINFO_ITEM(b, "B")
    b.Add(c)
    g = pcbnew.NETINFO_ITEM(b, "WALL")
    b.Add(g)
    for num, x, y, net in [
        (1, 10, 10, a),
        (2, 11, 10, c),
        (3, 12, 10, g),
        (4, 10, 16, g),
        (5, 11, 16, g),
        (6, 12, 16, g),
    ]:
        add_pad(f, num, x, y, net, w=0.4, h=1.2)
    add_pad(dest, 1, 25, 22, a)
    add_pad(dest, 2, 28, 22, c)
    wall = add_track(b, g, pcbnew.F_Cu, (8, 8.9), (14, 8.9))
    wall.SetLocked(True)
    add_track(b, g, pcbnew.F_Cu, (0, 19), (35, 19)).SetLocked(True)
    b.BuildConnectivity()
    return b


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    b = fixture()
    protected = protected_state(b)
    original = [track_signature(t) for t in b.GetTracks()]
    opts = NativeOptions(
        pitch_mm=0.1, time_limit_s=30, conn_time_s=5, routable_layers=[True, True]
    )
    result = replan_row(
        b, "U1", ["A", "B"], (7, 7, 15, 18), root / "accepted", options=opts
    )
    assert (
        result["after_opens"].get("A", 0) == 0
        and result["after_opens"].get("B", 0) == 0
    ), result
    out = pcbnew.LoadBoard(result["board"])
    vias = [t for t in out.GetTracks() if t.GetClass() == "PCB_VIA"]
    assert len(vias) >= 2
    assert any(10e6 < t.GetPosition().y < 13e6 for t in vias)
    assert (
        protected_state(b) == protected
        and [track_signature(t) for t in b.GetTracks()] == original
    )
    assert protected_state(out) == protected
    qfp = fixture()
    fp = next(f for f in qfp.GetFootprints() if f.GetReference() == "U1")
    ground = qfp.FindNet("WALL")
    for number, x, y in [
        (7, 9, 11.5),
        (8, 9, 13),
        (9, 9, 14.5),
        (10, 13, 11.5),
        (11, 13, 13),
        (12, 13, 14.5),
    ]:
        add_pad(fp, number, x, y, ground, w=1.2, h=0.4)
    qfp.BuildConnectivity()
    four = replan_row(qfp, "U1", ["A", "B"], (7, 7, 15, 18), root / "qfp", options=opts)
    assert four["after_opens"].get("A", 0) == 0 and four["after_opens"].get("B", 0) == 0
    rotated = fixture()

    def turn(pos):
        return pcbnew.VECTOR2I(35000000 - pos.y, pos.x)

    for pad in rotated.GetPads():
        pad.SetPosition(turn(pad.GetPosition()))
        pad.SetOrientation(pcbnew.EDA_ANGLE(90, pcbnew.DEGREES_T))
    for item in rotated.GetTracks():
        a, z = turn(item.GetStart()), turn(item.GetEnd())
        item.SetStart(a)
        item.SetEnd(z)
    rotated.BuildConnectivity()
    turned = replan_row(
        rotated, "U1", ["A", "B"], (17, 7, 28, 15), root / "rotated", options=opts
    )
    assert (
        turned["after_opens"].get("A", 0) == 0
        and turned["after_opens"].get("B", 0) == 0
    )
    from autorouter.engine.ai.board_copy import snapshot

    saved = snapshot(b, root / "input" / "board.kicad_pcb")
    digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts" / "replan-row.py"),
        str(saved),
        "U1",
        str(root / "cli"),
        "--net",
        "A",
        "--net",
        "B",
        "--bounds",
        "7",
        "7",
        "15",
        "18",
        "--time-limit",
        "30",
    ]
    process = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert process.returncode == 0, (process.stdout, process.stderr)
    assert (
        json.loads((root / "cli" / "row-replan.json").read_text())["after_opens"].get(
            "A", 0
        )
        == 0
    )
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == digest
    for label, kwargs in [
        ("bounds", {"bounds_mm": (12, 12, 10, 10)}),
        ("layers", {"escape_layers": ["In9.Cu"]}),
        ("net", {"net_names": ["missing"]}),
    ]:
        args = dict(
            board=b,
            footprint_ref="U1",
            net_names=["A", "B"],
            bounds_mm=(7, 7, 15, 18),
            output_directory=root / label,
            options=opts,
        )
        args.update(kwargs)
        try:
            replan_row(**args)
            raise AssertionError("invalid input accepted: " + label)
        except ValueError:
            pass
        assert not (root / label).exists()
    blocked = copy.deepcopy(opts)
    blocked.allow_vias = False
    try:
        replan_row(
            b, "U1", ["A", "B"], (7, 7, 15, 18), root / "blocked", options=blocked
        )
        raise AssertionError("inward alternate-layer goals accepted without vias")
    except ValueError:
        pass
    assert not (root / "blocked").exists()
print(
    "test_row_replan: joint escapes, protected input, invalid input and rejection passed"
)
