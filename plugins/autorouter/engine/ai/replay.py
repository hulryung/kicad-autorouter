"""Offline comparison and honest checkpoint time-lapses of retained improvements."""

import json
import math
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from . import render


def checkpoints(directory):
    """Read portable run-relative paths; never follow a manifest outside its run."""
    root = Path(directory).resolve()
    data = json.loads((root / "optimization.json").read_text(encoding="utf-8"))
    name = str(data["source"]).replace("\\", "/").split("/")[-1]

    def board(relative):
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError(f"Missing or unsafe checkpoint: {relative}")
        return path

    initial = data.get("initial", {})
    if not initial:
        legacy = root / "initial" / "review" / "review-rounds.json"
        if legacy.is_file() and root in legacy.resolve().parents:
            records = json.loads(legacy.read_text(encoding="utf-8"))
            if records:
                initial = records[0]
    frames = [
        {
            "label": "Initial board",
            "attempt": 0,
            "score": data["initial_score"],
            "metrics": initial.get("metrics", {}),
            "drc": initial.get("drc", {}),
            "board": board(initial.get("board", "initial/" + name)),
        }
    ]
    rejected = []
    for item in data.get("attempts", []):
        number = int(item["attempt"])
        if item.get("accepted"):
            frames.append(
                {
                    "label": f"Improvement {number}",
                    "attempt": number,
                    "score": item.get("score"),
                    "metrics": item.get("metrics", {}),
                    "drc": item.get("drc", {}),
                    "board": board(item.get("board", f"attempt-{number:03d}/" + name)),
                }
            )
        else:
            rejected.append(
                {"attempt": number, "reason": str(item.get("reason", "Not retained"))}
            )
    return frames, rejected, data.get("status", "unknown")


def export_replay(
    directory,
    output,
    *,
    layers="F.Cu,B.Cu,Edge.Cuts",
    size=1600,
    seconds=2.0,
    video=False,
    load_board=None,
    log=print,
    cancelled=None,
):
    """Publish a new directory atomically. Rendering failure cannot affect routing.

    Frames are accepted checkpoints, not invented intermediate routing states.
    ffmpeg is optional and only needed when video=True.
    """

    def check_cancelled():
        if cancelled and cancelled():
            raise RuntimeError("Replay export cancelled")

    check_cancelled()
    source, output = Path(directory).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Choose a new replay output directory")
    if (
        not 256 <= size <= 4096
        or not math.isfinite(seconds)
        or not 0.1 <= seconds <= 60
    ):
        raise ValueError("size must be 256..4096; seconds must be 0.1..60")
    ffmpeg = shutil.which("ffmpeg") if video else None
    if video and not ffmpeg:
        raise RuntimeError(
            "MP4 export needs ffmpeg on PATH; export frames without --video instead"
        )
    frames, rejected, status = checkpoints(source)
    if load_board is None:
        import pcbnew

        load_board = pcbnew.LoadBoard
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".replay-", dir=output.parent) as temporary:
        work = Path(temporary)
        (work / "frames").mkdir()
        dimensions = None
        bounds = None
        public = []
        for index, frame in enumerate(frames):
            check_cancelled()
            log(f'[replay] Rendering {frame["label"]} ({index + 1}/{len(frames)})')
            board = load_board(str(frame["board"]))
            box = board.GetBoardEdgesBoundingBox()
            current_bounds = (box.GetX(), box.GetY(), box.GetWidth(), box.GetHeight())
            if bounds is not None and bounds != current_bounds:
                raise ValueError("Board outline bounds changed between checkpoints")
            bounds = current_bounds
            scratch = work / "render"
            png = render.render_board(
                board, scratch, layers=layers, size=size, allow_3d=False
            )
            check_cancelled()
            if png is None:
                raise RuntimeError("Could not render a consistent 2-D checkpoint image")
            raw = png.read_bytes()
            if raw[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("Renderer did not produce a PNG")
            shape = struct.unpack(">II", raw[16:24])
            if dimensions is not None and shape != dimensions:
                raise ValueError(
                    "Checkpoint image dimensions changed; comparison would be misaligned"
                )
            dimensions = shape
            filename = f"frames/frame-{index:04d}.png"
            shutil.copyfile(png, work / filename)
            # Keep the viewer to summary measurements, without model prompts or action payloads.
            drc, metrics = frame["drc"], frame["metrics"]
            public.append(
                {
                    "label": frame["label"],
                    "attempt": frame["attempt"],
                    "score": frame["score"],
                    "image": filename,
                    "opens": drc.get("unconnected_total"),
                    "violations": (
                        len(drc["violation_identities"])
                        if "violation_identities" in drc
                        else None
                    ),
                    "length_mm": metrics.get("length_mm"),
                    "vias": metrics.get("vias"),
                }
            )
        shutil.rmtree(work / "render")
        payload = {
            "frames": public,
            "status": status,
            "layers": layers,
            "seconds": seconds,
            "rejected": rejected,
            "video": bool(video),
        }
        (work / "replay.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
        template = Path(__file__).with_name("replay.html").read_text(encoding="utf-8")
        encoded = (
            json.dumps(payload, ensure_ascii=True)
            .replace("<", "\\u003c")
            .replace("&", "\\u0026")
        )
        (work / "index.html").write_text(
            template.replace("/*REPLAY_DATA*/", encoded), encoding="utf-8"
        )
        lines = [
            "Accepted improvement checkpoints (not a continuous screen recording).",
            f"Layers: {layers}. Each checkpoint lasts {seconds:g} seconds.",
        ]
        lines.extend(
            f'{f["label"]}: score {f["score"]}; opens {f["opens"]}; vias {f["vias"]}.'
            for f in public
        )
        (work / "description.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        check_cancelled()
        if video:
            log("[replay] Encoding MP4")
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-framerate",
                    str(1 / seconds),
                    "-i",
                    str(work / "frames/frame-%04d.png"),
                    "-vf",
                    "pad=ceil(iw/2)*2:ceil(ih/2)*2:color=0x202024",
                    "-r",
                    "30",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(work / "timelapse.mp4"),
                ],
                check=True,
                capture_output=True,
                timeout=300,
            )
        check_cancelled()
        work.rename(output)
    return output / "index.html"
