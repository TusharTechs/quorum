"""Alternative scoring methods, kept for side-by-side comparison and the proof.

* raw               -- weighted mean of the judges who reviewed the project
* raptors-classic   -- raw, then Bayesian shrinkage (n*mean + k*mu)/(n + k), k = 10: the
                       method Hackathon Raptors publishes today, shown for continuity
* zscore            -- per-judge standardisation (common, NOT recommended: undefined for
                       flat or single-review judges and biased when judges see different sets)
"""

from __future__ import annotations

import math
from collections import defaultdict

from .prepare import weighted_total


def raw_scores(reviews, weights):
    by = defaultdict(list)
    for r in reviews:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            by[r["project"]].append(t)
    return {p: sum(v) / len(v) for p, v in by.items()}


def raptors_classic(reviews, weights, k=10.0):
    by = defaultdict(list)
    for r in reviews:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            by[r["project"]].append(t)
    allv = [x for v in by.values() for x in v]
    mu = sum(allv) / len(allv) if allv else 0.0
    return {p: (len(v) * (sum(v) / len(v)) + k * mu) / (len(v) + k) for p, v in by.items()}


def _mean_sd(v):
    m = sum(v) / len(v)
    if len(v) < 2:
        return m, 0.0
    return m, math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def zscore_scores(reviews, weights):
    """Per-judge z-score, averaged per project, mapped back to the 1-5 scale. Degenerate
    judges (n=1 or sd=0) get z=0 -- itself a modelling choice ('all average')."""
    obs = []
    for r in reviews:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            obs.append((r["judge"], r["project"], t))
    byj = defaultdict(list)
    for j, p, y in obs:
        byj[j].append((p, y))
    zs = defaultdict(list)
    degenerate = []
    for j, lst in byj.items():
        centre, scale = _mean_sd([y for _, y in lst])
        if len(lst) < 2 or scale == 0:
            degenerate.append(j)
            for p, _ in lst:
                zs[p].append(0.0)
        else:
            for p, y in lst:
                zs[p].append((y - centre) / scale)
    allv = [y for _, _, y in obs]
    gm, gsd = _mean_sd(allv) if allv else (0.0, 0.0)
    return {p: gm + gsd * sum(v) / len(v) for p, v in zs.items()}, sorted(degenerate)


def kendall_tau_b(x, y):
    n = len(x)
    c = d = tx = ty = 0
    for i in range(n):
        for m in range(i + 1, n):
            dx = x[i] - x[m]
            dy = y[i] - y[m]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                tx += 1
            elif dy == 0:
                ty += 1
            elif (dx > 0) == (dy > 0):
                c += 1
            else:
                d += 1
    den = math.sqrt((c + d + tx) * (c + d + ty))
    return (c - d) / den if den else 0.0


def _avg_ranks(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(v):
        m = i
        while m + 1 < len(v) and v[order[m + 1]] == v[order[i]]:
            m += 1
        for t in range(i, m + 1):
            r[order[t]] = (i + m) / 2 + 1
        i = m + 1
    return r


def spearman(x, y):
    if len(x) < 2:
        return 0.0
    rx, ry = _avg_ranks(x), _avg_ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


def rank_agreement(scores_a, scores_b, groups=None):
    """Kendall tau-b overall and per group (track) between two score dicts."""
    common = sorted(set(scores_a) & set(scores_b))
    out = {"overall": kendall_tau_b([scores_a[p] for p in common], [scores_b[p] for p in common])}
    if groups:
        by = defaultdict(list)
        for p in common:
            by[groups[p]].append(p)
        for g, ps in sorted(by.items()):
            if len(ps) >= 2:
                out[g] = kendall_tau_b([scores_a[p] for p in ps], [scores_b[p] for p in ps])
    return out
