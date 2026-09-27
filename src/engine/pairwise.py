"""Pairwise judging: Bradley-Terry estimation, pair selection, and tie-break fusion.

Pairwise comparison ("which of these two is better?") sidesteps judge leniency entirely:
a harsh and a generous judge agree about *order* even when they disagree about numbers.
It is weak as a primary mode for async online judging (it produces no per-criterion
scores or written feedback, and needs many comparisons), and strong exactly where Quorum
uses it: separating a handful of statistically tied contenders at a prize boundary.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from .linalg import chol_inverse, chol_solve, cholesky


def bt_fit(comparisons, items=None, prior=1.0, max_iter=5000, tol=1e-10):
    """Bradley-Terry by Hunter's (2004) MM algorithm with a dummy-opponent prior.

    comparisons: [{"a","b","outcome","weight"?}]; outcome 1 = a better, 0 = b better,
                 0.5 = tie (half a win each).
    prior:       every item plays `prior` virtual wins and losses against a dummy of
                 strength 1 -- a proper prior on log-strength, so the fit exists and is
                 unique even for all-win items and disconnected comparison graphs.
    Returns {"strength": {item: log pi}, "pi": {...}, "iterations": n}.
    """
    items = sorted(set(items or []) | {c["a"] for c in comparisons} | {c["b"] for c in comparisons})
    pi = {i: 1.0 for i in items}
    it = 0
    for it in range(1, max_iter + 1):
        wins = {i: prior for i in items}
        den = {i: 0.0 for i in items}
        for c in comparisons:
            w = c.get("weight", 1.0)
            a, b, sa = c["a"], c["b"], c["outcome"]
            wins[a] += w * sa
            wins[b] += w * (1 - sa)
            inv = w / (pi[a] + pi[b])
            den[a] += inv
            den[b] += inv
        delta = 0.0
        new = {}
        for i in items:
            new[i] = wins[i] / (den[i] + 2.0 * prior / (pi[i] + 1.0))
            delta = max(delta, abs(math.log(new[i]) - math.log(pi[i])))
        pi = new
        if delta < tol:
            break
    return {"strength": {i: math.log(pi[i]) for i in items}, "pi": pi, "iterations": it}


def bt_standard_errors(comparisons, fit, prior=1.0):
    """SE of log-strength from the observed Fisher information (the dummy anchors the scale)."""
    items = sorted(fit["pi"])
    if not items:
        return {}
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
    inv = chol_inverse(cholesky(I))
    return {i: math.sqrt(inv[idx[i]][idx[i]]) for i in items}


def components(nodes, edges):
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
    """Choose the next pair for an asynchronous judge.

    eligible : {group: [project ids]} the judge may compare (their track, or a tie-break set)
    history  : past comparisons [{"a","b","judge"}] (all judges)
    strength : current BT log-strengths (None early on)
    last_seen: the project the judge looked at last; pairs containing it cost only one new
               project to review (the Gavel chain) and get a bonus
    Hard rules: same group, never a pair this judge already compared, no conflicts.
    Soft score: 4p(1-p) + coverage + chain bonus + component-join bonus.
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
    for plist in eligible.values():
        plist = [p for p in plist if p not in set(conflicts)]
        comp = components(plist, [(c["a"], c["b"]) for c in history if c["a"] in plist and c["b"] in plist])
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


def _probit_lambda(z):
    if z < -30:
        return -z
    Phi = 0.5 * math.erfc(-z / math.sqrt(2.0))
    phi = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    return phi / max(Phi, 1e-300)


def fuse_pairwise_with_prior(prior_mean, prior_var, comparisons, sigma, iters=100):
    """Tie-break fusion: MAP latent scores given a Gaussian prior from the rubric fit
    (mean = calibrated score, var = se^2) and a Thurstone-Mosteller pairwise likelihood
    P(a beats b) = Phi((x_a - x_b) / (sqrt(2) sigma)). Judge leniency cancels inside a
    comparison, so no offsets are needed. Ties are half a win each way. Newton's method
    on a concave objective. Returns {"mean": {p: x}, "var": {p: posterior variance}}."""
    items = sorted(prior_mean)
    idx = {p: i for i, p in enumerate(items)}
    x = [prior_mean[p] for p in items]
    s = math.sqrt(2.0) * max(sigma, 1e-6)
    n = len(items)
    terms = []
    for c in comparisons:
        w = c.get("weight", 1.0)
        if c["outcome"] == 0.5:
            terms += [(c["a"], c["b"], 0.5 * w), (c["b"], c["a"], 0.5 * w)]
        elif c["outcome"] == 1.0:
            terms.append((c["a"], c["b"], w))
        else:
            terms.append((c["b"], c["a"], w))
    H = None
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
    cov = chol_inverse(cholesky(H)) if H else [[0.0]]
    return {"mean": {p: x[idx[p]] for p in items},
            "var": {p: cov[idx[p]][idx[p]] for p in items},
            "cov": {(a, b): cov[idx[a]][idx[b]] for a in items for b in items}}


def order_probability(fused, a, b):
    """P(a truly above b) under the fused Gaussian posterior."""
    m = fused["mean"][a] - fused["mean"][b]
    v = fused["var"][a] + fused["var"][b] - 2 * fused["cov"][(a, b)]
    return 0.5 * (1.0 + math.erf(m / math.sqrt(2.0 * max(v, 1e-15))))


def induced_comparisons(obs):
    """Turn each judge's rubric totals into within-judge pairwise outcomes (scale-free).
    Weight 1/(n_j - 1) so a judge's total weight grows like n_j/2, not n_j^2/2."""
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
                comps.append({"a": p, "b": q, "outcome": 1.0 if y > z else 0.0 if y < z else 0.5,
                              "judge": j, "weight": w})
    return comps
