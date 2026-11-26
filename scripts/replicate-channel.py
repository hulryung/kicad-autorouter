#!/usr/bin/env python3
"""Copy a routed channel into a DRC-validated new output project."""
import argparse
import json
import os
import sys
from pathlib import Path
os.environ['KICAD_AUTOROUTER_NO_REGISTER']='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board',type=Path)
    parser.add_argument('mapping',type=Path,help='JSON object: source reference -> destination reference')
    parser.add_argument('output',type=Path,help='new output directory; must not exist')
    args=parser.parse_args()
    import pcbnew
    from autorouter.engine.channel_template import replicate_channel
    if not args.board.is_file():
        parser.error('input board does not exist')
    board=pcbnew.LoadBoard(str(args.board.resolve()))
    if board is None:
        parser.error('could not load input board')
    try:
        report=replicate_channel(board,json.loads(args.mapping.read_text()),args.output)
    except (ValueError,RuntimeError) as error:
        parser.exit(1,str(error)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
