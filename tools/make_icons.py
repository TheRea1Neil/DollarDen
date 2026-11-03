#!/usr/bin/env python3
"""Build the launcher-icon sources from the original logo.

    tools/.imgvenv/bin/python tools/make_icons.py      # needs Pillow
    cd app && dart run flutter_launcher_icons

Two things here are not guesswork and should not be "simplified":

1. The background is removed by flooding inward from the border, not by
   a global "white is transparent" threshold. The robot's own left face
   panel, its eye highlights and the banknote are all near-white; a
   threshold punches holes straight through them.

2. The adaptive-icon foreground is sized deliberately.
   flutter_launcher_icons emits <inset android:inset="16%">, so the
   drawable covers 68% of the 108dp layer, and launchers show only the
   central 72dp and mask it to a circle or squircle. FRACTION below is
   chosen so the artwork lands around 56% of the layer -- large enough
   to read, small enough that the antenna and the banknote survive a
   circular mask.
"""
from PIL import Image, ImageDraw
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / 'brand' / 'nnf-personal-logo.jpeg'
OUT = ROOT / 'app' / 'assets' / 'logo'
SENTINEL = (255, 0, 255)
FRACTION = 0.82

def squared_artwork():
    src = Image.open(SRC).convert('RGB')
    box = src.convert('L').point(lambda p: 255 if p < 242 else 0).getbbox()
    art = src.crop(box)
    w, h = art.size
    side = max(w, h)
    canvas = Image.new('RGB', (side, side), 'white')
    canvas.paste(art, ((side - w) // 2, (side - h) // 2))
    return canvas

def cutout(img):
    flat = img.copy()
    w, h = flat.size
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        ImageDraw.floodfill(flat, xy, SENTINEL, thresh=18)
    rgba = img.convert('RGBA')
    fp, rp = flat.load(), rgba.load()
    for y in range(h):
        for x in range(w):
            if fp[x, y] == SENTINEL:
                r, g, b, _ = rp[x, y]
                rp[x, y] = (r, g, b, 0)
    return rgba

def place(source, name, canvas_px, fraction, bg):
    target = int(canvas_px * fraction)
    scaled = source.resize((target, target), Image.LANCZOS)
    canvas = Image.new('RGBA', (canvas_px, canvas_px), bg or (0, 0, 0, 0))
    off = (canvas_px - target) // 2
    canvas.paste(scaled, (off, off), scaled if scaled.mode == 'RGBA' else None)
    canvas.save(OUT / name)
    print(f'  {name:24s} {canvas.size[0]}px, artwork {fraction:.0%}')

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    sq = squared_artwork()
    cut = cutout(sq)
    print('writing icon sources:')
    place(sq, 'icon.png', 1024, 0.78, (255, 255, 255, 255))   # iOS: no alpha
    place(cut, 'icon_foreground.png', 1024, FRACTION, None)
    place(cut, 'logo.png', 512, 0.96, None)                   # in-app
    print(f'adaptive foreground lands at {FRACTION * 0.68:.0%} of the 108dp layer')
    return 0

if __name__ == '__main__':
    sys.exit(main())
