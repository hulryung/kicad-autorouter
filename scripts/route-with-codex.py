#!/usr/bin/env python3
"""Route and improve copies of a PCB with Codex, retaining the best verified result.
Run with KiCad's Python. Requires authenticated Codex CLI and kicad-cli.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins'))
from autorouter.engine.ai.optimize import Optimizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--attempts', type=int, default=8)
    parser.add_argument('--patience', type=int, default=4)
    parser.add_argument('--min-gain', type=int, default=1)
    parser.add_argument('--model', default='')
    parser.add_argument('--codex', default='')
    parser.add_argument('--time-limit', type=float, default=10, help='minutes per native route action')
    parser.add_argument('--request', default='Route the board completely, then improve routing quality.')
    args = parser.parse_args()
    run = Optimizer(args.board, args.out, binary=args.codex, model=args.model,
                    attempts=args.attempts, patience=args.patience, min_gain=args.min_gain,
                    options={'time_limit_min': args.time_limit})
    result = run.run(args.request)
    print(json.dumps({'status': result['status'], 'initial_score': result['initial_score'],
                      'best_score': result['best_score'], 'best_board': result['best_board']}, indent=2))


if __name__ == '__main__':
    main()
