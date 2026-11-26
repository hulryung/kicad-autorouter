#!/usr/bin/env python3
"""Jointly replan local IC escapes and publish a validated board copy."""

import argparse
import json
import math
import os
import sys
from pathlib import Path

os.environ["KICAD_AUTOROUTER_NO_REGISTER"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("board", type=Path)
    parser.add_argument("footprint")
    parser.add_argument("output", type=Path, help="new output directory")
    parser.add_argument(
        "--net",
        action="append",
        required=True,
        help="affected net; repeat for row nets and crossing blockers",
    )
    parser.add_argument(
        "--bounds",
        nargs=4,
        type=float,
        required=True,
        metavar=("X0", "Y0", "X1", "Y1"),
        help="local region in mm",
    )
    parser.add_argument(
        "--escape-layer",
        action="append",
        help="allowed inward via goal layer; default B.Cu",
    )
    parser.add_argument(
        "--pitch", type=float, default=0.15, help="global routing grid in mm"
    )
    parser.add_argument("--local-pitch", type=float, default=0.05)
    parser.add_argument("--escape-depth", type=float, default=1.3)
    parser.add_argument("--stagger", type=float, default=0.7)
    parser.add_argument(
        "--time-limit",
        type=float,
        default=90,
        help="budget per local/global routing stage in seconds",
    )
    args = parser.parse_args()
    if not math.isfinite(args.pitch) or args.pitch <= 0:
        parser.error("pitch must be finite and positive")
    if not math.isfinite(args.time_limit) or args.time_limit < 0:
        parser.error("time-limit must be finite and nonnegative")
    if not args.board.is_file():
        parser.error("input board does not exist")
    import pcbnew
    from autorouter.engine.row_replan import replan_row
    from autorouter.engine.native.problem import NativeOptions

    board = pcbnew.LoadBoard(str(args.board.resolve()))
    if board is None:
        parser.error("could not load input board")
    opts = NativeOptions(
        pitch_mm=args.pitch,
        time_limit_s=args.time_limit,
        routable_layers=[True] * board.GetCopperLayerCount(),
        conn_time_s=5,
        rip_rounds=4,
    )
    try:
        result = replan_row(
            board,
            args.footprint,
            args.net,
            args.bounds,
            args.output,
            options=opts,
            local_pitch_mm=args.local_pitch,
            escape_depth_mm=args.escape_depth,
            stagger_mm=args.stagger,
            escape_layers=args.escape_layer,
            status=lambda s: print(s, file=sys.stderr, flush=True),
        )
    except (ValueError, RuntimeError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
