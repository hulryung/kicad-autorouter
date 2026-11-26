#!/usr/bin/env python3
"""Route an isolated sense component to a designated pad on a validated copy."""
import argparse
import json
import math
import os
import sys
from pathlib import Path
os.environ['KICAD_AUTOROUTER_NO_REGISTER']='1'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('board',type=Path)
    parser.add_argument('source_pad',help='REF.PAD identifying the isolated sense component')
    parser.add_argument('join_pad',help='REF.PAD where sense may join force copper')
    parser.add_argument('output',type=Path,help='new output directory')
    parser.add_argument('--pitch',type=float,default=.1,help='grid pitch in mm')
    parser.add_argument('--width',type=float,help='track width in mm; default follows net rules')
    parser.add_argument('--separation',type=float,help='sense/force separation in mm; default follows net clearance')
    parser.add_argument('--time-limit',type=float,default=60,help='search time limit in seconds')
    parser.add_argument('--no-vias',action='store_true')
    args=parser.parse_args()
    for name,value in [('pitch',args.pitch),('width',args.width)]:
        if value is not None and (not math.isfinite(value) or value<=0):
            parser.error(name+' must be finite and positive')
    if not math.isfinite(args.time_limit) or args.time_limit<0:
        parser.error('time-limit must be finite and nonnegative')
    import pcbnew
    from autorouter.engine.kelvin import route_kelvin
    from autorouter.engine.native.problem import NativeOptions
    if not args.board.is_file():parser.error('input board does not exist')
    board=pcbnew.LoadBoard(str(args.board.resolve()))
    if board is None:parser.error('could not load input board')
    opts=NativeOptions(pitch_mm=args.pitch,time_limit_s=args.time_limit,allow_vias=not args.no_vias)
    if args.width is not None:
        opts.track_mode='fixed';opts.track_width=int(round(args.width*1000000))
    try:
        report=route_kelvin(board,args.source_pad,args.join_pad,args.output,options=opts,separation_mm=args.separation,
                            status=lambda message:print(message,file=sys.stderr,flush=True))
    except (ValueError,RuntimeError) as error:
        parser.exit(1,str(error)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
