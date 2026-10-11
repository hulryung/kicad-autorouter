"""A picture of the board for the model: top copper and silkscreen as PNG."""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .context import kicad_cli


def _svg_to_png(svg: Path, png: Path, size: int) -> bool:
    if sys.platform == "darwin":
        try:
            subprocess.run(["qlmanage", "-t", "-s", str(size), "-o", str(png.parent), str(svg)],
                           capture_output=True, timeout=60)
            made = png.parent / (svg.name + ".png")
            if made.exists():
                made.replace(png)
                return True
        except Exception:
            pass
    for tool, args in (("rsvg-convert", ["-w", str(size), "-o", str(png), str(svg)]),
                       ("inkscape", [str(svg), "--export-type=png", f"--export-width={size}", f"--export-filename={png}"]),
                       ("magick", [str(svg), "-resize", f"{size}x{size}", str(png)])):
        exe = shutil.which(tool)
        if exe:
            try:
                subprocess.run([exe] + args, capture_output=True, timeout=120)
                if png.exists():
                    return True
            except Exception:
                pass
    return False


def render_board(board, workdir: Path, name: str = "board.png", layers: str = "F.Cu,F.SilkS,Edge.Cuts",
                 size: int = 1600) -> Optional[Path]:
    """Save a PNG of the board (2-D copper view where a converter exists, else
    KiCad's 3-D render), or None if nothing worked."""
    import pcbnew
    cli = kicad_cli()
    if not cli:
        return None
    workdir.mkdir(parents=True, exist_ok=True)
    copy = workdir / "render-copy.kicad_pcb"
    png = workdir / name
    try:
        pcbnew.SaveBoard(str(copy), board)
    except Exception:
        return None
    svg = workdir / "board.svg"
    try:
        subprocess.run([cli, "pcb", "export", "svg", "--layers", layers, "--page-size-mode", "2",
                        "--exclude-drawing-sheet", "-o", str(svg), str(copy)], capture_output=True, timeout=120)
        if svg.exists() and _svg_to_png(svg, png, size):
            return png
    except Exception:
        pass
    try:
        subprocess.run([cli, "pcb", "render", "--side", "top", "--quality", "basic", "--width", str(size),
                        "--height", str(size), "-o", str(png), str(copy)], capture_output=True, timeout=300)
        if png.exists():
            return png
    except Exception:
        pass
    return None
