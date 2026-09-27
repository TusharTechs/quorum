"""Template helpers: number formatting and small server-rendered SVG charts (no JS chart
library, works offline, prints, and stays accessible with <title>/<desc>)."""

from __future__ import annotations

from django import template
from django.templatetags.static import static
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def icon(name, size=16, cls="", label=""):
    """A Lucide icon from the vendored sprite. Decorative unless `label` is given."""
    a11y = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"'
    return mark_safe(f'<svg class="icon {escape(cls)}" width="{int(size)}" height="{int(size)}" {a11y} focusable="false">'
                     f'<use href="{static("icons/sprite.svg")}#i-{escape(name)}"></use></svg>')


@register.filter
def f2(v):
    return "–" if v is None or v == "" else f"{float(v):.2f}"


@register.filter
def f3(v):
    return "–" if v is None or v == "" else f"{float(v):.3f}"


@register.filter
def pct(v):
    return "–" if v is None or v == "" else f"{100 * float(v):.0f}%"


@register.filter
def signed(v):
    return "–" if v is None else f"{float(v):+.2f}"


@register.filter
def get(d, k):
    try:
        return d.get(k) if hasattr(d, "get") else d[k]
    except (KeyError, IndexError, TypeError):
        return None


@register.filter
def short(h, n=12):
    return (h or "")[: int(n)]


@register.simple_tag
def whisker(lo, hi, rank, n):
    """Rank-interval bar: where a project could plausibly rank (90%)."""
    if lo is None or hi is None or not n:
        return ""
    w, pad = 120, 6
    x = lambda r: pad + (w - 2 * pad) * (r - 1) / max(n - 1, 1)
    return mark_safe(
        f'<svg class="whisker" viewBox="0 0 {w} 16" role="img" aria-label="90% rank interval {lo} to {hi}">'
        f'<title>Plausible ranks {lo}–{hi} (90%)</title>'
        f'<line class="track" x1="{pad}" y1="8" x2="{w - pad}" y2="8"/>'
        f'<line class="range" x1="{x(lo):.1f}" y1="8" x2="{x(hi):.1f}" y2="8"/>'
        f'<circle class="pt" cx="{x(rank):.1f}" cy="8" r="4"/></svg>'
    )


@register.simple_tag
def bar(value, total, kind=""):
    frac = 0 if not total else max(0.0, min(1.0, float(value) / float(total)))
    return mark_safe(f'<div class="bar {escape(kind)}" role="progressbar" aria-valuenow="{value}" aria-valuemin="0" '
                     f'aria-valuemax="{total}"><span style="width:{frac * 100:.1f}%"></span></div>')


@register.simple_tag
def waterfall(expl, names=None):
    """Explanation waterfall: raw mean -> one bar per judge -> final score (exact sum)."""
    if not expl:
        return ""
    names = names or {}
    lines = expl["lines"]
    rows = [("Raw mean", expl["raw_mean"], "base", None)] + \
           [(names.get(l["judge"], l["judge"]) + " · " + l["label"], l["contribution"],
             "excl" if l["tag"] == "flat" else ("pos" if l["contribution"] >= 0 else "neg"), l) for l in lines] + \
           [("Calibrated score", expl["final"], "base", None)]
    vals = [expl["raw_mean"], expl["final"]]
    run = expl["raw_mean"]
    for l in lines:
        vals += [run, run + l["contribution"]]
        run += l["contribution"]
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 0.25)
    lo, hi = lo - span * 0.15, hi + span * 0.15
    W, left, right, rh = 640, 250, 70, 26
    H = rh * len(rows) + 10
    sx = lambda v: left + (W - left - right) * (v - lo) / (hi - lo)
    out = [f'<svg class="wf" viewBox="0 0 {W} {H}" role="img" aria-label="How the score was calibrated">',
           '<title>Score explanation: raw mean, each judge correction, calibrated score</title>']
    run = expl["raw_mean"]
    for i, (label, v, kind, l) in enumerate(rows):
        y = 5 + i * rh
        if kind == "base":
            x0, x1 = sx(lo + (hi - lo) * 0.0), sx(v)
            x0 = sx(max(lo, min(vals) - span * 0.1))
            out.append(f'<rect class="base" x="{x0:.1f}" y="{y + 4}" width="{max(x1 - x0, 1):.1f}" height="{rh - 10}" rx="3" opacity=".85"/>')
            out.append(f'<text x="{x1 + 6:.1f}" y="{y + rh / 2 + 4}">{v:.3f}</text>')
        else:
            a, b = run, run + v
            x0, x1 = sorted((sx(a), sx(b)))
            out.append(f'<rect class="{kind}" x="{x0:.1f}" y="{y + 4}" width="{max(x1 - x0, 1.5):.1f}" height="{rh - 10}" rx="3"/>')
            out.append(f'<text x="{x1 + 6:.1f}" y="{y + rh / 2 + 4}">{v:+.3f}</text>')
            run = b
        out.append(f'<text class="lbl" x="8" y="{y + rh / 2 + 4}">{escape(label[:44])}</text>')
    out.append("</svg>")
    return mark_safe("".join(out))


@register.simple_tag
def burndown(points, total, due_frac=None):
    """Remaining reviews over time (points: list of (t in 0..1, remaining)) vs the ideal line."""
    if not points or not total:
        return ""
    W, H, p = 640, 180, 28
    sx = lambda t: p + (W - 2 * p) * t
    sy = lambda r: H - p - (H - 2 * p) * (r / total)
    path = " ".join(f"{'M' if i == 0 else 'L'}{sx(t):.1f},{sy(r):.1f}" for i, (t, r) in enumerate(points))
    last_t, last_r = points[-1]
    out = [f'<svg viewBox="0 0 {W} {H}" style="width:100%" role="img" aria-label="Judging burn-down">',
           "<title>Remaining reviews over the judging window versus the ideal pace</title>",
           f'<line x1="{sx(0)}" y1="{sy(total)}" x2="{sx(1)}" y2="{sy(0)}" stroke="var(--line-strong)" stroke-dasharray="4 4"/>',
           f'<line x1="{p}" y1="{H - p}" x2="{W - p}" y2="{H - p}" stroke="var(--line)"/>',
           f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="2.5"/>',
           f'<circle cx="{sx(last_t):.1f}" cy="{sy(last_r):.1f}" r="4" fill="var(--accent)"/>',
           f'<text x="{sx(last_t) + 6:.1f}" y="{sy(last_r) - 6:.1f}" font-size="12" fill="var(--ink-2)">{last_r} left</text>',
           f'<text x="{p}" y="{H - 8}" font-size="11" fill="var(--muted)">window opens</text>',
           f'<text x="{W - p}" y="{H - 8}" font-size="11" fill="var(--muted)" text-anchor="end">due</text>',
           "</svg>"]
    return mark_safe("".join(out))


@register.filter
def hue(text):
    """A stable hue (0-359) per project title, for generated thumbnails."""
    import hashlib

    return int(hashlib.sha256((text or "").encode()).hexdigest()[:4], 16) % 360


GLOSSARY = {
    "icc": ("How much the judges agree, from −1 to 1. Around 0 means their scores are no more alike than random "
            "numbers; above 0.3 is typical of a panel that can rank projects.", "ICC(1) with a permutation test"),
    "se": ("The score's margin of error. With other judges drawing this project, its score would usually land "
           "within about two of these either side.", "GLS standard error of the calibrated score"),
    "rank_interval": ("Where the project could plausibly finish, given how much the judges disagree: the range "
                      "covers 90% of 4,000 simulated re-rankings.", "model-based bootstrap"),
    "p_prize": ("How often the project wins a prize across 4,000 simulations of the uncertainty. 50% means the "
                "scores genuinely cannot say.", "multivariate normal draws from the fit"),
    "calibrated": ("The score after evening out harsh and generous judges: each judge's lean is measured on "
                   "projects other judges also saw, and shrunk when the evidence is thin.", "additive offsets, REML"),
    "tie": ("Two projects whose difference is smaller than the measurement noise. Quorum does not pretend to "
            "order them; a tie at a prize goes to a head-to-head round.", "|Δ| < 1.96 × SE of the difference"),
    "coverage": ("Projects that have reached the review target (or have reviews still pending). Below target "
                 "means fewer judges than promised have looked at it.", "reviews_per_project in the method"),
    "flat": ("A judge who gave every project the same scores. Those scores cannot separate projects, so they get "
             "weight zero, visibly, and the projects get replacement reviews.", "flat-judge rule, ≥3 reviews"),
    "tau": ("How similar two rankings are, from −1 (reversed) to 1 (identical). 0 means unrelated.",
            "Kendall τ-b"),
    "method": ("The judging method (criteria, weights, calibration, tie rule) fixed before registration opened. "
               "Its fingerprint is public, so no one can change the rules after seeing the scores.",
               "SHA-256 of the canonical method spec"),
    "audit": ("The latest entry in the tamper-evident log. Every entry includes the fingerprint of the one "
              "before it, so editing history breaks the chain.", "SHA-256 hash chain, signed checkpoints"),
}


@register.simple_tag
def gl(key):
    """A '?' button that explains a statistic in plain words (see app.js)."""
    text, src = GLOSSARY.get(key, ("", ""))
    if not text:
        return ""
    return mark_safe(f'<button type="button" class="gl" data-gl="{escape(text)}" data-src="{escape(src)}" '
                     f'aria-label="What does this mean?" aria-expanded="false">?</button>')
