"""Autorouter logo: a green board with gold octilinear traces between pads."""
import sys
from PIL import Image, ImageDraw

N = 1024
K = N / 512


def s(*v):
    return [int(round(x * K)) for x in v]


def draw():
    im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # board: vertical gradient, rounded corners
    grad = Image.new("RGBA", (N, N))
    gd = ImageDraw.Draw(grad)
    top, bot = (22, 128, 84), (8, 70, 46)
    for y in range(N):
        t = y / (N - 1)
        gd.line([(0, y), (N, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle(s(16, 16, 496, 496), radius=int(96 * K), fill=255)
    im.paste(grad, (0, 0), mask)
    d.rounded_rectangle(s(16, 16, 496, 496), radius=int(96 * K), outline=(120, 210, 150, 255), width=int(10 * K))
    gold, dark = (240, 184, 72, 255), (8, 70, 46, 255)
    w = int(40 * K)

    def trace(pts):
        pts = [tuple(s(*p)) for p in pts]
        d.line(pts, fill=gold, width=w, joint="curve")
        for p in pts:
            r = w // 2
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=gold)

    def pad(x, y, r=46, hole=18):
        d.ellipse(s(x - r, y - r, x + r, y + r), fill=gold)
        d.ellipse(s(x - hole, y - hole, x + hole, y + hole), fill=dark)

    trace([(112, 140), (400, 140)])
    trace([(112, 372), (200, 372), (316, 256), (400, 256)])
    pad(112, 140)
    pad(400, 140)
    pad(112, 372)
    pad(400, 256)
    return im


if __name__ == "__main__":
    out = sys.argv[1]
    big = draw()
    big.resize((512, 512), Image.LANCZOS).save(f"{out}/logo-512.png")
    for size, name in ((64, "icon.png"), (48, "icon-48.png"), (24, "icon-24.png")):
        big.resize((size, size), Image.LANCZOS).save(f"{out}/{name}")
