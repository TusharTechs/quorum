"""How much should anyone trust a ranking? Standard errors, ties, prize odds, agreement.

Everything here answers a question an organizer actually asks:

* "Is #1 really better than #2?"          -> adjacent P(A > B), statistical-tie flags
* "How likely is each project to place?"  -> prize probabilities from the fit's covariance
* "Do the judges agree at all?"           -> ICC(1) with a permutation test (signal check)
* "Would one judge change the podium?"    -> leave-one-judge-out, pivotal judges
* "Do the weights decide the winner?"     -> +/-10% weight sensitivity
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from .calibrate import design, fit_judge_effects, gls_pieces
from .linalg import chol_inverse, chol_solve, cholesky, norm_cdf
from .prepare import weighted_total


def project_uncertainty(fit, z=1.96):
    """Standard errors from the GLS covariance sigma^2 (X'H^-1 X)^-1 on weighted totals
    (same estimator, same k). Adds, along the ranking, P(project beats the next one) =
    Phi(diff / se_diff) and a tie flag when |diff| < z * se_diff. Treats k as known."""
    obs = fit["used_obs_totals"]
    k = fit["k"]
    gamma = 0.0 if k == math.inf else 1.0 / k
    projects, A, c, yHy, _ = gls_pieces(obs, gamma, design(obs))
    L = cholesky(A)
    beta = chol_solve(L, c)
    q = yHy - sum(bi * ci for bi, ci in zip(beta, c))
    df = len(obs) - len(projects)
    s2 = q / df if df > 0 else float("nan")
    if not (s2 == s2) or s2 <= 0:
        s2 = 1e-6
    Ainv = chol_inverse(L)
    idx = {p: i for i, p in enumerate(projects)}
    se = {p: math.sqrt(s2 * Ainv[idx[p]][idx[p]]) for p in projects}
    eff_n = {p: 1.0 / Ainv[idx[p]][idx[p]] for p in projects}
    order = sorted(projects, key=lambda p: (-fit["score"][p], p))
    adjacent = []
    for a, b in zip(order, order[1:]):
        var = s2 * (Ainv[idx[a]][idx[a]] + Ainv[idx[b]][idx[b]] - 2 * Ainv[idx[a]][idx[b]])
        sd = math.sqrt(max(var, 1e-15))
        diff = fit["score"][a] - fit["score"][b]
        adjacent.append({"higher": a, "lower": b, "diff": diff, "se_diff": sd,
                         "p_higher_better": norm_cdf(diff / sd), "tie": abs(diff) < z * sd})
    cov = [[s2 * Ainv[idx[a]][idx[b]] for b in order] for a in order]
    return {"sigma2": s2, "se": se, "effective_n": eff_n, "adjacent": adjacent,
            "order": order, "cov": cov, "df": df}


def tie_groups(unc):
    """Maximal runs of adjacent projects linked by statistical ties -> {project: group id}.
    A group of size 1 is 'clearly separated from its neighbours'."""
    groups = {}
    gid = 0
    order = unc["order"]
    if not order:
        return groups
    groups[order[0]] = gid
    for pair in unc["adjacent"]:
        if not pair["tie"]:
            gid += 1
        groups[pair["lower"]] = gid
    return groups


def simulate_rankings(fit, unc, top_n=3, draws=4000, seed=0, tracks=None, track_places=None):
    """Model-based ('parametric') bootstrap: draw score vectors ~ N(score, Cov), rank them.

    Returns P(top_n overall), P(#1 overall), 90% rank intervals and, when `tracks` is
    given ({project: track}), P(#1 within track). With `track_places` ({track: k}, the
    places a track prize pays) it also returns P(in the top k of its track). We sample from the fit's covariance
    rather than resampling reviews because with 2 reviews per project a within-project
    resample has zero spread half of the time."""
    rng = random.Random(seed)
    projects = unc["order"]
    P = len(projects)
    if P == 0:
        return {"p_top": {}, "p_first": {}, "rank_interval": {}, "p_track_first": {}}
    C = [row[:] for row in unc["cov"]]
    for i in range(P):
        C[i][i] += 1e-12
    L = cholesky(C)
    mu = [fit["score"][p] for p in projects]
    top = [0] * P
    first = [0] * P
    ranks = [[] for _ in range(P)]
    track_first = defaultdict(int)
    track_prize = defaultdict(int)
    track_places = track_places or {}
    by_track = defaultdict(list)
    if tracks:
        for i, p in enumerate(projects):
            by_track[tracks.get(p)].append(i)
    for _ in range(draws):
        zz = [rng.gauss(0.0, 1.0) for _ in range(P)]
        x = [mu[i] + sum(L[i][m] * zz[m] for m in range(i + 1)) for i in range(P)]
        order = sorted(range(P), key=lambda i: -x[i])
        for r, i in enumerate(order, 1):
            ranks[i].append(r)
        for i in order[:top_n]:
            top[i] += 1
        first[order[0]] += 1
        for t, idxs in by_track.items():
            best = max(idxs, key=lambda i: x[i])
            track_first[best] += 1
            k = track_places.get(t)
            if k:
                for i in sorted(idxs, key=lambda i: -x[i])[:k]:
                    track_prize[i] += 1
    out_int = {}
    for i, rs in enumerate(ranks):
        rs.sort()
        out_int[projects[i]] = (rs[int(0.05 * len(rs))], rs[min(len(rs) - 1, int(0.95 * len(rs)))])
    return {
        "p_top": {projects[i]: top[i] / draws for i in range(P)},
        "p_first": {projects[i]: first[i] / draws for i in range(P)},
        "rank_interval": out_int,
        "p_track_first": {projects[i]: track_first[i] / draws for i in range(P)} if tracks else {},
        "p_track_prize": ({projects[i]: track_prize[i] / draws for i in range(P)
                           if track_places.get((tracks or {}).get(projects[i]))} if track_places else {}),
        "top_n": top_n,
        "draws": draws,
    }


def icc1(reviews, weights):
    """One-way ICC(1) of per-review weighted totals with projects as groups (unbalanced n0)."""
    groups = defaultdict(list)
    for r in reviews:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            groups[r["project"]].append(t)
    return _icc_from_groups(list(groups.values()))


def _icc_from_groups(groups):
    groups = [g for g in groups if g]
    N = sum(len(v) for v in groups)
    a = len(groups)
    if a < 2 or N - a < 1:
        return {"icc1": None, "F": None, "df": (max(a - 1, 0), max(N - a, 0)), "n0": None}
    gm = sum(sum(v) for v in groups) / N
    ssb = sum(len(v) * (sum(v) / len(v) - gm) ** 2 for v in groups)
    ssw = sum(sum((x - sum(v) / len(v)) ** 2 for x in v) for v in groups)
    msb, msw = ssb / (a - 1), ssw / (N - a)
    n0 = (N - sum(len(v) ** 2 for v in groups) / N) / (a - 1)
    if msw == 0:
        return {"icc1": 1.0 if msb > 0 else None, "F": math.inf, "msb": msb, "msw": msw,
                "n0": n0, "df": (a - 1, N - a)}
    icc = (msb - msw) / (msb + (n0 - 1) * msw)
    return {"icc1": icc, "msb": msb, "msw": msw, "n0": n0, "F": msb / msw, "df": (a - 1, N - a)}


def signal_check(reviews, weights, permutations=2000, seed=0):
    """Do the judges agree about which projects are better, beyond chance?

    Permutation test of the one-way F: shuffle review totals across projects (keeping each
    project's review count) and count how often chance alone produces an F this large.
    p > 0.05 means the panel's scores cannot be distinguished from noise, so the honest
    output is 'treat positions as ties', not a confident podium."""
    groups = defaultdict(list)
    for r in reviews:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            groups[r["project"]].append(t)
    sizes = [len(v) for v in groups.values()]
    flat_vals = [x for v in groups.values() for x in v]
    obs = _icc_from_groups(list(groups.values()))
    if obs["F"] is None:
        return {**obs, "p_value": None, "verdict": "insufficient", "permutations": 0}
    rng = random.Random(seed)
    ge = 0
    for _ in range(permutations):
        rng.shuffle(flat_vals)
        pos = 0
        gs = []
        for n in sizes:
            gs.append(flat_vals[pos:pos + n])
            pos += n
        f = _icc_from_groups(gs)["F"]
        if f is not None and f >= obs["F"] - 1e-12:
            ge += 1
    p = (ge + 1) / (permutations + 1)
    icc = obs["icc1"]
    if p > 0.05:
        verdict = "no_signal"
    elif icc is not None and icc < 0.2:
        verdict = "weak"
    elif icc is not None and icc < 0.5:
        verdict = "moderate"
    else:
        verdict = "good"
    return {**obs, "p_value": p, "verdict": verdict, "permutations": permutations}


def leave_one_judge_out(reviews, weights, k, prize_n=3, flat_rule=True):
    """Refit without each judge (k held fixed for stability). Returns, per judge, the
    largest rank move they cause and whether removing them changes the prize set."""
    base = fit_judge_effects(reviews, weights, k=k, flat_rule=flat_rule)
    base_rank = sorted(base["score"], key=lambda p: (-base["score"][p], p))
    base_top = set(base_rank[:prize_n])
    pos = {p: i for i, p in enumerate(base_rank)}
    out = {}
    for j in sorted({r["judge"] for r in reviews}):
        f = fit_judge_effects(reviews, weights, k=k, flat_rule=flat_rule, exclude_judges=[j])
        order = sorted(f["score"], key=lambda p: (-f["score"][p], p))
        moves = [abs(i - pos[p]) for i, p in enumerate(order) if p in pos]
        top = set(order[:prize_n])
        out[j] = {"max_move": max(moves) if moves else 0, "prize_set_changes": top != base_top,
                  "top1_changes": bool(order) and bool(base_rank) and order[0] != base_rank[0]}
    return out


def weight_sensitivity(reviews, weights, k, prize_n=3, factor=0.10, flat_rule=True):
    """Scale each weight by (1 +/- factor), renormalise, refit: does any prize position change?"""
    base = fit_judge_effects(reviews, weights, k=k, flat_rule=flat_rule)
    base_rank = sorted(base["score"], key=lambda p: (-base["score"][p], p))[:prize_n]
    results = []
    for c in weights:
        for sign in (-1, 1):
            w2 = dict(weights)
            w2[c] = weights[c] * (1 + sign * factor)
            f = fit_judge_effects(reviews, w2, k=k, flat_rule=flat_rule)
            order = sorted(f["score"], key=lambda p: (-f["score"][p], p))[:prize_n]
            results.append({"criterion": c, "change": sign * factor, "podium": order,
                            "podium_changed": order != base_rank})
    return {"base_podium": base_rank, "perturbations": results,
            "stable": not any(r["podium_changed"] for r in results)}
