#!/usr/bin/env python3
"""Generate every Quorum brand asset from one geometry definition.

The mark is "quorum reached": six seats above (the members), one solid floor below (the
decision), the tail that makes it a Q, and one seat lit in the signal colour (the member
that completes the quorum). Outputs the inline template partial, standalone SVGs, the
favicon set and PNG icons, wordmark lock-ups with outlined text (so they render the same
everywhere, including a README), and a social card.

    pip install fonttools brotli pillow   # dev only (requirements-dev.txt)
    python3 tools/brand.py
"""

from __future__ import annotations

import io
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/quorum/static/brand"
DOCS = ROOT / "docs/brand"
PARTIAL = ROOT / "src/quorum/templates/partials/mark.svg"
FRAUNCES = ROOT / "src/quorum/static/fonts/fraunces-var.woff2"

INDIGO, INDIGO_DARK = "#3a3fd9", "#9aa0ff"
SIGNAL, SIGNAL_DARK = "#e5486e", "#ff7a98"
INK, INK_DARK, PAPER, PAPER_DARK = "#15171c", "#eef0f4", "#fbfaf7", "#101218"

CX = CY = 48.0
R, STROKE, SEAT = 30.0, 10.4, 4.9
FLOOR = (10.0, 170.0)          # degrees, clockwise from 3 o'clock (SVG y points down)
SEATS, GAP = 6, 26.0           # six seats over the top, GAP degrees clear of the floor ends
TAIL = (40.0, 51.0)            # angle, outer radius


def _pt(a, r=R):
    t = math.radians(a)
    return CX + r * math.cos(t), CY + r * math.sin(t)


def _seat_angles():
    a0, a1 = FLOOR
    step = (360 - (a1 - a0) - 2 * GAP) / (SEATS - 1)
    return [a1 + GAP + i * step for i in range(SEATS)]


def _bbox(pad=6.0):
    xs, ys = [], []
    for a in range(int(FLOOR[0]), int(FLOOR[1]) + 1):
        x, y = _pt(a)
        xs += [x - STROKE / 2, x + STROKE / 2]
        ys += [y - STROKE / 2, y + STROKE / 2]
    for a in _seat_angles():
        x, y = _pt(a)
        xs += [x - SEAT, x + SEAT]
        ys += [y - SEAT, y + SEAT]
    x, y = _pt(TAIL[0], TAIL[1])
    xs += [x + STROKE / 2]
    ys += [y + STROKE / 2]
    x0, y0, x1, y1 = min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad
    side = max(x1 - x0, y1 - y0)
    return (x0 + x1 - side) / 2, (y0 + y1 - side) / 2, side


def mark_body(ink: str, signal: str | None) -> str:
    (x0, y0), (x1, y1) = _pt(FLOOR[0]), _pt(FLOOR[1])
    parts = [f'<path d="M{x0:.2f} {y0:.2f}A{R:g} {R:g} 0 0 1 {x1:.2f} {y1:.2f}" fill="none" stroke="{ink}" '
             f'stroke-width="{STROKE}" stroke-linecap="round"/>']
    for i, a in enumerate(_seat_angles()):
        x, y = _pt(a)
        fill = signal if (signal and i == SEATS - 1) else ink
        parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{SEAT}" fill="{fill}"/>')
    (tx0, ty0), (tx1, ty1) = _pt(TAIL[0]), _pt(*TAIL)
    parts.append(f'<path d="M{tx0:.2f} {ty0:.2f}L{tx1:.2f} {ty1:.2f}" stroke="{ink}" stroke-width="{STROKE}" '
                 f'stroke-linecap="round"/>')
    return "".join(parts)


def viewbox():
    x, y, s = _bbox()
    return f"{x:.2f} {y:.2f} {s:.2f} {s:.2f}"


def svg(body: str, size=None, extra="") -> str:
    wh = f' width="{size}" height="{size}"' if size else ""
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox()}"{wh}{extra}>{body}</svg>\n'


# ------------------------------------------------------------------ wordmark outlines

def _instance_font(weight=640):
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    f = TTFont(str(FRAUNCES))
    if "fvar" in f:
        f = instancer.instantiateVariableFont(f, {"wght": weight})
    f.flavor = None
    return f


def wordmark_paths(text="Quorum", size=100.0, weight=640, tracking=-0.015):
    """Outline `text` as one SVG path. Returns (d, width, ascent, descent) in pixels at `size`."""
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen

    font = _instance_font(weight)
    gs, cmap = font.getGlyphSet(), font.getBestCmap()
    upm = font["head"].unitsPerEm
    scale = size / upm
    hhea = font["hhea"]
    x, ds = 0.0, []
    for ch in text:
        name = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[name].draw(TransformPen(pen, (scale, 0, 0, -scale, x, 0)))
        ds.append(pen.getCommands())
        x += gs[name].width * scale + tracking * size
    return " ".join(ds), x - tracking * size, hhea.ascent * scale, -hhea.descent * scale


def lockup(dark=False, height=96):
    ink, sig, word = (INDIGO_DARK, SIGNAL_DARK, INK_DARK) if dark else (INDIGO, SIGNAL, INK)
    vx, vy, vs = _bbox()
    mark_h = height
    d, w, asc, _desc = wordmark_paths(size=height * 0.62)
    gap = height * 0.22
    total_w = mark_h + gap + w
    baseline = height * 0.5 + asc * 0.36
    body = (f'<svg x="0" y="0" width="{mark_h}" height="{mark_h}" viewBox="{vx:.2f} {vy:.2f} {vs:.2f} {vs:.2f}">'
            f'{mark_body(ink, sig)}</svg>'
            f'<path transform="translate({mark_h + gap:.2f} {baseline:.2f})" d="{d}" fill="{word}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.0f} {height}" width="{total_w:.0f}" '
            f'height="{height}" role="img" aria-label="Quorum">{body}</svg>\n')


# ------------------------------------------------------------------ raster

def raster(size, ink=INDIGO, signal=SIGNAL, bg=None, radius=0.0, ss=8):
    from PIL import Image, ImageDraw

    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if bg:
        d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(radius * S), fill=bg)
    vx, vy, vs = _bbox(pad=6.0 if not bg else 16.0)
    k = S / vs

    def P(x, y):
        return (x - vx) * k, (y - vy) * k

    w = STROKE * k

    def cap(x, y):
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=ink)

    # floor: a filled annulus sector (a thick polyline would hatch), with round caps
    angs = [FLOOR[0] + (FLOOR[1] - FLOOR[0]) * i / 360 for i in range(361)]
    outer = [P(*_pt(a, R + STROKE / 2)) for a in angs]
    inner = [P(*_pt(a, R - STROKE / 2)) for a in reversed(angs)]
    d.polygon(outer + inner, fill=ink)
    cap(*P(*_pt(FLOOR[0])))
    cap(*P(*_pt(FLOOR[1])))
    # tail: a quad along the tail direction, with round caps
    (ax, ay), (bx, by) = P(*_pt(TAIL[0])), P(*_pt(*TAIL))
    nx, ny = -(by - ay), bx - ax
    n = math.hypot(nx, ny) or 1.0
    nx, ny = nx / n * w / 2, ny / n * w / 2
    d.polygon([(ax + nx, ay + ny), (bx + nx, by + ny), (bx - nx, by - ny), (ax - nx, ay - ny)], fill=ink)
    cap(ax, ay)
    cap(bx, by)
    for i, ang in enumerate(_seat_angles()):
        x, y = P(*_pt(ang))
        rr = SEAT * k
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=signal if i == SEATS - 1 else ink)
    return img.resize((size, size), Image.LANCZOS)


def social_card():
    from PIL import Image, ImageDraw, ImageFont

    W, H = 1280, 640
    img = Image.new("RGB", (W, H), PAPER_DARK)
    d = ImageDraw.Draw(img)
    for i in range(0, W, 40):  # faint grid, like graph paper
        d.line([(i, 0), (i, H)], fill="#161a23")
    for j in range(0, H, 40):
        d.line([(0, j), (W, j)], fill="#161a23")
    m = raster(300, INDIGO_DARK, SIGNAL_DARK)
    img.paste(m, (110, 170), m)
    buf = io.BytesIO()
    _instance_font(640).save(buf)
    buf.seek(0)
    title = ImageFont.truetype(buf, 132)
    d.text((450, 205), "Quorum", font=title, fill=INK_DARK)
    buf.seek(0)
    sub = ImageFont.truetype(buf, 38)
    d.text((456, 372), "Every project judged. Every tie decided.", font=sub, fill="#b9bfcc")
    d.text((456, 420), "Every team answered.", font=sub, fill="#b9bfcc")
    d.text((456, 500), "self-hosted hackathon judging  ·  runs offline  ·  Apache-2.0", font=ImageFont.truetype(
        io.BytesIO(buf.getvalue()), 24), fill="#7d8597")
    return img


def main():
    STATIC.mkdir(parents=True, exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    # themeable inline mark: colours come from CSS custom properties, with brand fallbacks
    PARTIAL.write_text(
        f'<svg class="mark" viewBox="{viewbox()}" width="28" height="28" aria-hidden="true" focusable="false">'
        + mark_body("var(--q, " + INDIGO + ")", "var(--q-signal, " + SIGNAL + ")") + "</svg>\n")
    (STATIC / "mark.svg").write_text(svg(mark_body(INDIGO, SIGNAL), extra=' role="img" aria-label="Quorum"'))
    (STATIC / "mark-mono.svg").write_text(svg(mark_body(INK, None), extra=' role="img" aria-label="Quorum"'))
    fav = (f'<style>.a{{fill:none;stroke:{INDIGO}}}.i{{fill:{INDIGO}}}.s{{fill:{SIGNAL}}}'
           f'@media (prefers-color-scheme:dark){{.a{{stroke:{INDIGO_DARK}}}.i{{fill:{INDIGO_DARK}}}.s{{fill:{SIGNAL_DARK}}}}}'
           '</style>')
    body = (mark_body("#000", "#111")  # placeholder colours, swapped for classes below
            .replace('fill="none" stroke="#000"', 'class="a"').replace('stroke="#000"', 'class="a"')
            .replace('fill="#000"', 'class="i"').replace('fill="#111"', 'class="s"'))
    (STATIC / "favicon.svg").write_text(svg(fav + body))
    for size in (16, 32, 48):
        raster(size).save(STATIC / f"favicon-{size}.png")
    raster(32).save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    raster(180, bg=PAPER, radius=0.22).convert("RGB").save(STATIC / "apple-touch-icon.png")
    for size in (192, 512):
        raster(size, bg=PAPER, radius=0.22).save(STATIC / f"icon-{size}.png")
    (DOCS / "quorum-lockup.svg").write_text(lockup(dark=False))
    (DOCS / "quorum-lockup-dark.svg").write_text(lockup(dark=True))
    (DOCS / "mark.svg").write_text(svg(mark_body(INDIGO, SIGNAL), size=256))
    social_card().save(DOCS / "social-card.png", optimize=True)
    print("brand assets written:", *sorted(p.name for p in STATIC.iterdir()), *sorted(p.name for p in DOCS.iterdir()))


if __name__ == "__main__":
    main()
