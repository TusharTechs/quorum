"""Monte Carlo evidence for the judging maths. Stdlib only (multiprocessing for speed).

  python3 simulate.py normalization 500   # task 6: the normalization proof on the fixture design
  python3 simulate.py design 500          # disjoint panels vs overlap-maximising assignment
  python3 simulate.py adaptive 400        # adaptive review allocation + pairwise tie-break
  python3 simulate.py pairwise 200        # Bradley-Terry recovery at fixture scale

Every experiment uses common random numbers: within one seed, the score a given judge
would give a given project is the same draw whichever strategy asks for it, so
differences between strategies are paired and low-variance.
"""
import json
import math
import random
import sys
from collections import defaultdict
from multiprocessing import Pool

import judging_math as jm

FIXTURE = "../fixtures.json"
CRIT = ("functionality", "quality", "innovation")
W = jm.EQUAL_WEIGHTS

_fx = jm.load_fixture(FIXTURE)
PROJECTS, _REVIEWS, _ = jm.prepare_reviews(_fx)
JUDGES = _fx["judges"]
PAIRS = [(r["judge"], r["project"]) for r in _REVIEWS]
TRACK = {p["id"]: p["track"] for p in PROJECTS}
TRACKS = sorted(set(TRACK.values()))

# Fixture track memberships leave Developer tools and Open hardware with only 3 eligible
# judges, which makes '4 reviews per project' infeasible. For the allocation experiments
# we give ten single-track judges a second track (documented, and marked 'augmented').
_EXTRA = {"jdg_08": "trk_01", "jdg_10": "trk_01", "jdg_13": "trk_01",
          "jdg_18": "trk_08", "jdg_19": "trk_08", "jdg_21": "trk_08",
          "jdg_28": "trk_02", "jdg_14": "trk_02", "jdg_05": "trk_06", "jdg_17": "trk_06"}
AUG_JUDGES = [{"id": j["id"], "tracks": list(j["tracks"]) + ([_EXTRA[j["id"]]] if j["id"] in _EXTRA else [])}
              for j in JUDGES]

BASE = dict(tau_p=0.45, tau_d=0.30, tau_b=0.40, sigma_c=0.90, tau_a=0.0, tau_t=0.0)
SCENARIOS = {
    "S0 no judge effects": dict(BASE, tau_b=0.0),
    "S1 leniency sd .4 (ICC~.34)": dict(BASE),
    "S2 S1 + judge scale sd .3": dict(BASE, tau_a=0.3),
    "S3 S1 + track strength sd .3": dict(BASE, tau_t=0.3),
    "S4 strong leniency sd .7": dict(BASE, tau_b=0.7),
    "S5 fixture-like: almost no signal": dict(tau_p=0.12, tau_d=0.05, tau_b=0.15, sigma_c=1.1, tau_a=0.0, tau_t=0.0),
}


def _stable(*parts):
    h = 1469598103934665603
    for x in parts:
        for ch in str(x):
            h = ((h ^ ord(ch)) * 1099511628211) % (1 << 61)
        h = ((h ^ 0x2F) * 1099511628211) % (1 << 61)
    return h


class World:
    """Truth + a deterministic review oracle. y_jpc = round(mu0 + a_j (theta_p + d_pc) + b_j + e),
    clipped to 1..5. Flat judges give `flat_value` on every criterion."""

    def __init__(self, seed, projects, judges, tau_p, tau_d, tau_b, sigma_c, tau_a, tau_t,
                 mu0=3.4, flat=("jdg_07",), flat_value=3.0, theta=None):
        rng = random.Random(_stable("world", seed))
        self.seed = seed
        tracks = sorted({p["track"] for p in projects})
        te = {t: rng.gauss(0, tau_t) for t in tracks}
        self.theta = theta or {p["id"]: te[p["track"]] + rng.gauss(0, tau_p) for p in projects}
        self.d = {(p["id"], c): rng.gauss(0, tau_d) for p in projects for c in CRIT}
        self.Q = {p: self.theta[p] + sum(self.d[(p, c)] for c in CRIT) / 3 for p in self.theta}
        self.b = {j["id"]: rng.gauss(0, tau_b) for j in judges}
        self.a = {j["id"]: math.exp(rng.gauss(0, tau_a)) if tau_a else 1.0 for j in judges}
        self.flat = set(flat)
        self.flat_value = flat_value
        self.sigma_c = sigma_c
        self.mu0 = mu0
        self.sigma_tot = math.sqrt((sigma_c ** 2 + 1 / 12) / 3)

    def review(self, j, p):
        if j in self.flat:
            return {"judge": j, "project": p, "criteria": {c: self.flat_value for c in CRIT}}
        rng = random.Random(_stable("rev", self.seed, j, p))
        crit = {}
        for c in CRIT:
            v = self.mu0 + self.a[j] * (self.theta[p] + self.d[(p, c)]) + self.b[j] + rng.gauss(0, self.sigma_c)
            crit[c] = float(min(5, max(1, round(v))))
        return {"judge": j, "project": p, "criteria": crit}

    def impression(self, key, p):
        """A judge's persistent impression of a project (same noise level as a review total)."""
        rng = random.Random(_stable("imp", self.seed, key, p))
        return self.Q[p] + rng.gauss(0, self.sigma_tot)

    def compare_persistent(self, a, b, key):
        return 1.0 if self.impression(key, a) > self.impression(key, b) else 0.0

    def compare(self, a, b, key):
        """Thurstone choice on latent quality with the same per-look noise as a review total."""
        rng = random.Random(_stable("cmp", self.seed, key, a, b))
        xa = self.Q[a] + rng.gauss(0, self.sigma_tot)
        xb = self.Q[b] + rng.gauss(0, self.sigma_tot)
        return 1.0 if xa > xb else 0.0


# ----------------------------------------------------------------------------------------------
def metrics(est, Q, track, seed):
    rng = random.Random(_stable("tie", seed))
    tb = {p: rng.random() for p in Q}
    ids = sorted(Q)
    true_order = sorted(ids, key=lambda p: -Q[p])
    est_order = sorted(ids, key=lambda p: (-est[p], tb[p]))
    er = {p: i for i, p in enumerate(est_order, 1)}
    tr = {p: i for i, p in enumerate(true_order, 1)}
    top10 = true_order[:10]
    wins = 0
    for t in set(track.values()):
        ps = [p for p in ids if track[p] == t]
        if max(ps, key=lambda p: Q[p]) == min(ps, key=lambda p: (-est[p], tb[p])):
            wins += 1
    return {
        "tau": jm.kendall_tau_b([est[p] for p in ids], [Q[p] for p in ids]),
        "top3_exact": float(set(est_order[:3]) == set(true_order[:3])),
        "first": float(est_order[0] == true_order[0]),
        "rank_err_top5": sum(abs(er[p] - tr[p]) for p in true_order[:5]) / 5,
        "tau_top10": jm.kendall_tau_b([est[p] for p in top10], [Q[p] for p in top10]),
        "track_winner": wins / len(set(track.values())),
    }


def all_methods(reviews, world=None):
    obs = [(r["judge"], r["project"], jm.weighted_total(r["criteria"], W)) for r in reviews]
    out = {}
    out["A raw mean"] = jm.raw_mean(obs)
    out["B z-score"], _ = jm.zscore_scores(obs)
    out["C1 robust z"], _ = jm.zscore_scores(obs, robust=True)
    out["C2 percentile"] = jm.percentile_scores(obs)
    out["D k=3"] = jm.fit_judge_effects(reviews, W, k=3.0, flat_rule=False)["score"]
    fr = jm.fit_judge_effects(reviews, W, flat_rule=False)
    out["D k=REML"] = fr["score"]
    fs = jm.fit_judge_effects(reviews, W, flat_rule=True)
    out["D* REML+flat"] = fs["score"]
    out["D2 +scale"], _, _ = jm.fit_scale_model(obs, k_b=fr["k"])
    unc = jm.project_uncertainty(fs)
    out["E1 EB-shrunk"], _ = jm.eb_shrink(fs, unc)
    out["E2 induced-BT"] = jm.bt_fit(jm.induced_comparisons(obs), items=sorted(fs["score"]))["strength"]
    if world is not None:
        by = defaultdict(list)
        for j, p, y in obs:
            if j not in world.flat:
                by[p].append(y - world.b[j])
        out["O oracle offsets"] = {p: (sum(v) / len(v) if v else out["A raw mean"][p]) for p, v in
                                   ((p, by.get(p, [])) for p in out["A raw mean"])}
    return out, fs


def summarise(rows, key_methods, metric_names, ref=None):
    """rows: list of {method: {metric: value}} -> table of mean (95% CI) and paired diff vs ref."""
    n = len(rows)
    res = {}
    for m in key_methods:
        res[m] = {}
        for k in metric_names:
            v = [r[m][k] for r in rows]
            mu = sum(v) / n
            sd = math.sqrt(sum((x - mu) ** 2 for x in v) / max(1, n - 1))
            res[m][k] = (mu, 1.96 * sd / math.sqrt(n))
            if ref and m != ref:
                d = [r[m][k] - r[ref][k] for r in rows]
                md = sum(d) / n
                sdd = math.sqrt(sum((x - md) ** 2 for x in d) / max(1, n - 1))
                res[m][k + "_diff"] = (md, 1.96 * sdd / math.sqrt(n))
                res[m][k + "_win"] = sum(1 for x in d if x > 0) / n
    return res


def print_table(title, res, metric_names, ref=None, fmt="%.3f"):
    print("\n### " + title)
    head = "| method | " + " | ".join(metric_names) + (" | tau - tau(%s) [95%% CI] | P(tau > %s) |" % (ref, ref) if ref else " |")
    print(head)
    print("|---" * (len(metric_names) + 1 + (2 if ref else 0)) + "|")
    for m, r in res.items():
        cells = [(fmt % r[k][0]) + " ±" + ("%.3f" % r[k][1]) for k in metric_names]
        extra = ""
        if ref:
            if m == ref:
                extra = " | - | - |"
            else:
                extra = " | %+.3f ±%.3f | %.2f |" % (r["tau_diff"][0], r["tau_diff"][1], r["tau_win"])
        print("| %s | %s%s" % (m, " | ".join(cells), extra if ref else " |"))


# ----------------------------------------------------------------------------------------------
# Experiment 1: normalization proof on the fixture's exact design
def _norm_one(args):
    seed, scen = args
    world = World(seed, PROJECTS, JUDGES, **SCENARIOS[scen])
    reviews = [world.review(j, p) for j, p in PAIRS]
    M, fs = all_methods(reviews, world)
    out = {m: metrics(s, world.Q, TRACK, seed) for m, s in M.items()}
    # Health track (flat judge's projects): within-track tau
    hp = [p for p in world.Q if TRACK[p] == "trk_06"]
    for m, s in M.items():
        out[m]["health_first"] = float(max(hp, key=lambda p: world.Q[p]) == max(hp, key=lambda p: s[p]))
    out["_k"] = fs["k"]
    out["_icc"] = jm.icc1(reviews)["icc1"]
    return out


def exp_normalization(n, only=None):
    metric_names = ["tau", "top3_exact", "first", "rank_err_top5", "track_winner", "health_first"]
    allres = {}
    with Pool(8) as pool:
        for scen in SCENARIOS:
            if only and not scen.startswith(only):
                continue
            rows = pool.map(_norm_one, [(s, scen) for s in range(n)])
            ks = sorted(r["_k"] for r in rows)
            icc = sum(r["_icc"] for r in rows) / n
            rows = [{m: v for m, v in r.items() if not m.startswith("_")} for r in rows]
            methods = list(rows[0].keys())
            res = summarise(rows, methods, metric_names, ref="A raw mean")
            print_table("%s  (n=%d sims; realised ICC(1)=%.2f; REML k median %.1f, IQR %.1f-%.1f)" % (
                scen, n, icc, ks[n // 2], ks[n // 4], ks[3 * n // 4]), res, metric_names, ref="A raw mean")
            allres[scen] = {"res": res, "icc": icc, "k_median": ks[n // 2]}
    return allres


# ----------------------------------------------------------------------------------------------
# Experiment 2: assignment design -- disjoint panels vs overlap-maximising greedy vs random
def _design_one(seed):
    projects = [{"id": "p%02d" % i, "track": "t%d" % (i % 8)} for i in range(40)]
    judges = [{"id": "j%02d" % i, "tracks": None} for i in range(30)]
    world = World(seed, projects, judges, **SCENARIOS["S1 leniency sd .4 (ICC~.34)"], flat=("j00",))
    track = {p["id"]: p["track"] for p in projects}
    rng = random.Random(_stable("design", seed))
    designs = {}
    designs["disjoint panels"] = jm.assign_disjoint_panels(projects, judges, 3, seed)["assignment"]
    g = jm.assign(projects, judges, 3, capacity=4, seed=seed)
    designs["overlap greedy"] = g["assignment"]
    # random: each project draws 3 judges uniformly among those with spare capacity
    load = defaultdict(int)
    rnd = {}
    for p in rng.sample([q["id"] for q in projects], 40):
        c = [j["id"] for j in judges if load[j["id"]] < 5]
        pick = rng.sample(c, 3)
        for j in pick:
            load[j] += 1
        rnd[p] = pick
    designs["random"] = rnd
    out = {}
    for name, asg in designs.items():
        reviews = [world.review(j, p) for p, js in asg.items() for j in js]
        obs = [(r["judge"], r["project"], jm.weighted_total(r["criteria"], W)) for r in reviews]
        fs = jm.fit_judge_effects(reviews, W)
        by = defaultdict(list)
        for j, p, y in obs:
            if j not in world.flat:
                by[p].append(y - world.b[j])
        est = {"raw": jm.raw_mean(obs), "D k=3": jm.fit_judge_effects(reviews, W, k=3.0)["score"],
               "D* REML+flat": fs["score"],
               "oracle": {p: sum(v) / len(v) if v else 3.0 for p, v in ((p, by.get(p, [])) for p in world.Q)}}
        dd = jm.design_diagnostics(reviews, projects, judges)
        for m, s in est.items():
            r = metrics(s, world.Q, track, seed)
            r["components"] = dd["n_components"]
            r["judge_pairs_sharing"] = dd["judge_pairs_sharing"]
            out["%s | %s" % (name, m)] = r
    return out


def exp_design(n):
    with Pool(8) as pool:
        rows = pool.map(_design_one, range(n))
    metric_names = ["tau", "top3_exact", "first", "rank_err_top5", "track_winner", "components", "judge_pairs_sharing"]
    res = summarise(rows, list(rows[0].keys()), metric_names)
    print_table("Assignment design (40 projects, 30 judges, 3 reviews each, S1 truth, one flat judge; n=%d)" % n,
                res, metric_names)
    # paired: D* gain over raw within each design
    print("\nD* gain over raw mean, per design (paired, tau):")
    for d in ("disjoint panels", "overlap greedy", "random"):
        diffs = [r["%s | D* REML+flat" % d]["tau"] - r["%s | raw" % d]["tau"] for r in rows]
        mu = sum(diffs) / n
        sd = math.sqrt(sum((x - mu) ** 2 for x in diffs) / (n - 1))
        print("  %-16s %+.3f ±%.3f" % (d, mu, 1.96 * sd / math.sqrt(n)))
    return res


# ----------------------------------------------------------------------------------------------
# Experiment 3: adaptive review allocation + pairwise tie-break
CAP = 8


def _collect(world, pairs):
    return [world.review(j, p) for j, p in pairs]


def _fit_probs(reviews, seed, draws=1000):
    fit = jm.fit_judge_effects(reviews, W)
    unc = jm.project_uncertainty(fit)
    bs = jm.prize_probabilities(fit, unc, top_n=3, draws=draws, seed=seed)
    return fit, unc, bs


def _adaptive(world, base_pairs, rounds, seed):
    pairs = list(base_pairs)
    for r, budget in enumerate(rounds):
        fit, unc, bs = _fit_probs(_collect(world, pairs), _stable(seed, r))
        load = defaultdict(int)
        for j, _ in pairs:
            load[j] += 1
        cap = {j["id"]: CAP - load[j["id"]] for j in AUG_JUDGES}
        plan = jm.plan_next_round(fit, bs, budget, AUG_JUDGES, cap, project_track=TRACK,
                                  existing=set(pairs), seed=_stable(seed, "plan", r))
        pairs += plan
    return pairs


def _tiebreak(world, fit, unc, bs, n_judges=3, max_set=6, thresh=0.05, mode="persistent"):
    """mode 'iid': every comparison has fresh noise (optimistic upper bound).
    mode 'persistent': each tie-break judge holds one noisy impression per project and all
    their comparisons follow it (realistic); each comparison then gets weight 2/|T| so a
    judge's |T|(|T|-1)/2 correlated comparisons count as |T|-1 independent ones."""
    T = sorted([p for p, v in bs["p_top"].items() if v > thresh], key=lambda p: -bs["p_top"][p])[:max_set]
    if len(T) < 2:
        return dict(fit["score"]), dict(fit["score"]), 0, len(T)
    comps = []
    wt = 1.0 if mode == "iid" else 2.0 / len(T)
    for k in range(n_judges):
        for i in range(len(T)):
            for m in range(i + 1, len(T)):
                a, b = T[i], T[m]
                o = world.compare(a, b, "tb%d" % k) if mode == "iid" else world.compare_persistent(a, b, "tb%d" % k)
                comps.append({"a": a, "b": b, "outcome": o, "judge": "tb%d" % k, "weight": wt})
    sigma = math.sqrt(unc["sigma2"])
    fused = jm.fuse_pairwise_with_prior({p: fit["score"][p] for p in T}, {p: unc["se"][p] ** 2 for p in T},
                                        comps, sigma)
    est_f = dict(fit["score"])
    est_f.update(fused)
    # pairwise-only: order T by BT, keep them on top block in that order
    bt = jm.bt_fit(comps, items=T, prior=0.5)["strength"]
    est_b = dict(fit["score"])
    top = max(fit["score"].values()) + 10
    for p in T:
        est_b[p] = top + bt[p]
    return est_f, est_b, len(comps), len(T)


def _adaptive_one(seed):
    world = World(seed, PROJECTS, AUG_JUDGES, **SCENARIOS["S1 leniency sd .4 (ICC~.34)"])
    pairs = {}
    for k in (2, 3, 4):
        asg = jm.assign(PROJECTS, AUG_JUDGES, k, capacity=CAP, seed=seed)["assignment"]
        pairs[k] = [(j, p) for p, js in asg.items() for j in js]
    strategies = {
        "U3 uniform 3 (120)": pairs[3],
        "A2+ 2 then 2x20 targeted (120)": _adaptive(world, pairs[2], [20, 20], seed),
        "A2+ 2 then 1x40 targeted (120)": _adaptive(world, pairs[2], [40], seed),
        "A3+ 3 then +20 targeted (140)": _adaptive(world, pairs[3], [20], seed),
        "A3+ 3 then 2x10 targeted (140)": _adaptive(world, pairs[3], [10, 10], seed),
        "U4 uniform 4 (160)": pairs[4],
    }
    out = {}
    for name, pr in strategies.items():
        reviews = _collect(world, pr)
        fit, unc, bs = _fit_probs(reviews, _stable(seed, name, "final"))
        r = metrics(fit["score"], world.Q, TRACK, seed)
        r["reviews"] = len(pr)
        r["icc"] = jm.icc1(reviews)["icc1"]
        out[name] = r
        if name.startswith(("U3", "A2+ 2 then 2x20", "A3+ 3 then +20", "U4")):
            for ms, mode in ((6, "iid"), (4, "persistent"), (6, "persistent")):
                est_f, est_b, ncomp, nT = _tiebreak(world, fit, unc, bs, max_set=ms, mode=mode)
                r2 = metrics(est_f, world.Q, TRACK, seed)
                r2["reviews"] = len(pr)
                r2["tb_comparisons"] = ncomp
                r2["tb_set"] = nT
                out[name + " + TB<=%d %s (fused)" % (ms, mode)] = r2
                if ms == 6 and mode == "persistent":
                    r3 = metrics(est_b, world.Q, TRACK, seed)
                    r3["reviews"] = len(pr)
                    out[name + " + TB<=6 persistent (pairwise only)"] = r3
    return out


def exp_adaptive(n):
    with Pool(8) as pool:
        rows = pool.map(_adaptive_one, range(n))
    metric_names = ["top3_exact", "first", "rank_err_top5", "tau_top10", "track_winner", "tau", "reviews"]
    names = list(rows[0].keys())
    res = summarise(rows, names, metric_names)
    print_table("Adaptive allocation (fixture tracks, augmented; S1 truth ICC~.34, offsets sd .4, flat jdg_07; n=%d)" % n,
                res, metric_names)
    print("\nrealised ICC(1) under U3: %.2f" % (sum(r["U3 uniform 3 (120)"]["icc"] for r in rows) / n))
    for nm in names:
        if "(fused)" in nm:
            print("  %s: mean tie set %.1f projects, %.1f comparisons" % (
                nm, sum(r[nm]["tb_set"] for r in rows) / n, sum(r[nm]["tb_comparisons"] for r in rows) / n))
    print("\nPaired differences vs U3 (mean ±95% CI):")
    for nm in names:
        if nm.startswith("U3 uniform 3 (120)") and nm == "U3 uniform 3 (120)":
            continue
        cells = []
        for k in ("top3_exact", "first", "rank_err_top5", "tau_top10"):
            d = [r[nm][k] - r["U3 uniform 3 (120)"][k] for r in rows]
            mu = sum(d) / n
            sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (n - 1))
            cells.append("%s %+.3f±%.3f" % (k, mu, 1.96 * sd / math.sqrt(n)))
        print("  %-48s %s" % (nm, "  ".join(cells)))
    return res


# ----------------------------------------------------------------------------------------------
# Experiment 4: pairwise (Bradley-Terry) recovery at fixture scale
_TRUTH = None


def _fixture_truth():
    fit = jm.fit_judge_effects(_REVIEWS, W)
    return fit["score"]


def _within_track(est, Q):
    taus, wins = [], 0
    for t in TRACKS:
        ps = [p for p in Q if TRACK[p] == t]
        taus.append(jm.kendall_tau_b([est[p] for p in ps], [Q[p] for p in ps]))
        wins += max(ps, key=lambda p: Q[p]) == max(ps, key=lambda p: est[p])
    return sum(taus) / len(taus), wins / len(TRACKS)


def _pairwise_one(args):
    seed, cfg = args
    truth_raw = cfg["truth_scores"]
    m0 = sum(truth_raw.values()) / len(truth_raw)
    sd0 = math.sqrt(sum((v - m0) ** 2 for v in truth_raw.values()) / (len(truth_raw) - 1))
    scale = cfg["truth_sd"] / sd0 if cfg["truth_sd"] else 1.0
    theta = {p: (v - m0) * scale for p, v in truth_raw.items()}
    world = World(seed, PROJECTS, JUDGES, **dict(SCENARIOS["S1 leniency sd .4 (ICC~.34)"], tau_d=0.0), theta=theta)
    rng = random.Random(_stable("pw", seed, cfg["name"]))
    clickers = set(rng.sample([j["id"] for j in JUDGES if j["id"] != "jdg_07"], cfg["clickers"]))
    history = []
    last = {}
    looks = set()
    for rnd in range(cfg["m"]):
        strength = jm.bt_fit(history, items=sorted(TRACK), tol=1e-7)["strength"] if (cfg["sel"] == "adaptive" and history) else {}
        order = list(JUDGES)
        rng.shuffle(order)
        for j in order:
            jid = j["id"]
            elig = {t: [p for p in TRACK if TRACK[p] == t] for t in j["tracks"]}
            if cfg["sel"] == "adaptive":
                pair = jm.next_pair(jid, elig, history, strength, last.get(jid), rng=rng)
            else:
                done = {frozenset((c["a"], c["b"])) for c in history if c["judge"] == jid}
                opts = [(a, b) for ps in elig.values() for i, a in enumerate(ps) for b in ps[i + 1:]
                        if frozenset((a, b)) not in done]
                pair = rng.choice(opts) if opts else None
            if pair is None:
                continue
            a, b = pair
            if jid in clickers:
                o = float(rng.random() < 0.5)
            elif cfg.get("noise") == "persistent":
                o = world.compare_persistent(a, b, jid)
            else:
                o = world.compare(a, b, jid + ":%d" % rnd)
            history.append({"a": a, "b": b, "outcome": o, "judge": jid})
            looks.update({(jid, a), (jid, b)})
            last[jid] = b
    bt = jm.bt_fit(history, items=sorted(TRACK))["strength"]
    out = {}
    tau, win = _within_track(bt, world.Q)
    out["BT"] = {"tau_within": tau, "winner": win, "comparisons": len(history), "looks": len(looks)}
    if cfg["clickers"]:
        cb = jm.bt_fit(history, items=sorted(TRACK), judge_reliability=True)
        tau, win = _within_track(cb["strength"], world.Q)
        out["Crowd-BT"] = {"tau_within": tau, "winner": win, "comparisons": len(history), "looks": len(looks)}
    # rubric baseline on the fixture design with the same truth and noise (+ judge offsets sd .4)
    reviews = [world.review(jj, p) for jj, p in PAIRS]
    fs = jm.fit_judge_effects(reviews, W)
    tau, win = _within_track(fs["score"], world.Q)
    out["rubric D* (123 reviews)"] = {"tau_within": tau, "winner": win, "comparisons": 0, "looks": len(reviews)}
    raw = jm.raw_mean([(r["judge"], r["project"], jm.weighted_total(r["criteria"], W)) for r in reviews])
    tau, win = _within_track(raw, world.Q)
    out["rubric raw (123 reviews)"] = {"tau_within": tau, "winner": win, "comparisons": 0, "looks": len(reviews)}
    return out


def exp_pairwise(n, noise="iid"):
    truth = _fixture_truth()
    cfgs = []
    for tsd in (None, 0.48):
        for m in (3, 5, 10, 15):
            for sel in ("random", "adaptive"):
                cfgs.append({"name": "%s truth=%s m=%d %s" % (noise, "fixture" if tsd is None else "sd%.2f" % tsd, m, sel),
                             "truth_scores": truth, "truth_sd": tsd, "m": m, "sel": sel, "clickers": 0, "noise": noise})
    if noise == "iid":
        cfgs.append({"name": "truth=sd0.48 m=10 adaptive, 3 random clickers", "truth_scores": truth, "truth_sd": 0.48,
                     "m": 10, "sel": "adaptive", "clickers": 3, "noise": noise})
    m0 = sum(truth.values()) / len(truth)
    print("fixture D* truth: sd = %.3f; per-look noise sd = %.3f" % (
        math.sqrt(sum((v - m0) ** 2 for v in truth.values()) / 39),
        World(0, PROJECTS, JUDGES, **SCENARIOS["S1 leniency sd .4 (ICC~.34)"]).sigma_tot))
    print("\n| config | estimator | within-track tau | track-winner acc | comparisons | judge-project looks |")
    print("|---|---|---|---|---|---|")
    allres = {}
    with Pool(8) as pool:
        for cfg in cfgs:
            rows = pool.map(_pairwise_one, [(s, cfg) for s in range(n)])
            for est in rows[0]:
                vals = {k: sum(r[est][k] for r in rows) / n for k in rows[0][est]}
                sdv = math.sqrt(sum((r[est]["tau_within"] - vals["tau_within"]) ** 2 for r in rows) / (n - 1))
                if est.startswith("rubric") and not cfg["name"].endswith("m=3 random"):  # same for all cfgs
                    continue
                print("| %s | %s | %.3f ±%.3f | %.3f | %.0f | %.0f |" % (
                    cfg["name"], est, vals["tau_within"], 1.96 * sdv / math.sqrt(n), vals["winner"],
                    vals["comparisons"], vals["looks"]))
                allres["%s | %s" % (cfg["name"], est)] = vals
    return allres


def exp_flat(n):
    """Paired effect of the flat-judge rule: D* (REML + flat excluded) minus D (REML, flat kept)."""
    out = {}
    with Pool(8) as pool:
        for scen in ("S0 no judge effects", "S1 leniency sd .4 (ICC~.34)", "S4 strong leniency sd .7"):
            rows = pool.map(_norm_one, [(s, scen) for s in range(n)])
            print("\n%s (n=%d): D* minus D k=REML, paired mean ±95%% CI" % (scen, n))
            for k in ("tau", "rank_err_top5", "top3_exact", "first", "health_first"):
                d = [r["D* REML+flat"][k] - r["D k=REML"][k] for r in rows]
                mu = sum(d) / n
                sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (n - 1))
                print("   %-14s %+.4f ±%.4f" % (k, mu, 1.96 * sd / math.sqrt(n)))
                out["%s|%s" % (scen, k)] = (mu, 1.96 * sd / math.sqrt(n))
            # also z-score vs D* and EB vs D*
            for m in ("B z-score", "E1 EB-shrunk"):
                d = [r[m]["tau"] - r["D* REML+flat"]["tau"] for r in rows]
                mu = sum(d) / n
                sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (n - 1))
                print("   tau(%s) - tau(D*) %+.4f ±%.4f" % (m, mu, 1.96 * sd / math.sqrt(n)))
    return out


if __name__ == "__main__":
    which = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
    fn = {"normalization": exp_normalization, "design": exp_design, "adaptive": exp_adaptive,
          "pairwise": exp_pairwise, "flat": exp_flat}[which]
    res = fn(n, sys.argv[3]) if len(sys.argv) > 3 else fn(n)
    with open("results_%s%s.json" % (which, ("_" + sys.argv[3].split()[0]) if len(sys.argv) > 3 else ""), "w") as fh:
        json.dump(res, fh, indent=1, default=str)
