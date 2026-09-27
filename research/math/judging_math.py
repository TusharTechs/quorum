"""
judging_math.py -- scoring maths for the DOGFOOD judging engine.

Pure Python 3 standard library. No numpy, no scipy. Every public function takes
plain lists/dicts and returns plain lists/dicts so it can be lifted into a web
backend as-is.

Public entry points
-------------------
  prepare_reviews(fixture, weights, duplicate_policy)   -> cleaned review list + audit log
  fit_judge_effects(reviews, weights, k=None, ...)      -> the recommended normalisation
  explain_project(fit, project_id)                      -> exact per-judge decomposition
  project_uncertainty(fit)                              -> SE, P(A beats next), tie flags
  bt_fit(comparisons, ...)                              -> Bradley-Terry / Crowd-BT (MM / EM)
  next_pair(judge, ...)                                 -> pairwise pair selection
  assign(projects, judges, k, capacity, conflicts, seed)-> overlap-maximising assignment
  design_diagnostics(reviews, projects, judges)         -> graph / identifiability report

Model (the one we recommend, "additive judge offsets with empirical-Bayes shrinkage")
------------------------------------------------------------------------------------
    y_jp = s_p + b_j + e_jp,     b_j ~ N(0, tau^2),   e_jp ~ N(0, sigma^2)

y_jp is judge j's score of project p for one criterion (1..5), s_p is the
project's calibrated score on the same 1..5 scale, b_j is the judge's leniency.
The penalised least-squares problem

    minimise  sum (y_jp - s_p - b_j)^2  +  k * sum_j b_j^2,      k = sigma^2 / tau^2

is exactly Henderson's mixed-model equations, so s_p is the GLS/BLUE estimate
and b_j is the BLUP. It is solved by alternating least squares:

    s_p <- mean_{j in p} (y_jp - b_j)
    b_j <- sum_{p in j} (y_jp - s_p) / (n_j + k)

k is estimated by REML (projects fixed, judges random) unless given.
Because the estimator is linear in y for fixed k, fitting each criterion and then
weighting equals weighting and then fitting; we fit per criterion so a review
with a missing criterion is simply a missing cell, never an imputed one.
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict

DEFAULT_CRITERIA = ("functionality", "quality", "innovation")
EQUAL_WEIGHTS = {"functionality": 1.0, "quality": 1.0, "innovation": 1.0}

# =============================================================================
# 0. Small dense linear algebra (Cholesky) -- enough for 40x40 systems
# =============================================================================


def cholesky(a):
    """Lower-triangular L with L L^T = a. a is a list of lists (SPD)."""
    n = len(a)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        Li = L[i]
        for j in range(i + 1):
            Lj = L[j]
            s = a[i][j]
            for m in range(j):
                s -= Li[m] * Lj[m]
            if i == j:
                if s <= 0.0:
                    raise ValueError("matrix not positive definite")
                Li[i] = math.sqrt(s)
            else:
                Li[j] = s / Lj[j]
    return L


def chol_solve(L, b):
    n = len(L)
    z = [0.0] * n
    for i in range(n):
        s = b[i]
        Li = L[i]
        for m in range(i):
            s -= Li[m] * z[m]
        z[i] = s / Li[i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = z[i]
        for m in range(i + 1, n):
            s -= L[m][i] * x[m]
        x[i] = s / L[i][i]
    return x


def chol_inverse(L):
    n = len(L)
    cols = []
    for i in range(n):
        e = [0.0] * n
        e[i] = 1.0
        cols.append(chol_solve(L, e))
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def chol_logdet(L):
    return 2.0 * sum(math.log(L[i][i]) for i in range(len(L)))


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# =============================================================================
# 1. Data preparation: duplicates, flat judges, weighted totals
# =============================================================================


def load_fixture(path):
    with open(path) as fh:
        return json.load(fh)


def _norm_repo(url):
    return (url or "").strip().lower().rstrip("/").removesuffix(".git")


def find_duplicates(projects, submissions_close=None):
    """Group projects that are the same submission: same team AND (same repo OR same title).

    Canonical = latest submission at or before the deadline (the team's final word);
    if none is on time, the latest overall. Returns list of {canonical, superseded, reason}.
    """
    by_team = defaultdict(list)
    for p in projects:
        by_team[p.get("team")].append(p)
    seen = set()
    out = []
    for team, plist in by_team.items():
        if len(plist) < 2:
            continue
        # union projects of this team that share repo or title
        parent = {p["id"]: p["id"] for p in plist}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a in plist:
            for b in plist:
                if a["id"] < b["id"] and (
                    _norm_repo(a.get("repo_url")) == _norm_repo(b.get("repo_url"))
                    or (a.get("title") or "").strip().lower() == (b.get("title") or "").strip().lower()
                ):
                    parent[find(a["id"])] = find(b["id"])
        comps = defaultdict(list)
        for p in plist:
            comps[find(p["id"])].append(p)
        for comp in comps.values():
            if len(comp) < 2:
                continue
            on_time = [p for p in comp if submissions_close is None or p["submitted_at"] <= submissions_close]
            pool = on_time or comp
            canon = max(pool, key=lambda p: p["submitted_at"])
            key = tuple(sorted(p["id"] for p in comp))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "canonical": canon["id"],
                "superseded": sorted(p["id"] for p in comp if p["id"] != canon["id"]),
                "reason": "same team and same repo/title",
            })
    return out


def prepare_reviews(fixture, criteria=DEFAULT_CRITERIA, duplicate_policy="merge"):
    """Return (projects, reviews, log).

    reviews: list of {"judge","project","criteria":{c: float}} with at most one review
    per (judge, project).
    duplicate_policy:
      "merge"       -- superseded copies' reviews are re-pointed at the canonical copy;
                       if one judge reviewed both copies, their scores are averaged
                       criterion-by-criterion into ONE observation (no double counting).
      "keep_latest" -- superseded copies and their reviews are dropped.
    """
    close = fixture.get("event", {}).get("submissions_close")
    projects = {p["id"]: dict(p) for p in fixture["projects"]}
    dups = find_duplicates(fixture["projects"], close)
    remap = {}
    log = []
    for g in dups:
        for s in g["superseded"]:
            remap[s] = g["canonical"]
            projects.pop(s, None)
        log.append({"event": "duplicate", **g, "policy": duplicate_policy})
    cells = defaultdict(list)
    dropped = 0
    for s in fixture["scores"]:
        p = s["project"]
        if p in remap:
            if duplicate_policy == "keep_latest":
                dropped += 1
                continue
            p = remap[p]
        crit = {c: float(v) for c, v in (s.get("criteria") or {}).items()
                if c in criteria and v is not None}
        if not crit:
            dropped += 1
            continue
        cells[(s["judge"], p)].append(crit)
    reviews = []
    retest = []
    for (j, p), lst in sorted(cells.items()):
        if len(lst) > 1:
            retest.append({"judge": j, "project": p, "scores": lst})
        merged = {}
        for c in criteria:
            vals = [d[c] for d in lst if c in d]
            if vals:
                merged[c] = sum(vals) / len(vals)
        reviews.append({"judge": j, "project": p, "criteria": merged, "n_merged": len(lst)})
    if retest:
        log.append({"event": "same_judge_scored_both_copies", "cases": retest})
    if dropped:
        log.append({"event": "reviews_dropped", "count": dropped})
    return list(projects.values()), reviews, log


def weighted_total(crit, weights):
    """Weighted mean over the criteria present (weights renormalised). None if nothing present."""
    present = [c for c in weights if c in crit]
    if not present:
        return None
    w = sum(weights[c] for c in present)
    return sum(weights[c] * crit[c] for c in present) / w


def detect_flat_judges(reviews, min_reviews=3):
    """A judge is 'flat' if every criterion score on every review is the same value and
    they did at least `min_reviews` reviews. Such a judge carries no information about
    which project is better (their discrimination slope is exactly 0), so the model gives
    their reviews weight 0 for ranking. Their scores stay in the audit trail."""
    vals = defaultdict(list)
    count = defaultdict(int)
    for r in reviews:
        vals[r["judge"]].extend(r["criteria"].values())
        count[r["judge"]] += 1
    flat = {}
    for j, v in vals.items():
        if count[j] >= min_reviews and len(set(v)) == 1:
            flat[j] = {"value": v[0], "n_reviews": count[j], "n_values": len(v)}
    return flat


# =============================================================================
# 2. Core estimators
# =============================================================================


def fit_additive(obs, k, tol=1e-12, max_iter=20000):
    """Alternating least squares for y = s_p + b_j + e with ridge k on b_j.

    obs: list of (judge, project, y). Returns (score{p}, offset{j}, iterations).
    At the optimum sum_j b_j == 0 exactly (the 'average judge' is the reference).
    k = math.inf gives the raw mean (all offsets 0).
    """
    by_p = defaultdict(list)
    by_j = defaultdict(list)
    for j, p, y in obs:
        by_p[p].append((j, y))
        by_j[j].append((p, y))
    b = {j: 0.0 for j in by_j}
    s = {}
    if k == math.inf:
        s = {p: sum(y for _, y in lst) / len(lst) for p, lst in by_p.items()}
        return s, b, 0
    it = 0
    for it in range(1, max_iter + 1):
        s = {p: sum(y - b[j] for j, y in lst) / len(lst) for p, lst in by_p.items()}
        delta = 0.0
        for j, lst in by_j.items():
            nb = sum(y - s[p] for p, y in lst) / (len(lst) + k)
            d = abs(nb - b[j])
            if d > delta:
                delta = d
            b[j] = nb
        if delta < tol:
            break
    s = {p: sum(y - b[j] for j, y in lst) / len(lst) for p, lst in by_p.items()}
    return s, b, it


def _design(obs):
    projects = sorted({p for _, p, _ in obs})
    judges = sorted({j for j, _, _ in obs})
    pi = {p: i for i, p in enumerate(projects)}
    n_p = [0] * len(projects)
    s_p = [0.0] * len(projects)
    n_j = defaultdict(int)
    t_j = defaultdict(float)
    cols = defaultdict(list)  # judge -> list of project indices
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


def _gls_pieces(obs, gamma, design=None):
    """A = X'H^-1 X, c = X'H^-1 y, y'H^-1 y for H = I + gamma Z Z' (Woodbury, Z'Z diagonal)."""
    projects, judges, n_p, s_p, n_j, t_j, cols, yy = design or _design(obs)
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


def reml_loglik(obs, gamma, design=None):
    """Restricted log-likelihood (up to a constant) for gamma = tau^2 / sigma^2,
    projects as fixed effects, judges random. Returns (loglik, sigma2)."""
    design = design or _design(obs)
    projects, A, c, yHy, logdetH = _gls_pieces(obs, gamma, design)
    L = cholesky(A)
    beta = chol_solve(L, c)
    q = yHy - sum(bi * ci for bi, ci in zip(beta, c))
    df = len(obs) - len(projects)
    if df <= 0:
        return float("nan"), float("nan")
    s2 = max(q / df, 1e-12)
    ll = -0.5 * (df * math.log(s2) + logdetH + chol_logdet(L))
    return ll, s2


def estimate_k(obs, grid=None, refine=18):
    """REML estimate of k = sigma^2/tau^2. Returns dict with k, gamma, sigma2, tau2,
    loglik at optimum, and an approximate 95% profile interval for k.
    gamma = 0 (k = inf) means the data show no judge effect beyond noise."""
    design = _design(obs)
    grid = grid or [0.0] + [10 ** (x / 8.0) for x in range(-24, 13)]  # 0, 1e-3 .. 31.6
    prof = []
    for g in grid:
        ll, s2 = reml_loglik(obs, g, design)
        prof.append((g, ll, s2))
    best = max(prof, key=lambda t: t[1])
    i = prof.index(best)
    # golden-section refine on log(gamma) between neighbours (skip if at gamma=0 boundary)
    if best[0] > 0:
        lo = math.log(prof[i - 1][0]) if i - 1 >= 0 and prof[i - 1][0] > 0 else math.log(best[0]) - 0.3
        hi = math.log(prof[i + 1][0]) if i + 1 < len(prof) else math.log(best[0]) + 0.3
        gr = (math.sqrt(5) - 1) / 2
        a, b2 = lo, hi
        x1, x2 = b2 - gr * (b2 - a), a + gr * (b2 - a)
        f1 = reml_loglik(obs, math.exp(x1), design)[0]
        f2 = reml_loglik(obs, math.exp(x2), design)[0]
        for _ in range(refine):
            if f1 > f2:
                b2, x2, f2 = x2, x1, f1
                x1 = b2 - gr * (b2 - a)
                f1 = reml_loglik(obs, math.exp(x1), design)[0]
            else:
                a, x1, f1 = x1, x2, f2
                x2 = a + gr * (b2 - a)
                f2 = reml_loglik(obs, math.exp(x2), design)[0]
        g = math.exp((a + b2) / 2)
        ll, s2 = reml_loglik(obs, g, design)
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
        "profile": prof,
    }


def fit_judge_effects(reviews, weights=None, k=None, flat_rule=True, flat_min_reviews=3,
                      k_default=4.0, k_bounds=(0.5, 1e6)):
    """The recommended normalisation.

    reviews : list of {"judge","project","criteria":{c: score}}  (one per judge/project)
    weights : {criterion: weight}; any positive numbers, renormalised.
    k       : ridge / shrinkage strength. None -> REML estimate on the weighted totals,
              clamped to k_bounds; falls back to k_default if REML cannot run.
    flat_rule: judges whose every score is identical (>= flat_min_reviews reviews) get
              weight 0 for ranking.

    Returns a dict (the 'fit') consumed by explain_project / project_uncertainty.
    """
    weights = dict(weights or EQUAL_WEIGHTS)
    crits = [c for c in weights if weights[c] > 0]
    W = sum(weights[c] for c in crits)
    flat = detect_flat_judges(reviews, flat_min_reviews) if flat_rule else {}

    all_projects = sorted({r["project"] for r in reviews})
    used = [r for r in reviews if r["judge"] not in flat]
    used_projects = {r["project"] for r in used}
    # a project reviewed ONLY by flat judges keeps those reviews (and is flagged)
    orphan = [p for p in all_projects if p not in used_projects]
    used += [r for r in reviews if r["project"] in orphan]

    # --- k from REML on weighted totals of the reviews that enter the model
    tot_obs = []
    for r in used:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            tot_obs.append((r["judge"], r["project"], t))
    reml = None
    if k is None:
        try:
            reml = estimate_k(tot_obs)
            k = min(max(reml["k"], k_bounds[0]), k_bounds[1])
        except (ValueError, ZeroDivisionError):
            k = k_default
    # --- per-criterion fits (missing criteria = missing cells)
    per_crit = {}
    for c in crits:
        obs = [(r["judge"], r["project"], r["criteria"][c]) for r in used if c in r["criteria"]]
        s, b, it = fit_additive(obs, k)
        n_pc = defaultdict(int)
        for _, p, _ in obs:
            n_pc[p] += 1
        per_crit[c] = {"score": s, "offset": b, "iterations": it, "n": dict(n_pc)}
    # --- combine
    score, raw, n_rev = {}, {}, defaultdict(int)
    raw_cells = defaultdict(lambda: defaultdict(list))
    for r in reviews:
        n_rev[r["project"]] += 1
        for c in crits:
            if c in r["criteria"]:
                raw_cells[r["project"]][c].append(r["criteria"][c])
    for p in all_projects:
        wc = [c for c in crits if p in per_crit[c]["score"]]
        ww = sum(weights[c] for c in wc)
        score[p] = sum(weights[c] * per_crit[c]["score"][p] for c in wc) / ww if ww else None
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
        "method": "additive judge offsets, ridge k (empirical Bayes)",
        "weights": {c: weights[c] / W for c in crits},
        "k": k,
        "reml": {kk: v for kk, v in (reml or {}).items() if kk != "profile"} if reml else None,
        "flat_judges": flat,
        "orphan_projects": orphan,
        "score": score,
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


# =============================================================================
# 3. Per-project explanation (exact decomposition)
# =============================================================================


def explain_project(fit, project):
    """final = raw mean + sum of per-judge terms, exactly.

    For each criterion c:  final_pc = mean_{kept j}(y_jpc - b_jc)
                                    = raw_pc + [mean_kept(y) - mean_all(y)] - mean_kept(b_jc)
    The bracket is attributed to excluded (flat) judges ("exclusion" term); each kept
    judge contributes -b_jc / n_kept_pc. Criteria are combined with the organiser's weights.
    """
    w = fit["weights"]
    flat = fit["flat_judges"]
    orphan = project in fit["orphan_projects"]
    revs = [r for r in fit["reviews"] if r["project"] == project]
    lines = defaultdict(float)
    kind = {}
    raw = 0.0
    final = 0.0
    wsum = 0.0
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
            kind[j] = "excluded: flat scorer (gave %s to everything)" % flat[j]["value"]
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
        if kind[j]:
            label = kind[j]
        elif abs(b) < 0.05:
            label = "near-average judge"
        else:
            label = "harsh judge" if b < 0 else "generous judge"
        out.append({"judge": j, "n_reviews_by_judge": fit["n_judge"][j], "offset": b,
                    "contribution": v, "label": label})
    out.sort(key=lambda d: -abs(d["contribution"]))
    total = raw + sum(d["contribution"] for d in out)
    return {"project": project, "raw_mean": raw, "lines": out, "final": final,
            "check_sum": total, "exact": abs(total - final) < 1e-9, "k": fit["k"]}


def format_explanation(e, names=None):
    names = names or {}
    parts = ["%s: raw %.2f" % (names.get(e["project"], e["project"]), e["raw_mean"])]
    for d in e["lines"]:
        parts.append("%s (%s, n=%d) %+.2f" % (names.get(d["judge"], d["judge"]), d["label"],
                                              d["n_reviews_by_judge"], d["contribution"]))
    parts.append("final %.2f" % e["final"])
    return ", ".join(parts)


# =============================================================================
# 4. Uncertainty and statistical ties
# =============================================================================


def project_uncertainty(fit, z=1.96):
    """Standard errors from the GLS covariance sigma^2 (X'H^-1X)^-1 on the weighted totals
    (the same estimator, the same k). Adds, for the ranking, P(project beats the next one)
    = Phi(diff / se_diff) and a tie flag when |diff| < z * se_diff.

    Caveat: treats k as known (ignores uncertainty in the variance components)."""
    obs = fit["used_obs_totals"]
    k = fit["k"]
    gamma = 0.0 if k == math.inf else 1.0 / k
    design = _design(obs)
    projects, A, c, yHy, logdetH = _gls_pieces(obs, gamma, design)
    L = cholesky(A)
    beta = chol_solve(L, c)
    q = yHy - sum(bi * ci for bi, ci in zip(beta, c))
    df = len(obs) - len(projects)
    s2 = q / df
    Ainv = chol_inverse(L)
    idx = {p: i for i, p in enumerate(projects)}
    se = {p: math.sqrt(s2 * Ainv[idx[p]][idx[p]]) for p in projects}
    eff_n = {p: 1.0 / Ainv[idx[p]][idx[p]] for p in projects}
    order = sorted(projects, key=lambda p: -fit["score"][p])
    adj = []
    for a, b in zip(order, order[1:]):
        var = s2 * (Ainv[idx[a]][idx[a]] + Ainv[idx[b]][idx[b]] - 2 * Ainv[idx[a]][idx[b]])
        sd = math.sqrt(max(var, 1e-15))
        diff = fit["score"][a] - fit["score"][b]
        adj.append({"higher": a, "lower": b, "diff": diff, "se_diff": sd,
                    "p_higher_better": norm_cdf(diff / sd), "tie": abs(diff) < z * sd})
    return {"sigma2": s2, "se": se, "effective_n": eff_n, "adjacent": adj,
            "cov": lambda a, b: s2 * Ainv[idx[a]][idx[b]], "order": order}


def rank_intervals_from_cov(fit, unc, draws=4000, seed=1, coverage=0.9, subset=None):
    """Posterior-style rank intervals: draw scores ~ N(score, Cov) and rank each draw."""
    rng = random.Random(seed)
    projects = subset or unc["order"]
    P = len(projects)
    C = [[unc["cov"](a, b) for b in projects] for a in projects]
    L = cholesky(C)
    ranks = {p: [] for p in projects}
    mu = [fit["score"][p] for p in projects]
    for _ in range(draws):
        zz = [rng.gauss(0, 1) for _ in range(P)]
        x = [mu[i] + sum(L[i][m] * zz[m] for m in range(i + 1)) for i in range(P)]
        order = sorted(range(P), key=lambda i: -x[i])
        for r, i in enumerate(order, 1):
            ranks[projects[i]].append(r)
    lo_q, hi_q = (1 - coverage) / 2, 1 - (1 - coverage) / 2
    out = {}
    for p, rs in ranks.items():
        rs.sort()
        out[p] = (rs[int(lo_q * len(rs))], rs[min(len(rs) - 1, int(hi_q * len(rs)))],
                  sum(1 for r in rs if r == 1) / len(rs))
    return out


def prize_probabilities(fit, unc, top_n=3, draws=2000, seed=0):
    """Model-based ('parametric') bootstrap of the ranking: draw score vectors from
    N(score, sigma^2 (X'H^-1X)^-1) and count how often each project lands in the top_n
    and at #1. We use this rather than resampling reviews because with 2 reviews per
    project a within-project resample has zero spread half of the time."""
    rng = random.Random(seed)
    projects = unc["order"]
    P = len(projects)
    C = [[unc["cov"](a, b) for b in projects] for a in projects]
    for i in range(P):
        C[i][i] += 1e-12
    L = cholesky(C)
    mu = [fit["score"][p] for p in projects]
    top = [0] * P
    first = [0] * P
    for _ in range(draws):
        z = [rng.gauss(0.0, 1.0) for _ in range(P)]
        x = [mu[i] + sum(L[i][m] * z[m] for m in range(i + 1)) for i in range(P)]
        order = sorted(range(P), key=lambda i: -x[i])
        for i in order[:top_n]:
            top[i] += 1
        first[order[0]] += 1
    return {"p_top": {projects[i]: top[i] / draws for i in range(P)},
            "p_first": {projects[i]: first[i] / draws for i in range(P)},
            "se": unc["se"], "sigma2": unc["sigma2"], "top_n": top_n}


def plan_next_round(fit, bootstrap, budget, judges, capacity, project_track=None,
                    existing=None, conflicts=(), max_per_project=2, seed=0):
    """Adaptive review allocation: spend `budget` extra reviews where they most reduce
    uncertainty about who wins a prize.

    fit       : output of fit_judge_effects
    bootstrap : output of prize_probabilities (p_top, se, sigma2)
    budget    : number of (judge, project) reviews to plan
    judges    : list of {"id","tracks"}  (tracks None = any)
    capacity  : {judge: remaining review slots}
    project_track: {project: track} for eligibility (None = no track constraint)
    existing  : set of (judge, project) already reviewed/assigned (default: from fit)

    Greedy on expected value of one more review:
        gain_p = P_p (1 - P_p) * v_p^2 / (v_p + sigma^2)
    P_p = P(project in prize set); v_p = current variance of its score. The second factor
    is the variance reduction one more review buys (diminishing: v_p is updated to
    1/(1/v_p + 1/sigma^2) after each planned review). The judge for a slot is an eligible,
    non-flat judge with spare capacity who has not seen the project, preferring judges
    whose own offset is best estimated (most information), so the new score needs the
    least correction. Returns list of (judge, project).
    """
    rng = random.Random(seed)
    p_top = bootstrap["p_top"]
    s2 = bootstrap["sigma2"]
    v = {p: bootstrap["se"][p] ** 2 for p in p_top}
    existing = set(existing) if existing is not None else {(r["judge"], r["project"]) for r in fit["reviews"]}
    conflicts = set(conflicts)
    flat = set(fit.get("flat_judges", {}))
    by_j = defaultdict(set)
    by_p = defaultdict(set)
    for j, p in existing:
        by_j[j].add(p)
        by_p[p].add(j)
    info = {j: len(ps) - sum(1.0 / max(1, len(by_p[p])) for p in ps) for j, ps in by_j.items()}
    tracks_of = {j["id"]: (set(j["tracks"]) if j.get("tracks") else None) for j in judges}
    cap = dict(capacity)
    extra = defaultdict(int)
    dead = set()
    plan = []
    while len(plan) < budget:
        cands = []
        for p, P in p_top.items():
            if p in dead or extra[p] >= max_per_project:
                continue
            g = P * (1 - P) * v[p] ** 2 / (v[p] + s2)
            if g > 0:
                cands.append((g + 1e-12 * rng.random(), p))
        if not cands:
            break
        cands.sort(reverse=True)
        placed = False
        for _, p in cands:
            elig = [j for j in tracks_of
                    if cap.get(j, 0) > 0 and j not in flat and (j, p) not in existing
                    and (j, p) not in conflicts
                    and (tracks_of[j] is None or project_track is None or project_track[p] in tracks_of[j])]
            if not elig:
                dead.add(p)
                continue
            j = max(elig, key=lambda jj: (info.get(jj, 0.0), rng.random()))
            plan.append((j, p))
            existing.add((j, p))
            cap[j] -= 1
            extra[p] += 1
            v[p] = 1.0 / (1.0 / v[p] + 1.0 / s2)
            placed = True
            break
        if not placed:
            break
    return plan


def _probit_lambda(z):
    """phi(z)/Phi(z), stable for very negative z."""
    if z < -30:
        return -z
    Phi = 0.5 * math.erfc(-z / math.sqrt(2.0))
    phi = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    return phi / max(Phi, 1e-300)


def fuse_pairwise_with_prior(prior_mean, prior_var, comparisons, sigma, iters=100):
    """Tie-break fusion: MAP of latent scores x given a Gaussian prior from the rubric fit
    (mean = calibrated score, var = se^2) and Thurstone-Mosteller pairwise likelihood
    P(a beats b) = Phi((x_a - x_b) / (sqrt(2) sigma)). Judge leniency cancels in a
    comparison, so no offsets are needed. Ties contribute half a win each way.
    Newton's method on a concave objective; items = keys of prior_mean."""
    items = sorted(prior_mean)
    idx = {p: i for i, p in enumerate(items)}
    x = [prior_mean[p] for p in items]
    s = math.sqrt(2.0) * sigma
    n = len(items)
    terms = []
    for c in comparisons:
        if c["outcome"] == 0.5:
            terms += [(c["a"], c["b"], 0.5), (c["b"], c["a"], 0.5)]
        elif c["outcome"] == 1.0:
            terms.append((c["a"], c["b"], c.get("weight", 1.0)))
        else:
            terms.append((c["b"], c["a"], c.get("weight", 1.0)))
    for _ in range(iters):
        g = [-(x[i] - prior_mean[items[i]]) / prior_var[items[i]] for i in range(n)]
        H = [[0.0] * n for _ in range(n)]
        for i in range(n):
            H[i][i] = 1.0 / prior_var[items[i]]
        for w_, l_, wt in terms:
            a, b = idx[w_], idx[l_]
            z = (x[a] - x[b]) / s
            lam = _probit_lambda(z)
            g[a] += wt * lam / s
            g[b] -= wt * lam / s
            h = wt * lam * (z + lam) / (s * s)
            H[a][a] += h
            H[b][b] += h
            H[a][b] -= h
            H[b][a] -= h
        step = chol_solve(cholesky(H), g)
        x = [xi + si for xi, si in zip(x, step)]
        if max(abs(si) for si in step) < 1e-10:
            break
    return {p: x[idx[p]] for p in items}


def icc1(reviews, weights=None):
    """One-way ICC(1) of per-review weighted totals with projects as groups (unbalanced n0)."""
    weights = weights or EQUAL_WEIGHTS
    groups = defaultdict(list)
    for r in reviews:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            groups[r["project"]].append(t)
    N = sum(len(v) for v in groups.values())
    a = len(groups)
    gm = sum(sum(v) for v in groups.values()) / N
    ssb = sum(len(v) * (sum(v) / len(v) - gm) ** 2 for v in groups.values())
    ssw = sum(sum((x - sum(v) / len(v)) ** 2 for x in v) for v in groups.values())
    msb, msw = ssb / (a - 1), ssw / (N - a)
    n0 = (N - sum(len(v) ** 2 for v in groups.values()) / N) / (a - 1)
    icc = (msb - msw) / (msb + (n0 - 1) * msw)
    return {"icc1": icc, "msb": msb, "msw": msw, "n0": n0, "F": msb / msw, "df": (a - 1, N - a)}


# =============================================================================
# 5. Comparison methods (for the report; not recommended)
# =============================================================================


def raw_mean(obs):
    by = defaultdict(list)
    for _, p, y in obs:
        by[p].append(y)
    return {p: sum(v) / len(v) for p, v in by.items()}


def _mean_sd(v):
    m = sum(v) / len(v)
    if len(v) < 2:
        return m, 0.0
    return m, math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def zscore_scores(obs, robust=False):
    """Per-judge z-score, then mean z per project, mapped back to the 1-5 scale.
    Degenerate judges (n=1 or sd=0 or MAD=0 and sd=0) get z=0 for every review --
    that fallback is itself a modelling choice (it says 'this judge thinks all their
    projects are exactly average')."""
    byj = defaultdict(list)
    for j, p, y in obs:
        byj[j].append((p, y))
    zs = defaultdict(list)
    degenerate = []
    for j, lst in byj.items():
        ys = [y for _, y in lst]
        if robust:
            srt = sorted(ys)
            n = len(srt)
            med = srt[n // 2] if n % 2 else 0.5 * (srt[n // 2 - 1] + srt[n // 2])
            dev = sorted(abs(y - med) for y in ys)
            mad = dev[n // 2] if n % 2 else 0.5 * (dev[n // 2 - 1] + dev[n // 2])
            centre, scale = med, 1.4826 * mad
            if scale == 0:
                centre, scale = _mean_sd(ys)
        else:
            centre, scale = _mean_sd(ys)
        if len(ys) < 2 or scale == 0:
            degenerate.append(j)
            for p, _ in lst:
                zs[p].append(0.0)
        else:
            for p, y in lst:
                zs[p].append((y - centre) / scale)
    allv = [y for _, _, y in obs]
    gm, gsd = _mean_sd(allv)
    return {p: gm + gsd * sum(v) / len(v) for p, v in zs.items()}, degenerate


def percentile_scores(obs):
    """Within-judge mid-rank percentile ((rank-0.5)/n, ties averaged), mean per project."""
    byj = defaultdict(list)
    for j, p, y in obs:
        byj[j].append((p, y))
    pc = defaultdict(list)
    for j, lst in byj.items():
        n = len(lst)
        for p, y in lst:
            below = sum(1 for _, v in lst if v < y)
            eq = sum(1 for _, v in lst if v == y)
            pc[p].append((below + 0.5 * eq) / n)
    return {p: sum(v) / len(v) for p, v in pc.items()}


def fit_scale_model(obs, k_b, k_a=6.0, max_iter=500, tol=1e-9):
    """y = mu + b_j + a_j * t_p + e with ridge k_b on b_j and k_a*(a_j-1)^2 on the slope.
    Returns {p: mu + t_p}. Included to show it is NOT estimable at n_j <= 6."""
    by_p = defaultdict(list)
    by_j = defaultdict(list)
    for j, p, y in obs:
        by_p[p].append((j, y))
        by_j[j].append((p, y))
    mu = sum(y for _, _, y in obs) / len(obs)
    a = {j: 1.0 for j in by_j}
    b = {j: 0.0 for j in by_j}
    t = {p: sum(y for _, y in lst) / len(lst) - mu for p, lst in by_p.items()}
    for _ in range(max_iter):
        old = dict(t)
        for p, lst in by_p.items():
            num = sum(a[j] * (y - mu - b[j]) for j, y in lst)
            den = sum(a[j] ** 2 for j, _ in lst)
            t[p] = num / den if den > 1e-9 else 0.0
        m = sum(t.values()) / len(t)
        for p in t:
            t[p] -= m
        for j, lst in by_j.items():
            # 2x2 ridge regression of (y - mu) on [1, t_p], prior b~0 (k_b), a~1 (k_a)
            s11 = len(lst) + k_b
            s12 = sum(t[p] for p, _ in lst)
            s22 = sum(t[p] ** 2 for p, _ in lst) + k_a
            r1 = sum(y - mu for _, y in lst)
            r2 = sum(t[p] * (y - mu) for p, y in lst) + k_a * 1.0
            det = s11 * s22 - s12 * s12
            b[j] = (s22 * r1 - s12 * r2) / det
            a[j] = (s11 * r2 - s12 * r1) / det
        mu = sum(y - b[j] - a[j] * t[p] for j, p, y in obs) / len(obs)
        if max(abs(t[p] - old[p]) for p in t) < tol:
            break
    return {p: mu + t[p] for p in t}, a, b


def eb_shrink(fit, unc):
    """Empirical-Bayes (Efron-Morris) shrinkage of project scores toward the mean:
    tau_p^2 = max(0, var(scores) - mean(se^2)); shrunk = m + tau^2/(tau^2+se^2) (s - m)."""
    ps = list(fit["score"])
    m = sum(fit["score"][p] for p in ps) / len(ps)
    var = sum((fit["score"][p] - m) ** 2 for p in ps) / (len(ps) - 1)
    mse = sum(unc["se"][p] ** 2 for p in ps) / len(ps)
    tau2 = max(0.0, var - mse)
    return {p: m + (tau2 / (tau2 + unc["se"][p] ** 2)) * (fit["score"][p] - m) for p in ps}, tau2


def induced_comparisons(obs):
    """Turn each judge's rubric scores into within-judge pairwise outcomes (scale-free).
    Weight 1/(n_j-1) so a judge's total weight grows like n_j/2, not n_j^2/2."""
    byj = defaultdict(list)
    for j, p, y in obs:
        byj[j].append((p, y))
    comps = []
    for j, lst in byj.items():
        n = len(lst)
        if n < 2:
            continue
        w = 1.0 / (n - 1)
        for i in range(n):
            for m in range(i + 1, n):
                (p, y), (q, z) = lst[i], lst[m]
                out = 1.0 if y > z else 0.0 if y < z else 0.5
                comps.append({"a": p, "b": q, "outcome": out, "judge": j, "weight": w})
    return comps


# =============================================================================
# 6. Bradley-Terry / Crowd-BT
# =============================================================================


def bt_fit(comparisons, items=None, prior=1.0, judge_reliability=False,
           eta_prior=(9.0, 1.0), max_iter=5000, tol=1e-10):
    """Bradley-Terry by Hunter's (2004) MM algorithm with a dummy-opponent prior.

    comparisons: list of {"a","b","outcome","judge"?,"weight"?}; outcome 1 = a better,
                 0 = b better, 0.5 = tie (counted as half a win each -- Rao-Kupper/Davidson
                 are more exact but give the same ranking at these sizes).
    prior:       every item plays `prior` virtual wins and `prior` virtual losses against a
                 dummy of strength 1. This is a proper prior on log-strength, so the fit
                 exists and is unique even for all-win items and disconnected graphs; a
                 disconnected component is pulled to the common centre (its relative level
                 is NOT identified by data -- report it, don't rank across it).
    judge_reliability: Crowd-BT (Chen et al. 2013) as a batch EM: judge k agrees with the
                 BT model with prob eta_k, flips otherwise; eta_k ~ Beta(eta_prior).
    Returns {"strength": {item: log pi}, "pi":..., "eta": {judge: eta}, "iterations": n}.
    """
    items = sorted(items or {c["a"] for c in comparisons} | {c["b"] for c in comparisons})
    pi = {i: 1.0 for i in items}
    eta = defaultdict(lambda: eta_prior[0] / sum(eta_prior))
    judges = {c.get("judge") for c in comparisons}
    if judge_reliability:
        eta = {j: eta_prior[0] / sum(eta_prior) for j in judges}
    it = 0
    for it in range(1, max_iter + 1):
        # E-step (Crowd-BT): effective win share for a in each comparison
        eff = []
        z_sum = defaultdict(float)
        n_j = defaultdict(float)
        for c in comparisons:
            w = c.get("weight", 1.0)
            o = c["outcome"]
            if judge_reliability and o in (0.0, 1.0):
                win, lose = (c["a"], c["b"]) if o == 1.0 else (c["b"], c["a"])
                pw = pi[win] / (pi[win] + pi[lose])
                e = eta[c.get("judge")]
                zz = e * pw / (e * pw + (1 - e) * (1 - pw))
                z_sum[c.get("judge")] += zz
                n_j[c.get("judge")] += 1
                share_a = zz if o == 1.0 else 1 - zz
            else:
                share_a = o
            eff.append((c["a"], c["b"], w, share_a))
        # M-step: MM update for pi
        wins = {i: prior for i in items}
        den = {i: 0.0 for i in items}
        for a, b, w, sa in eff:
            wins[a] += w * sa
            wins[b] += w * (1 - sa)
            inv = w / (pi[a] + pi[b])
            den[a] += inv
            den[b] += inv
        delta = 0.0
        new = {}
        for i in items:
            d = den[i] + 2.0 * prior / (pi[i] + 1.0)
            new[i] = wins[i] / d
            delta = max(delta, abs(math.log(new[i]) - math.log(pi[i])))
        pi = new
        if judge_reliability:
            a0, b0 = eta_prior
            for j in judges:
                eta[j] = (z_sum[j] + a0 - 1) / (n_j[j] + a0 + b0 - 2) if n_j[j] else a0 / (a0 + b0)
        if delta < tol:
            break
    return {"strength": {i: math.log(pi[i]) for i in items}, "pi": pi,
            "eta": dict(eta) if judge_reliability else None, "iterations": it}


def bt_standard_errors(comparisons, fit, prior=1.0):
    """SE of log-strength from the observed Fisher information (dummy anchors the scale)."""
    items = sorted(fit["pi"])
    idx = {i: n for n, i in enumerate(items)}
    P = len(items)
    I = [[0.0] * P for _ in range(P)]
    pi = fit["pi"]
    for i in items:
        p0 = pi[i] / (pi[i] + 1.0)
        I[idx[i]][idx[i]] += 2 * prior * p0 * (1 - p0)
    for c in comparisons:
        a, b = idx[c["a"]], idx[c["b"]]
        pa = pi[c["a"]] / (pi[c["a"]] + pi[c["b"]])
        v = c.get("weight", 1.0) * pa * (1 - pa)
        I[a][a] += v
        I[b][b] += v
        I[a][b] -= v
        I[b][a] -= v
    Linv = chol_inverse(cholesky(I))
    return {i: math.sqrt(Linv[idx[i]][idx[i]]) for i in items}


def _components(nodes, edges):
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return {n: find(n) for n in nodes}


def next_pair(judge, eligible, history, strength=None, last_seen=None, conflicts=(), rng=None,
              w_uncertain=1.0, w_coverage=1.0, w_familiar=0.5, w_connect=2.0):
    """Choose the next pair for an asynchronous judge ('information gain lite').

    judge     : judge id
    eligible  : {track: [project ids]} the judge may see (their tracks only)
    history   : list of past comparisons {"a","b","judge",...} (all judges)
    strength  : current BT log-strengths (None early on -> all 0)
    last_seen : project the judge looked at last; pairs containing it cost only one new
                project to review (the Gavel chain trick) and get a bonus
    conflicts : set of project ids this judge must not see

    Hard rules: same track, not a pair this judge already compared, no conflicts.
    Soft score: w_uncertain * 4p(1-p)            (outcome uncertainty; p from BT)
              + w_coverage  * mean(1/sqrt(1+n_i))  (items with few comparisons first)
              + w_familiar  * [pair contains last_seen]
              + w_connect   * [pair joins two components of the track's comparison graph]
    Returns (a, b) or None when the judge has exhausted their pairs.
    """
    rng = rng or random.Random()
    strength = strength or {}
    done = {frozenset((c["a"], c["b"])) for c in history if c.get("judge") == judge}
    count = defaultdict(int)
    for c in history:
        count[c["a"]] += 1
        count[c["b"]] += 1
    best, best_score = None, -1e18
    for track, plist in eligible.items():
        plist = [p for p in plist if p not in conflicts]
        comp = _components(plist, [(c["a"], c["b"]) for c in history
                                   if c["a"] in plist and c["b"] in plist])
        for i in range(len(plist)):
            for m in range(i + 1, len(plist)):
                a, b = plist[i], plist[m]
                if frozenset((a, b)) in done:
                    continue
                d = strength.get(a, 0.0) - strength.get(b, 0.0)
                p = 1.0 / (1.0 + math.exp(-d))
                sc = (w_uncertain * 4 * p * (1 - p)
                      + w_coverage * 0.5 * (1 / math.sqrt(1 + count[a]) + 1 / math.sqrt(1 + count[b]))
                      + (w_familiar if last_seen in (a, b) else 0.0)
                      + (w_connect if comp[a] != comp[b] else 0.0)
                      + 1e-6 * rng.random())
                if sc > best_score:
                    best, best_score = (a, b), sc
    return best


# =============================================================================
# 7. Assignment: overlap-maximising greedy (and a disjoint-panel baseline)
# =============================================================================


def assign(projects, judges, k=3, capacity=None, conflicts=(), seed=0):
    """Greedy assignment that keeps the judge-project graph connected and well mixed,
    because judge offsets are only identifiable through shared projects.

    projects : list of {"id","track"}
    judges   : list of {"id","tracks": [...] or None (= any track)}
    k        : reviews per project
    capacity : int or {judge: int}; default ceil(k*|P|/|J|)+1
    conflicts: set of (judge_id, project_id) pairs that are forbidden
    Slots are filled in rounds (every project gets its 1st judge, then its 2nd, ...),
    hardest project first (fewest eligible judges). For each slot the eligible judge
    with spare capacity maximising
        3 * [new co-review edges with judges already on the project]
      + 2 * [joins two different components of the judge graph]
      - 2 * load/capacity
      + tiny random tie-break
    is chosen. Returns {"assignment": {p: [j..]}, "unfilled": [...], "components": n}.
    """
    rng = random.Random(seed)
    conflicts = set(conflicts)
    J = [j["id"] for j in judges]
    tracks_of = {j["id"]: (set(j["tracks"]) if j.get("tracks") else None) for j in judges}
    if capacity is None:
        capacity = math.ceil(k * len(projects) / max(1, len(J))) + 1
    cap = capacity if isinstance(capacity, dict) else {j: capacity for j in J}
    load = defaultdict(int)
    edges = set()
    parent = {j: j for j in J}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def eligible(j, p):
        t = tracks_of[j]
        return (t is None or p["track"] in t) and (j, p["id"]) not in conflicts

    elig = {p["id"]: [j for j in J if eligible(j, p)] for p in projects}
    order = sorted(projects, key=lambda p: (len(elig[p["id"]]), rng.random()))
    out = {p["id"]: [] for p in projects}
    unfilled = []
    for rnd in range(k):
        for p in order:
            pid = p["id"]
            cands = [j for j in elig[pid] if j not in out[pid] and load[j] < cap[j]]
            if not cands:
                unfilled.append((pid, rnd + 1))
                continue

            def score(j):
                new_edges = sum(1 for o in out[pid] if (min(j, o), max(j, o)) not in edges)
                joins = sum(1 for o in out[pid] if find(o) != find(j))
                return 3 * new_edges + 2 * joins - 2 * load[j] / cap[j] + 1e-6 * rng.random()

            j = max(cands, key=score)
            for o in out[pid]:
                edges.add((min(j, o), max(j, o)))
                parent[find(j)] = find(o)
            out[pid].append(j)
            load[j] += 1
    comps = len({find(j) for j in J if load[j] > 0})
    return {"assignment": out, "unfilled": unfilled, "components": comps,
            "load": dict(load), "judge_edges": len(edges)}


def assign_disjoint_panels(projects, judges, k=3, seed=0):
    """Baseline: split judges into panels of k; each panel reviews a disjoint block of
    projects (classic 'table judging'). Panels never share a project, so the judge graph
    has one component per panel and cross-panel leniency is unidentifiable."""
    rng = random.Random(seed)
    J = [j["id"] for j in judges]
    rng.shuffle(J)
    panels = [J[i:i + k] for i in range(0, len(J) - len(J) % k, k)]
    P = [p["id"] for p in projects]
    rng.shuffle(P)
    out = {}
    for i, p in enumerate(P):
        out[p] = list(panels[i % len(panels)])
    return {"assignment": out, "components": len(panels)}


# =============================================================================
# 8. Design diagnostics
# =============================================================================


def design_diagnostics(reviews, projects, judges):
    ptrack = {p["id"]: p["track"] for p in projects}
    jtracks = {j["id"]: j.get("tracks") or [] for j in judges}
    by_j = defaultdict(set)
    by_p = defaultdict(set)
    for r in reviews:
        by_j[r["judge"]].add(r["project"])
        by_p[r["project"]].add(r["judge"])
    nodes = ["j:" + j for j in by_j] + ["p:" + p for p in by_p]
    comp = _components(nodes, [("j:" + r["judge"], "p:" + r["project"]) for r in reviews])
    comps = defaultdict(list)
    for n, c in comp.items():
        comps[c].append(n)
    track_comps = {}
    for t in sorted(set(ptrack.values())):
        rv = [r for r in reviews if ptrack[r["project"]] == t]
        nd = {"j:" + r["judge"] for r in rv} | {"p:" + r["project"] for r in rv}
        tc = _components(sorted(nd), [("j:" + r["judge"], "p:" + r["project"]) for r in rv])
        track_comps[t] = len(set(tc.values()))
    # information about each judge offset: n_j - sum_{p in j} 1/n_p  (Schur complement diag)
    info = {j: len(ps) - sum(1.0 / len(by_p[p]) for p in ps) for j, ps in by_j.items()}
    co = defaultdict(set)
    shared = {}
    js = sorted(by_j)
    for a in range(len(js)):
        for b in range(a + 1, len(js)):
            s = len(by_j[js[a]] & by_j[js[b]])
            if s:
                co[js[a]].add(js[b])
                co[js[b]].add(js[a])
                shared[(js[a], js[b])] = s
    bridges = {}
    for j, ps in by_j.items():
        ts = defaultdict(int)
        for p in ps:
            ts[ptrack[p]] += 1
        if len(ts) > 1:
            bridges[j] = dict(ts)
    completion = {}
    tsize = defaultdict(int)
    for p in ptrack.values():
        tsize[p] += 1
    for j in jtracks:
        row = {}
        for t in jtracks[j]:
            row[t] = (sum(1 for p in by_j.get(j, ()) if ptrack[p] == t), tsize[t])
        completion[j] = row
    return {
        "n_components": len(comps),
        "component_sizes": sorted((sum(1 for n in c if n[0] == "j"), sum(1 for n in c if n[0] == "p"))
                                  for c in comps.values()),
        "track_components": track_comps,
        "offset_information": info,
        "co_judges": {j: len(co[j]) for j in js},
        "judge_pairs_sharing": len(shared),
        "judge_pairs_total": len(js) * (len(js) - 1) // 2,
        "shared_counts": shared,
        "bridges": bridges,
        "completion": completion,
        "reviews_per_project": {p: len(v) for p, v in by_p.items()},
        "reviews_per_judge": {j: len(v) for j, v in by_j.items()},
    }


# =============================================================================
# 9. Rank-agreement metrics
# =============================================================================


def rank_agreement(scores_a, scores_b, groups=None):
    """Cross-check two rankings (e.g. rubric vs pairwise): Kendall tau-b overall and per group
    (track). Use it to flag tracks where the two judging modes disagree instead of fusing them."""
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
    rx, ry = _avg_ranks(x), _avg_ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else 0.0


if __name__ == "__main__":
    # self-test: ALS vs direct GLS solve, decomposition exactness
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "../fixtures.json"
    fx = load_fixture(path)
    projects, reviews, log = prepare_reviews(fx)
    fit = fit_judge_effects(reviews, EQUAL_WEIGHTS)
    obs = fit["used_obs_totals"]
    pr, A, c, _, _ = _gls_pieces(obs, 1.0 / fit["k"])
    beta = chol_solve(cholesky(A), c)
    err = max(abs(beta[i] - fit["score"][p]) for i, p in enumerate(pr))
    print("k=%.3f  max |ALS - GLS| = %.2e" % (fit["k"], err))
    bad = [p for p in fit["score"] if not explain_project(fit, p)["exact"]]
    print("decomposition exact for all projects:", not bad)
