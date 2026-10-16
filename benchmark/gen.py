#!/usr/bin/env python3
"""Generate the SVGs used for the README screenshots and the benchmark.

    gen.py gallery OUTDIR   # a dozen light, colourful drawings (screenshots)
    gen.py heavy OUTDIR     # a few big files that are slow in librsvg (benchmark)

Everything is seeded, so the output is identical on every run.
"""

import colorsys
import math
import random
import sys
from pathlib import Path

NS = 'xmlns="http://www.w3.org/2000/svg"'


def hsl(h, s=.6, l=.5):
    r, g, b = colorsys.hls_to_rgb(h % 1, l, s)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def svg(w, h, body, bg=None):
    rect = f'<rect width="{w}" height="{h}" fill="{bg}"/>' if bg else ""
    return f'<svg {NS} width="{w}" height="{h}" viewBox="0 0 {w} {h}">{rect}{body}</svg>'


# --- gallery -----------------------------------------------------------------------------

def spiral(seed, n=900):
    rnd = random.Random(seed)
    hue = rnd.random()
    out = []
    for i in range(n):
        a = i * 2.39996
        r = 8 * math.sqrt(i)
        out.append(f'<circle cx="{400 + r * math.cos(a):.1f}" cy="{300 + r * math.sin(a):.1f}" '
                   f'r="{2 + i / n * 7:.1f}" fill="{hsl(hue + i / n * .3)}"/>')
    return svg(800, 600, "".join(out), "#14141c")


def scatter(seed, n=3000, shift=0.0):
    rnd = random.Random(seed)
    out = ['<g stroke="#ccc" stroke-width="1">']
    out += [f'<line x1="{x}" y1="40" x2="{x}" y2="560"/>' for x in range(60, 800, 80)]
    out += [f'<line x1="40" y1="{y}" x2="760" y2="{y}"/>' for y in range(60, 600, 80)]
    out.append("</g>")
    for _ in range(n):
        k = rnd.randrange(3)
        cx, cy = [(250, 300), (520, 220), (480, 430)][k]
        x, y = rnd.gauss(cx + shift * (k + 1), 55), rnd.gauss(cy - shift * 2 * k, 55)
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{hsl(.08 + k * .3)}" fill-opacity=".45"/>')
    out.append('<text x="60" y="30" font-family="sans-serif" font-size="18">clusters'
               f'{" (v2)" if shift else " (v1)"}</text>')
    return svg(800, 600, "".join(out), "white")


def waves(seed, lines=60):
    rnd = random.Random(seed)
    hue = rnd.random()
    out = []
    for j in range(lines):
        pts = " ".join(f"{x},{300 + 160 * math.sin(x / 70 + j / 9) * math.sin(x / 210 + j / 5) + j * 3 - 90:.1f}"
                       for x in range(0, 801, 8))
        out.append(f'<polyline points="{pts}" fill="none" stroke="{hsl(hue + j / lines * .4, .7, .55)}" stroke-width="2"/>')
    return svg(800, 600, "".join(out), "#101820")


def rings(seed, n=40):
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        cx, cy, r = rnd.uniform(0, 800), rnd.uniform(0, 600), rnd.uniform(20, 140)
        out.append(f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="none" '
                   f'stroke="{hsl(rnd.random())}" stroke-width="{rnd.uniform(2, 9):.1f}" stroke-opacity=".7"/>')
    return svg(800, 600, "".join(out), "#fafafa")


def grid(seed, cells=24):
    rnd = random.Random(seed)
    size = 600 / cells
    hue = rnd.random()
    out = []
    for i in range(cells):
        for j in range(cells):
            if rnd.random() < .55:
                out.append(f'<rect x="{i * size:.1f}" y="{j * size:.1f}" width="{size - 2:.1f}" height="{size - 2:.1f}" '
                           f'rx="3" fill="{hsl(hue + (i + j) / cells * .25, .65, .5)}"/>')
    return svg(600, 600, "".join(out), "#ffffff")


def mandala(seed, petals=24):
    rnd = random.Random(seed)
    hue = rnd.random()
    out = []
    for k in range(petals):
        out.append(f'<ellipse cx="300" cy="170" rx="28" ry="120" fill="{hsl(hue + k / petals * .5)}" '
                   f'fill-opacity=".55" transform="rotate({k * 360 / petals:.1f} 300 300)"/>')
    return svg(600, 600, "".join(out), "#1b1030")


def bars(seed, shift=0):
    rnd = random.Random(seed)
    out = ['<line x1="50" y1="520" x2="760" y2="520" stroke="#333"/>']
    for i in range(14):
        h = rnd.uniform(60, 420) + (shift * (i % 4) * 12)
        out.append(f'<rect x="{60 + i * 50}" y="{520 - h:.0f}" width="36" height="{h:.0f}" fill="{hsl(.58 + i * .01)}"/>')
    return svg(800, 560, "".join(out), "white")


GALLERY = [
    ("spiral-a", lambda: spiral(1)), ("spiral-b", lambda: spiral(2, 1400)),
    ("clusters-v1", lambda: scatter(5)), ("clusters-v2", lambda: scatter(5, shift=14)),
    ("waves-a", lambda: waves(3)), ("waves-b", lambda: waves(4, 90)),
    ("rings-a", lambda: rings(6)), ("rings-b", lambda: rings(7, 70)),
    ("grid-a", lambda: grid(8)), ("grid-b", lambda: grid(9, 36)),
    ("mandala-a", lambda: mandala(10)), ("mandala-b", lambda: mandala(11, 40)),
    ("bars-v1", lambda: bars(12)), ("bars-v2", lambda: bars(12, shift=1)),
]


# --- heavy files -------------------------------------------------------------------------

def heavy_scatter(n=250_000):
    """A big scatter plot: one <circle> per point, like matplotlib/plotly exports."""
    rnd = random.Random(1)
    out = []
    for _ in range(n):
        x, y = rnd.gauss(2000, 650), rnd.gauss(1500, 450)
        out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{hsl(x / 4000)}" fill-opacity=".35"/>')
    return svg(4000, 3000, "".join(out), "white")


def heavy_paths(n=3000, steps=60):
    """Thousands of semi-transparent random-walk polylines, like a map or a PCB."""
    rnd = random.Random(2)
    out = []
    for _ in range(n):
        x, y = rnd.uniform(0, 4000), rnd.uniform(0, 3000)
        pts = []
        for _ in range(steps):
            x += rnd.gauss(0, 30)
            y += rnd.gauss(0, 30)
            pts.append(f"{x:.1f},{y:.1f}")
        out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{hsl(rnd.random())}" '
                   f'stroke-width="{rnd.uniform(1, 4):.1f}" stroke-opacity=".5"/>')
    return svg(4000, 3000, "".join(out), "#0d1117")


def heavy_filters(n=60):
    """Blurred, masked, gradient-filled groups: effects that librsvg handles slowly."""
    rnd = random.Random(3)
    defs = ('<defs><filter id="b" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="14"/></filter>'
            '<filter id="s"><feDropShadow dx="8" dy="8" stdDeviation="10" flood-opacity=".5"/></filter>'
            '<radialGradient id="g"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#000"/></radialGradient>'
            '<mask id="m"><rect width="4000" height="3000" fill="url(#g)"/></mask></defs>')
    out = [defs]
    for _ in range(n):
        cx, cy, r = rnd.uniform(0, 4000), rnd.uniform(0, 3000), rnd.uniform(150, 600)
        out.append(f'<g filter="url(#b)" mask="url(#m)"><circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" fill="{hsl(rnd.random())}"/></g>')
        out.append(f'<rect x="{cx:.0f}" y="{cy:.0f}" width="{r / 2:.0f}" height="{r / 3:.0f}" fill="{hsl(rnd.random())}" filter="url(#s)"/>')
    return svg(4000, 3000, "".join(out), "#f4f1ea")


def heavy_clipped(n=7000, steps=40):
    """Like a matplotlib export of many lines: thousands of paths, each with clip-path="url(#axes)"."""
    rnd = random.Random(4)
    out = ['<defs><clipPath id="axes"><rect x="60" y="30" width="680" height="500"/></clipPath></defs>',
           '<rect x="60" y="30" width="680" height="500" fill="#ebebeb"/>']
    for _ in range(n):
        x, y = rnd.uniform(60, 740), rnd.uniform(30, 530)
        d = [f"M {x:.2f} {y:.2f}"]
        for _ in range(steps):
            x += rnd.gauss(0, 12)
            y += rnd.gauss(0, 12)
            d.append(f"L {x:.2f} {y:.2f}")
        out.append(f'<path d="{" ".join(d)}" clip-path="url(#axes)" '
                   f'style="fill: none; stroke: {hsl(rnd.random(), .7, .45)}; stroke-opacity: .4; stroke-width: 1.5"/>')
    return svg(800, 560, "".join(out), "white")


HEAVY = [("scatter-250k", heavy_scatter), ("paths-3000", heavy_paths), ("filters-blur", heavy_filters),
         ("clipped-7000", heavy_clipped)]


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("gallery", "heavy"):
        sys.exit(__doc__)
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    for name, make in GALLERY if sys.argv[1] == "gallery" else HEAVY:
        (out / f"{name}.svg").write_text(make())
        print(f"{name}.svg  {(out / f'{name}.svg').stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
