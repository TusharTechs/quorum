"""Cross-judge calibration: additive judge offsets with empirical-Bayes shrinkage.

Model, fitted separately for every rubric criterion c:

    y_jpc = s_pc + b_jc + e,      b ~ N(0, tau^2),  e ~ N(0, sigma^2),  k = sigma^2 / tau^2

y is judge j's score of project p, s is the project's calibrated score (same 1..5 scale),
b is the judge's leniency. Minimising

    sum (y - s - b)^2 + k * sum b^2

is exactly Henderson's mixed-model equations: s is the GLS estimate and b the BLUP. It is
solved by alternating least squares (s <- mean(y - b); b <- sum(y - s) / (n_j + k)).

Why not per-judge z-scores: judges see small, different, non-random subsets of projects,
so a judge who happened to draw weak projects *looks* harsh. This model compares each
judge only with what other judges said about the same projects, and shrinkage stops thin
evidence from producing large corrections. k is chosen by REML (projects fixed, judges
random), so when the data show no judge effect the method reduces to the raw mean.
"""

from __future__ import annotations

import math
from collections import defaultdict

from .linalg import chol_logdet, chol_solve, cholesky
from .prepare import detect_flat_judges, weighted_total


def fit_additive(obs, k, tol=1e-12, max_iter=20000):
    """ALS for y = s_p + b_j + e with ridge k on b_j.

    obs: list of (judge, project, y). Returns (score{p}, offset{j}, iterations).
    At the optimum sum_j b_j == 0 exactly (the average judge is the reference).
    k = inf gives the raw mean (all offsets 0).
    """
    by_p = defaultdict(list)
    by_j = defaultdict(list)
    for j, p, y in obs:
        by_p[p].append((j, y))
        by_j[j].append((p, y))
    b = {j: 0.0 for j in by_j}
    if k == math.inf:
        return {p: sum(y for _, y in lst) / len(lst) for p, lst in by_p.items()}, b, 0
    it = 0
    s = {}
    for it in range(1, max_iter + 1):
        s = {p: sum(y - b[j] for j, y in lst) / len(lst) for p, lst in by_p.items()}
        delta = 0.0
        for j, lst in by_j.items():
            nb = sum(y - s[p] for p, y in lst) / (len(lst) + k)
            delta = max(delta, abs(nb - b[j]))
            b[j] = nb
        if delta < tol:
            break
    s = {p: sum(y - b[j] for j, y in lst) / len(lst) for p, lst in by_p.items()}
    return s, b, it


def design(obs):
    projects = sorted({p for _, p, _ in obs})
    judges = sorted({j for j, _, _ in obs})
    pi = {p: i for i, p in enumerate(projects)}
    n_p = [0] * len(projects)
    s_p = [0.0] * len(projects)
    n_j = defaultdict(int)
    t_j = defaultdict(float)
    cols = defaultdict(list)
    yy = 0.0
    for j, p, y in obs:
        i = pi[p]
        n_p[i] += 1
        s_p[i] += y
        n_j[j] += 1
        t_j[j] += y
        cols[j].append(i)
        yy += y * y
    return projects, judges, n_p, s_p, n_j, t_j, cols, yy


def gls_pieces(obs, gamma, dsg=None):
    """A = X'H^-1 X, c = X'H^-1 y, y'H^-1 y, log|H| for H = I + gamma Z Z' (Woodbury)."""
    projects, judges, n_p, s_p, n_j, t_j, cols, yy = dsg or design(obs)
    P = len(projects)
    A = [[0.0] * P for _ in range(P)]
    for i in range(P):
        A[i][i] = float(n_p[i])
    c = list(s_p)
    yHy = yy
    logdetH = 0.0
    for j in judges:
        D = gamma / (1.0 + gamma * n_j[j])
        logdetH += math.log1p(gamma * n_j[j])
        idx = cols[j]
        for a in idx:
            c[a] -= D * t_j[j]
            Aa = A[a]
            for b2 in idx:
                Aa[b2] -= D
        yHy -= D * t_j[j] * t_j[j]
    return projects, A, c, yHy, logdetH


def reml_loglik(obs, gamma, dsg=None):
    """Restricted log-likelihood (up to a constant) for gamma = tau^2/sigma^2."""
    dsg = dsg or design(obs)
    projects, A, c, yHy, logdetH = gls_pieces(obs, gamma, dsg)
    L = cholesky(A)
    beta = chol_solve(L, c)
    q = yHy - sum(bi * ci for bi, ci in zip(beta, c))
    df = len(obs) - len(projects)
    if df <= 0:
        return float("nan"), float("nan")
    s2 = max(q / df, 1e-12)
    return -0.5 * (df * math.log(s2) + logdetH + chol_logdet(L)), s2


def estimate_k(obs, grid=None, refine=18):
    """REML estimate of k = sigma^2 / tau^2 with an approximate 95% profile interval.
    gamma = 0 (k = inf) means the data show no judge effect beyond noise."""
    dsg = design(obs)
    grid = grid or [0.0] + [10 ** (x / 8.0) for x in range(-24, 13)]
    prof = []
    for g in grid:
        ll, s2 = reml_loglik(obs, g, dsg)
        prof.append((g, ll, s2))
    best = max(prof, key=lambda t: (t[1] if t[1] == t[1] else -math.inf))
    i = prof.index(best)
    if best[0] > 0:
        lo = math.log(prof[i - 1][0]) if i - 1 >= 0 and prof[i - 1][0] > 0 else math.log(best[0]) - 0.3
        hi = math.log(prof[i + 1][0]) if i + 1 < len(prof) else math.log(best[0]) + 0.3
        gr = (math.sqrt(5) - 1) / 2
        a, b2 = lo, hi
        x1, x2 = b2 - gr * (b2 - a), a + gr * (b2 - a)
        f1 = reml_loglik(obs, math.exp(x1), dsg)[0]
        f2 = reml_loglik(obs, math.exp(x2), dsg)[0]
        for _ in range(refine):
            if f1 > f2:
                b2, x2, f2 = x2, x1, f1
                x1 = b2 - gr * (b2 - a)
                f1 = reml_loglik(obs, math.exp(x1), dsg)[0]
            else:
                a, x1, f1 = x1, x2, f2
                x2 = a + gr * (b2 - a)
                f2 = reml_loglik(obs, math.exp(x2), dsg)[0]
        g = math.exp((a + b2) / 2)
        ll, s2 = reml_loglik(obs, g, dsg)
        if ll > best[1]:
            best = (g, ll, s2)
    g, ll, s2 = best
    inside = [t[0] for t in prof if best[1] - t[1] <= 1.92]
    g_lo, g_hi = min(inside), max(inside)
    return {
        "gamma": g,
        "k": (1.0 / g) if g > 0 else math.inf,
        "sigma2": s2,
        "tau2": g * s2,
        "loglik": ll,
        "k_interval_95": ((1.0 / g_hi) if g_hi > 0 else math.inf, (1.0 / g_lo) if g_lo > 0 else math.inf),
    }


def fit_judge_effects(reviews, weights, k=None, flat_rule=True, flat_min_reviews=3,
                      k_default=4.0, k_bounds=(0.5, 1e6), exclude_judges=()):
    """The recommended calibration ('offset-reml/v1').

    reviews : [{"judge","project","criteria":{c: score}}], one per (judge, project)
    weights : {criterion: weight}; positive numbers, renormalised
    k       : shrinkage strength; None -> REML on weighted totals, clamped to k_bounds,
              falling back to k_default if REML cannot run (e.g. too few reviews)
    flat_rule: judges whose every score is identical over >= flat_min_reviews reviews get
              weight 0 for ranking (kept in the audit trail and in explanations)
    exclude_judges: judges removed entirely (used for leave-one-judge-out analysis)
    """
    weights = {c: float(w) for c, w in weights.items() if w > 0}
    crits = list(weights)
    W = sum(weights.values())
    reviews = [r for r in reviews if r["judge"] not in set(exclude_judges)]
    flat = detect_flat_judges(reviews, flat_min_reviews) if flat_rule else {}

    all_projects = sorted({r["project"] for r in reviews})
    used = [r for r in reviews if r["judge"] not in flat]
    used_projects = {r["project"] for r in used}
    orphan = [p for p in all_projects if p not in used_projects]
    used += [r for r in reviews if r["project"] in orphan]

    tot_obs = []
    for r in used:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            tot_obs.append((r["judge"], r["project"], t))
    reml = None
    k_source = "given"
    if k is None:
        try:
            reml = estimate_k(tot_obs)
            k = min(max(reml["k"], k_bounds[0]), k_bounds[1])
            k_source = "reml"
        except (ValueError, ZeroDivisionError):
            k = k_default
            k_source = "default"
    per_crit = {}
    for c in crits:
        obs = [(r["judge"], r["project"], r["criteria"][c]) for r in used if c in r["criteria"]]
        s, b, it = fit_additive(obs, k)
        per_crit[c] = {"score": s, "offset": b, "iterations": it}
    score, raw, n_rev = {}, {}, defaultdict(int)
    raw_cells = defaultdict(lambda: defaultdict(list))
    for r in reviews:
        n_rev[r["project"]] += 1
        for c in crits:
            if c in r["criteria"]:
                raw_cells[r["project"]][c].append(r["criteria"][c])
    crit_score = {}
    for p in all_projects:
        wc = [c for c in crits if p in per_crit[c]["score"]]
        ww = sum(weights[c] for c in wc)
        score[p] = sum(weights[c] * per_crit[c]["score"][p] for c in wc) / ww if ww else None
        crit_score[p] = {c: per_crit[c]["score"][p] for c in wc}
        rc = [c for c in crits if raw_cells[p][c]]
        rw = sum(weights[c] for c in rc)
        raw[p] = sum(weights[c] * sum(raw_cells[p][c]) / len(raw_cells[p][c]) for c in rc) / rw
    judges = sorted({r["judge"] for r in reviews})
    offset = {}
    for j in judges:
        vals = [(weights[c], per_crit[c]["offset"][j]) for c in crits if j in per_crit[c]["offset"]]
        offset[j] = sum(w * v for w, v in vals) / sum(w for w, _ in vals) if vals else 0.0
    n_judge = defaultdict(int)
    for r in reviews:
        n_judge[r["judge"]] += 1
    return {
        "method": "offset-reml/v1",
        "weights": {c: weights[c] / W for c in crits},
        "k": k,
        "k_source": k_source,
        "reml": reml,
        "flat_judges": flat,
        "orphan_projects": orphan,
        "score": score,
        "criterion_score": crit_score,
        "raw": raw,
        "n_reviews": dict(n_rev),
        "offset": offset,
        "n_judge": dict(n_judge),
        "per_criterion": per_crit,
        "reviews": reviews,
        "used_obs_totals": tot_obs,
    }


def rank_order(score, reverse=True):
    """Competition ranking (1,2,2,4) of a {id: value} dict, higher is better."""
    items = sorted(score.items(), key=lambda kv: (-kv[1] if reverse else kv[1], kv[0]))
    ranks = {}
    prev, prev_rank = None, 0
    for i, (p, v) in enumerate(items, 1):
        if prev is not None and abs(v - prev) < 1e-12:
            ranks[p] = prev_rank
        else:
            ranks[p] = i
            prev_rank = i
        prev = v
    return ranks


def explain_project(fit, project):
    """final = raw mean + one term per judge, exactly.

    Per criterion c:  final_pc = raw_pc + [mean_kept(y) - mean_all(y)] - mean_kept(b_jc)
    The bracket is attributed to excluded (flat) judges; each kept judge contributes
    -b_jc / n_kept. Criteria are combined with the organiser's weights.
    """
    w = fit["weights"]
    flat = fit["flat_judges"]
    orphan = project in fit["orphan_projects"]
    revs = [r for r in fit["reviews"] if r["project"] == project]
    lines = defaultdict(float)
    kind = {}
    raw = final = wsum = 0.0
    for c, wc in w.items():
        allc = [r for r in revs if c in r["criteria"]]
        if not allc:
            continue
        kept = [r for r in allc if orphan or r["judge"] not in flat]
        raw_c = sum(r["criteria"][c] for r in allc) / len(allc)
        mean_kept = sum(r["criteria"][c] for r in kept) / len(kept)
        pc = fit["per_criterion"][c]
        excl_term = mean_kept - raw_c
        excluded = [r["judge"] for r in allc if r not in kept]
        for j in excluded:
            lines[j] += wc * excl_term / len(excluded)
            kind[j] = "flat"
        for r in kept:
            j = r["judge"]
            lines[j] += wc * (-pc["offset"][j] / len(kept))
            kind.setdefault(j, None)
        raw += wc * raw_c
        final += wc * pc["score"][project]
        wsum += wc
    raw /= wsum
    final /= wsum
    out = []
    for j, v in lines.items():
        v /= wsum
        b = fit["offset"][j]
        if kind[j] == "flat":
            label = "excluded: gave %g to everything" % flat[j]["value"]
            tag = "flat"
        elif abs(b) < 0.05:
            label, tag = "near-average judge", "average"
        elif b < 0:
            label, tag = "harsher than co-judges", "harsh"
        else:
            label, tag = "more generous than co-judges", "generous"
        out.append({"judge": j, "n_reviews_by_judge": fit["n_judge"][j], "offset": b,
                    "contribution": v, "label": label, "tag": tag})
    out.sort(key=lambda d: (-abs(d["contribution"]), d["judge"]))
    total = raw + sum(d["contribution"] for d in out)
    return {"project": project, "raw_mean": raw, "lines": out, "final": final,
            "check_sum": total, "exact": abs(total - final) < 1e-9, "k": fit["k"]}
