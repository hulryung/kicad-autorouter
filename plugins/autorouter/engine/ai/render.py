"""A picture of the board for the model: top copper and silkscreen as PNG."""
import os
import re
import shutil
import subprocess
import sys
import struct
import zlib
from pathlib import Path
from typing import Optional

from .context import kicad_cli


def _write_rgba_png(path, width, height, pixels):
    """Write NanoSVG's RGBA output without a GUI app or Pillow dependency."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    stride = width * 4
    rows = b''.join(b'\0' + pixels[y * stride:(y + 1) * stride] for y in range(height))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' +
                     chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) +
                     chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def _svg_to_png(svg: Path, png: Path, size: int) -> bool:
    # KiCad ships wxPython/NanoSVG. Rasterizing at an explicit pixel scale
    # avoids Quick Look's physical-unit thumbnail padding, which also made
    # coordinate-based close-ups point at the wrong part of the board.
    try:
        import wx.svg
        source = re.sub(rb'(<svg\b[^>]*>)',
                        rb'\1<rect x="0" y="0" width="100%" height="100%" fill="#202024"/>',
                        svg.read_bytes(), count=1)
        drawing = wx.svg.SVGimage.CreateFromBytes(source)
        scale = size / max(drawing.width, drawing.height)
        width = max(1, round(drawing.width * scale))
        height = max(1, round(drawing.height * scale))
        pixels = drawing.Rasterize(scale=scale, width=width, height=height)
        _write_rgba_png(png, width, height, pixels)
        return True
    except (ImportError, AttributeError, ValueError, RuntimeError):
        pass
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


def crop_at(board, full_png: Path, x_mm: float, y_mm: float, size_mm: float, out: Path) -> Optional[Path]:
    """A close-up of the full-board PNG around a board position (mm). The
    PNG was exported in board-area page mode, so pixels map linearly onto
    the board's bounding box."""
    try:
        import pcbnew
        bb = board.GetBoardEdgesBoundingBox()
        if bb.GetWidth() <= 0:
            return None
        if sys.platform == "darwin" or shutil.which("sips"):
            # read the image size with sips, crop with sips (no PIL needed)
            info = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(full_png)],
                                  capture_output=True, text=True).stdout
            w = int([l for l in info.splitlines() if "pixelWidth" in l][0].split()[-1])
            h = int([l for l in info.splitlines() if "pixelHeight" in l][0].split()[-1])
        else:
            return None
        bx, by = bb.GetX() / 1e6, bb.GetY() / 1e6
        bw, bh = bb.GetWidth() / 1e6, bb.GetHeight() / 1e6
        scale = min(w / bw, h / bh)                 # the page mode keeps the aspect ratio
        px = (x_mm - bx) * scale
        py = (y_mm - by) * scale
        side = int(size_mm * scale)
        left = int(max(0, min(w - side, px - side / 2)))
        top = int(max(0, min(h - side, py - side / 2)))
        subprocess.run(["sips", "-c", str(side), str(side), "--cropOffset", str(top), str(left),
                        str(full_png), "--out", str(out)], capture_output=True)
        if out.exists():
            subprocess.run(["sips", "-Z", "900", str(out)], capture_output=True)
            return out
    except Exception:
        return None
    return None
