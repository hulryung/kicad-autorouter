#!/usr/bin/env python3
"""Export an offline improvement comparison, PNG checkpoints and optional MP4.
Run with KiCad's Python; MP4 additionally requires ffmpeg on PATH.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from autorouter.engine.ai.replay import export_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="directory containing optimization.json")
    parser.add_argument("--out", type=Path, required=True, help="new output directory")
    parser.add_argument("--layers", default="F.Cu,B.Cu,Edge.Cuts")
    parser.add_argument("--size", type=int, default=1600)
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument(
        "--video", action="store_true", help="also encode timelapse.mp4 with ffmpeg"
    )
    args = parser.parse_args()
    print(
        export_replay(
            args.run,
            args.out,
            layers=args.layers,
            size=args.size,
            seconds=args.seconds,
            video=args.video,
        )
    )


if __name__ == "__main__":
    main()
