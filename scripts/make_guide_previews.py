"""Preview images for the routing guides: the same spot of boards routed
with each guide (and of the designer's board), cropped alike, so the
Routing guide page can show what each choice looks like.

  <KiCad python3> scripts/make_guide_previews.py --at U202 --half 14 \
      router-default=.improve/bcu-baseline/routed.kicad_pcb \
      outer-signals-planes=.improve/bcu-style1/routed.kicad_pcb \
      designer=/path/to/designer.kicad_pcb

writes plugins/autorouter/plugin_resources/guides/<name>.png (600 px).
"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
OUT = HERE.parent / "plugins" / "autorouter" / "plugin_resources" / "guides"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("boards", nargs="+", help="name=board.kicad_pcb")
    ap.add_argument("--at", default="", help="a footprint reference to centre on, or x,y in mm")
    ap.add_argument("--half", type=float, default=14.0, help="half the crop side, mm")
    ap.add_argument("--size", type=int, default=600)
    a = ap.parse_args()
    import pcbnew
    from improve_bench import Renderer
    OUT.mkdir(parents=True, exist_ok=True)
    first = a.boards[0].split("=", 1)[1]
    if "," in a.at:
        cx, cy = map(float, a.at.split(","))
    else:
        board = pcbnew.LoadBoard(first)
        fp = board.FindFootprintByReference(a.at) if a.at else None
        if fp is None:
            sys.exit(f"no footprint {a.at}")
        cx, cy = fp.GetPosition().x / 1e6, fp.GetPosition().y / 1e6
    with tempfile.TemporaryDirectory() as tmp:
        for item in a.boards:
            name, path = item.split("=", 1)
            r = Renderer(Path(path), name, Path(tmp))
            png = r.crop("preview", cx, cy, a.half, a.size)
            if png is None:
                print(f"{name}: no image (kicad-cli or an SVG converter missing)")
                continue
            shutil.copy(png, OUT / f"{name}.png")
            print(f"{name}: {OUT / (name + '.png')}")


if __name__ == "__main__":
    main()
