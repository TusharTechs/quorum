"""Run every method on the official fixture and print report-ready tables.

usage: python3 analysis_fixture.py [path/to/fixtures.json]  > results_fixture.txt
"""
import json
import math
import random
import sys
from collections import defaultdict

import judging_math as jm

PATH = sys.argv[1] if len(sys.argv) > 1 else "../fixtures.json"
fx = jm.load_fixture(PATH)
TITLE = {p["id"]: p["title"] for p in fx["projects"]}
TRACK = {p["id"]: p["track"] for p in fx["projects"]}
TNAME = {t["id"]: t["name"] for t in fx["tracks"]}
ALT = {"functionality": 0.5, "quality": 0.25, "innovation": 0.25}
WEIGHTS = {"equal": jm.EQUAL_WEIGHTS, "alt(.5/.25/.25)": ALT}
OUT = {}


def h(t):
    print("\n" + "=" * 100 + "\n" + t + "\n" + "=" * 100)


def lab(p):
    return "%s %s" % (p, TITLE[p])


# ----------------------------------------------------------------------------------------------
h("1. DATA PREP: duplicates, flat judges, test-retest")
projects, reviews, log = jm.prepare_reviews(fx, duplicate_policy="merge")
for e in log:
    if e["event"] == "duplicate":
        print("duplicate:", e)
    if e["event"] == "same_judge_scored_both_copies":
        diffs = []
        for c in e["cases"]:
            a, b = c["scores"]
            ta, tb = sum(a.values()) / 3, sum(b.values()) / 3
            diffs.append(tb - ta)
            print("  test-retest %s on %s: %.2f vs %.2f (diff %+.2f)" % (c["judge"], c["project"], ta, tb, tb - ta))
        # within-judge noise sd estimate: Var(diff) = 2 sigma^2
        s2 = sum(d * d for d in diffs) / (2 * len(diffs))
        print("  test-retest sigma estimate (per-review total) = sqrt(mean(d^2)/2) = %.2f  (n=%d pairs)" % (math.sqrt(s2), len(diffs)))
        OUT["retest_sigma"] = math.sqrt(s2)
print("after merge: %d projects, %d reviews (fixture had %d / %d)" % (len(projects), len(reviews), len(fx["projects"]), len(fx["scores"])))
_, rev_keep, _ = jm.prepare_reviews(fx, duplicate_policy="keep_latest")
print("keep_latest alternative: %d reviews" % len(rev_keep))
flat = jm.detect_flat_judges(reviews)
print("flat judges:", flat)
# chance a discriminating judge gives 9 identical criterion values: empirical marginal
cnt = defaultdict(int)
tot = 0
for r in reviews:
    for v in r["criteria"].values():
        cnt[v] += 1
        tot += 1
p9 = sum((c / tot) ** 9 for c in cnt.values())
print("P(9 identical criterion values | judge draws from the pooled marginal) = %.1e" % p9)
OUT["p_flat_by_chance"] = p9

# ----------------------------------------------------------------------------------------------
h("2. DESIGN: bipartite graph, identifiability, overlap, completion")
dd = jm.design_diagnostics(reviews, projects, fx["judges"])
print("connected components (judges, projects):", dd["component_sizes"])
print("components per track:", {TNAME[t]: n for t, n in dd["track_components"].items()})
print("bridge judges (reviews per track):", dd["bridges"])
print("judge pairs sharing >=1 project: %d of %d" % (dd["judge_pairs_sharing"], dd["judge_pairs_total"]))
print("co-judges per judge:", dict(sorted(dd["co_judges"].items())))
print("offset information n_j - sum 1/n_p (effective comparisons behind each judge offset):")
for j, v in sorted(dd["offset_information"].items(), key=lambda kv: kv[1]):
    print("   %s n=%2d info=%.2f" % (j, dd["reviews_per_judge"][j], v))
rpp = dd["reviews_per_project"]
print("reviews per project histogram:", {n: sum(1 for v in rpp.values() if v == n) for n in sorted(set(rpp.values()))})
print("projects with 2 reviews:", sorted(p for p, v in rpp.items() if v == 2))
print("judge x track completion (done/track size), lowest completion first:")
rows = []
for j, row in dd["completion"].items():
    done = sum(a for a, _ in row.values())
    size = sum(b for _, b in row.values())
    rows.append((done / size, j, row))
for frac, j, row in sorted(rows)[:10]:
    print("   %s %.0f%%  %s" % (j, 100 * frac, {TNAME[t]: "%d/%d" % v for t, v in row.items()}))
OUT["design"] = {k: v for k, v in dd.items() if k in ("component_sizes", "track_components", "bridges",
                                                      "judge_pairs_sharing", "judge_pairs_total")}

# ----------------------------------------------------------------------------------------------
h("3. RELIABILITY: is there any project signal in the fixture?")
for wname, w in WEIGHTS.items():
    ic = jm.icc1(reviews, w)
    # permutation test for the one-way F (shuffle review totals across reviews)
    obs = [(r["judge"], r["project"], jm.weighted_total(r["criteria"], w)) for r in reviews]
    rng = random.Random(7)
    ys = [y for _, _, y in obs]
    F0 = ic["F"]
    ge = 0
    B = 2000
    for _ in range(B):
        rng.shuffle(ys)
        fake = [{"judge": j, "project": p, "criteria": {"x": y}} for (j, p, _), y in zip(obs, ys)]
        if jm.icc1(fake, {"x": 1})["F"] >= F0:
            ge += 1
    print("%s: ICC(1)=%.3f  F=%.3f on %s  MSB=%.3f MSW=%.3f n0=%.2f  permutation p=%.2f"
          % (wname, ic["icc1"], ic["F"], ic["df"], ic["msb"], ic["msw"], ic["n0"], ge / B))
    OUT.setdefault("icc", {})[wname] = dict(ic, perm_p=ge / B)
crit = ["functionality", "quality", "innovation"]
for a in range(3):
    for b in range(a + 1, 3):
        xa = [r["criteria"][crit[a]] for r in reviews]
        xb = [r["criteria"][crit[b]] for r in reviews]
        ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
        cv = sum((u - ma) * (v - mb) for u, v in zip(xa, xb))
        r_ = cv / math.sqrt(sum((u - ma) ** 2 for u in xa) * sum((v - mb) ** 2 for v in xb))
        print("corr(%s,%s) = %.2f" % (crit[a], crit[b], r_))
fitE = jm.fit_judge_effects(reviews, jm.EQUAL_WEIGHTS)
print("REML (flat judge excluded): sigma=%.3f tau_judge=%.3f k=%.1f 95%% k-interval=%s" % (
    math.sqrt(fitE["reml"]["sigma2"]), math.sqrt(fitE["reml"]["tau2"]), fitE["k"], fitE["reml"]["k_interval_95"]))
fitNF = jm.fit_judge_effects(reviews, jm.EQUAL_WEIGHTS, flat_rule=False)
print("REML (all judges):          sigma=%.3f tau_judge=%.3f k=%.1f 95%% k-interval=%s" % (
    math.sqrt(fitNF["reml"]["sigma2"]), math.sqrt(fitNF["reml"]["tau2"]), fitNF["k"], fitNF["reml"]["k_interval_95"]))
OUT["reml"] = {"flat_excluded": fitE["reml"], "all": fitNF["reml"]}


# ----------------------------------------------------------------------------------------------
def all_methods(revs, w, reml_k=None):
    """Return {method: {project: score}} (higher = better)."""
    obs = [(r["judge"], r["project"], jm.weighted_total(r["criteria"], w)) for r in revs]
    out = {}
    out["A raw mean"] = jm.raw_mean(obs)
    out["B z-score"], _ = jm.zscore_scores(obs)
    out["C1 robust z"], _ = jm.zscore_scores(obs, robust=True)
    out["C2 percentile"] = jm.percentile_scores(obs)
    out["D k=3"] = jm.fit_judge_effects(revs, w, k=3.0, flat_rule=False)["score"]
    fr = jm.fit_judge_effects(revs, w, k=reml_k, flat_rule=False)
    out["D k=REML"] = fr["score"]
    fs = jm.fit_judge_effects(revs, w, k=reml_k, flat_rule=True)
    out["D* REML+flat"] = fs["score"]
    out["D2 +scale"], _, _ = jm.fit_scale_model(obs, k_b=fr["k"])
    unc = jm.project_uncertainty(fs)
    out["E1 EB-shrunk"], _ = jm.eb_shrink(fs, unc)
    bt = jm.bt_fit(jm.induced_comparisons(obs), items=sorted({p for _, p, _ in obs}))
    out["E2 induced-BT"] = bt["strength"]
    return out, fs


def ranks_of(score):
    return jm.rank_order(score)


def track_ranks(score):
    out = {}
    byt = defaultdict(dict)
    for p, v in score.items():
        byt[TRACK[p]][p] = v
    for t, sc in byt.items():
        out.update(jm.rank_order(sc))
    return out


METHODS_ORDER = ["A raw mean", "B z-score", "C1 robust z", "C2 percentile", "D k=3", "D k=REML",
                 "D* REML+flat", "D2 +scale", "E1 EB-shrunk", "E2 induced-BT"]

for wname, w in WEIGHTS.items():
    h("4. METHODS ON THE FIXTURE -- weights %s" % wname)
    M, fs = all_methods(reviews, w)
    R = {m: ranks_of(s) for m, s in M.items()}
    TR = {m: track_ranks(s) for m, s in M.items()}
    raw = R["A raw mean"]
    print("k (REML, flat excluded) = %.2f; flat judges: %s" % (fs["k"], list(fs["flat_judges"])))
    print("\nTop-10 overall under D* vs raw:")
    order = sorted(M["D* REML+flat"], key=lambda p: -M["D* REML+flat"][p])
    print("| D* rank | project | track | n | raw mean | raw rank | D* score | move |")
    print("|---|---|---|---|---|---|---|---|")
    for p in order[:10]:
        print("| %d | %s | %s | %d | %.2f | %d | %.2f | %+d |" % (
            R["D* REML+flat"][p], lab(p), TNAME[TRACK[p]], fs["n_reviews"][p], M["A raw mean"][p], raw[p],
            M["D* REML+flat"][p], raw[p] - R["D* REML+flat"][p]))
    print("\nRank agreement with raw (Kendall tau-b over 40) and movement:")
    print("| method | tau vs raw | mean abs move | max abs move | #1 overall | winners that differ from raw (of 8 tracks) |")
    print("|---|---|---|---|---|---|")
    ids = sorted(M["A raw mean"])
    for m in METHODS_ORDER:
        tau = jm.kendall_tau_b([M[m][p] for p in ids], [M["A raw mean"][p] for p in ids])
        mv = [abs(R[m][p] - raw[p]) for p in ids]
        top = min(ids, key=lambda p: (R[m][p], p))
        wins_m = {TRACK[p] for p in ids if TR[m][p] == 1 and TR["A raw mean"][p] != 1}
        print("| %s | %.2f | %.1f | %d | %s | %d |" % (m, tau, sum(mv) / len(mv), max(mv), lab(top), len(wins_m)))
    print("\nTrack winners per method (ties shown with /):")
    tracks = sorted(set(TRACK[p] for p in ids))
    print("| track | " + " | ".join(METHODS_ORDER) + " |")
    print("|---|" + "---|" * len(METHODS_ORDER))
    winners = {}
    for t in tracks:
        cells = []
        for m in METHODS_ORDER:
            ws = sorted(p for p in ids if TRACK[p] == t and TR[m][p] == 1)
            cells.append("/".join(ws))
            winners.setdefault(m, {})[t] = ws
        print("| %s | %s |" % (TNAME[t], " | ".join(cells)))
    OUT.setdefault("winners", {})[wname] = winners
    print("\nFull ranking table (overall rank) for all 40 projects:")
    print("| project | track | n | raw | " + " | ".join(METHODS_ORDER) + " |")
    print("|---|---|---|---|" + "---|" * len(METHODS_ORDER))
    for p in sorted(ids, key=lambda p: R["D* REML+flat"][p]):
        print("| %s | %s | %d | %.2f | %s |" % (lab(p), TNAME[TRACK[p]][:10], fs["n_reviews"][p], M["A raw mean"][p],
                                              " | ".join(str(R[m][p]) for m in METHODS_ORDER)))
    OUT.setdefault("scores", {})[wname] = M
    OUT.setdefault("fit_k", {})[wname] = fs["k"]

    # --- jdg_07 and jdg_01 projects
    print("\nProjects touched by jdg_07 (flat) and jdg_01 (n=1): overall rank [track rank]")
    for j in ("jdg_07", "jdg_01", "jdg_23"):
        for p in sorted({r["project"] for r in reviews if r["judge"] == j}):
            print("  %s via %s: " % (lab(p), j) + ", ".join("%s %d[%d]" % (m.split()[0], R[m][p], TR[m][p]) for m in METHODS_ORDER))

# ----------------------------------------------------------------------------------------------
h("5. WHERE Z-SCORES BREAK (equal weights)")
obs = [(r["judge"], r["project"], jm.weighted_total(r["criteria"], jm.EQUAL_WEIGHTS)) for r in reviews]
byj = defaultdict(list)
for j, p, y in obs:
    byj[j].append((p, y))
for j in sorted(byj):
    ys = [y for _, y in byj[j]]
    m, sd = jm._mean_sd(ys)
    if len(ys) <= 2 or sd < 0.25:
        zs = ["%.2f" % ((y - m) / sd) if sd > 0 else "undef" for y in ys]
        print("  %s n=%d scores=%s sd=%.2f -> z=%s" % (j, len(ys), ["%.2f" % y for y in ys], sd, zs))

h("6. 'A JUDGE WHO DREW WEAK PROJECTS LOOKS HARSH': naive vs identified leniency (equal weights)")
gm = sum(y for _, _, y in obs) / len(obs)
byp = defaultdict(list)
for j, p, y in obs:
    byp[p].append((j, y))
fit = jm.fit_judge_effects(reviews, jm.EQUAL_WEIGHTS)
fit0 = jm.fit_judge_effects(reviews, jm.EQUAL_WEIGHTS, k=1e-6, flat_rule=False)
print("| judge | n | judge mean | naive offset (mean - grand) | co-judges' mean on same projects | gap vs co-judges | identified offset (k->0) | shrunk offset (k=%.1f, all judges) |" % fitNF["k"])
print("|---|---|---|---|---|---|---|---|")
rows = []
for j in sorted(byj):
    ys = [y for _, y in byj[j]]
    others = [y2 for p, _ in byj[j] for j2, y2 in byp[p] if j2 != j]
    rows.append((j, len(ys), sum(ys) / len(ys), sum(ys) / len(ys) - gm, sum(others) / len(others),
                 sum(ys) / len(ys) - sum(others) / len(others), fit0["offset"][j], fitNF["offset"][j]))
for r in sorted(rows, key=lambda r: r[3]):
    print("| %s | %d | %.2f | %+.2f | %.2f | %+.2f | %+.2f | %+.2f |" % r)
OUT["judge_table"] = rows

# ----------------------------------------------------------------------------------------------
h("7. EXPLANATIONS (equal weights, recommended D*)")
for p in ["prj_09", "prj_17", "prj_19", "prj_41"] + [q for q in sorted(fit["score"], key=lambda q: -fit["score"][q])[:3]]:
    e = jm.explain_project(fit, p)
    print(" ", jm.format_explanation(e, TITLE), "| exact:", e["exact"])
bad = [p for p in fit["score"] if not jm.explain_project(fit, p)["exact"]]
print("decomposition exact for all 40:", not bad)
fitA = jm.fit_judge_effects(reviews, ALT)
print("alt weights:", all(jm.explain_project(fitA, p)["exact"] for p in fitA["score"]))
OUT["explanations"] = {p: jm.explain_project(fit, p) for p in fit["score"]}

# ----------------------------------------------------------------------------------------------
h("8. UNCERTAINTY AND TIES (equal weights)")
for name, f in (("D* (k=%.1f)" % fit["k"], fit), ("raw (k=inf)", jm.fit_judge_effects(reviews, jm.EQUAL_WEIGHTS, k=math.inf, flat_rule=False))):
    unc = jm.project_uncertainty(f)
    print("\n%s: residual sigma=%.3f" % (name, math.sqrt(unc["sigma2"])))
    adj = unc["adjacent"]
    ties10 = sum(1 for a in adj[:9] if a["tie"])
    print("  top-10 adjacent pairs statistically tied (|diff| < 1.96 se): %d of 9" % ties10)
    print("  all adjacent pairs tied: %d of %d" % (sum(1 for a in adj if a["tie"]), len(adj)))
    for a in adj[:5]:
        print("  %s (%.2f, se %.2f, eff-n %.1f) vs %s (%.2f): diff %.2f se_diff %.2f P(higher better)=%.2f tie=%s" % (
            a["higher"], f["score"][a["higher"]], unc["se"][a["higher"]], unc["effective_n"][a["higher"]],
            a["lower"], f["score"][a["lower"]], a["diff"], a["se_diff"], a["p_higher_better"], a["tie"]))
    # separation of #1 from #k
    o = unc["order"]
    for kk in (1, 2, 4, 9, 19):
        a, b = o[0], o[kk]
        var = unc["cov"](a, a) + unc["cov"](b, b) - 2 * unc["cov"](a, b)
        d = f["score"][a] - f["score"][b]
        print("  #1 vs #%d: diff %.2f, z=%.2f" % (kk + 1, d, d / math.sqrt(var)))
    ri = jm.rank_intervals_from_cov(f, unc, draws=3000)
    print("  90% rank intervals (model-based) top-10: " + "; ".join("%s [%d-%d] P(#1)=%.2f" % (p, *ri[p]) for p in o[:10]))
    OUT.setdefault("uncertainty", {})[name] = {"ties_top10": ties10, "adjacent": adj[:10],
                                               "rank_int": {p: ri[p] for p in o[:10]},
                                               "se": unc["se"], "eff_n": unc["effective_n"]}
    # within-track top-3
    print("  within-track top-3 separation (P(#1>#2), P(#2>#3)):")
    for t in sorted(set(TRACK.values())):
        ps = [p for p in o if TRACK.get(p) == t]
        if len(ps) < 2:
            continue
        s = []
        for a, b in zip(ps[:3], ps[1:3]):
            var = unc["cov"](a, a) + unc["cov"](b, b) - 2 * unc["cov"](a, b)
            s.append("%s>%s %.2f" % (a, b, jm.norm_cdf((f["score"][a] - f["score"][b]) / math.sqrt(var))))
        print("   %-18s %s" % (TNAME[t], "; ".join(s)))
ebs, tau2p = jm.eb_shrink(fit, jm.project_uncertainty(fit))
print("\nEB estimate of true between-project variance tau_p^2 = %.4f (0 => no detectable project differences)" % tau2p)
OUT["tau2_project_eb"] = tau2p

# ----------------------------------------------------------------------------------------------
h("9. STABILITY: leave-one-judge-out and stratified bootstrap (equal weights)")
M0, _ = all_methods(reviews, jm.EQUAL_WEIGHTS)
R0 = {m: ranks_of(s) for m, s in M0.items()}
judges = sorted({r["judge"] for r in reviews})
lojo = {m: [] for m in METHODS_ORDER}
worst = {m: (0, None) for m in METHODS_ORDER}
top1_same = {m: 0 for m in METHODS_ORDER}
top1_0 = {m: min(R0[m], key=lambda p: (R0[m][p], p)) for m in METHODS_ORDER}
for j in judges:
    Mj, _ = all_methods([r for r in reviews if r["judge"] != j], jm.EQUAL_WEIGHTS)
    for m in METHODS_ORDER:
        Rj = ranks_of(Mj[m])
        ch = [abs(Rj[p] - R0[m][p]) for p in Rj]
        lojo[m].append((max(ch), sum(ch) / len(ch), j))
        if max(ch) > worst[m][0]:
            worst[m] = (max(ch), j)
        if Rj[top1_0[m]] == 1:
            top1_same[m] += 1
print("| method | LOJO max rank change | mean abs rank change (avg over the 30 refits) | most influential judge | #1 still #1 (of 30 refits) |")
print("|---|---|---|---|---|")
for m in METHODS_ORDER:
    mx = max(v[0] for v in lojo[m])
    mean = sum(v[1] for v in lojo[m]) / len(lojo[m])
    print("| %s | %d | %.2f | %s | %d |" % (m, mx, mean, worst[m][1], top1_same[m]))
OUT["lojo"] = {m: (max(v[0] for v in lojo[m]), sum(v[1] for v in lojo[m]) / len(lojo[m]), worst[m][1]) for m in METHODS_ORDER}

B = 300
rng = random.Random(11)
byp_rev = defaultdict(list)
for r in reviews:
    byp_rev[r["project"]].append(r)
boot = {m: defaultdict(list) for m in METHODS_ORDER}
for b in range(B):
    rs = []
    for p, lst in byp_rev.items():
        rs.extend(rng.choice(lst) for _ in lst)
    Mb, _ = all_methods(rs, jm.EQUAL_WEIGHTS)
    for m in METHODS_ORDER:
        Rb = ranks_of(Mb[m])
        for p, r_ in Rb.items():
            boot[m][p].append(r_)
print("\nStratified bootstrap (B=%d, reviews resampled within project): 90%% rank intervals" % B)
width = {}
for m in METHODS_ORDER:
    ws = []
    for p, rs in boot[m].items():
        rs.sort()
        ws.append(rs[int(0.95 * len(rs)) - 1] - rs[int(0.05 * len(rs))])
    width[m] = sum(ws) / len(ws)
    print("  %-14s mean 90%% rank-interval width = %.1f ranks (of 40)" % (m, width[m]))
m = "D* REML+flat"
print("\n  D* top-10 with bootstrap intervals:")
for p in sorted(R0[m], key=lambda p: R0[m][p])[:10]:
    rs = sorted(boot[m][p])
    print("   #%d %s  [%d-%d]  P(top-3)=%.2f" % (R0[m][p], lab(p), rs[int(0.05 * len(rs))], rs[int(0.95 * len(rs)) - 1],
                                                  sum(1 for r_ in rs if r_ <= 3) / len(rs)))
OUT["boot_width"] = width
OUT["boot_top10"] = {p: sorted(boot[m][p]) for p in sorted(R0[m], key=lambda p: R0[m][p])[:10]}

# ----------------------------------------------------------------------------------------------
h("10. ADAPTIVE ALLOCATION ON THE REAL FIXTURE: what would plan_next_round ask for next?")
fitP = jm.fit_judge_effects(reviews, jm.EQUAL_WEIGHTS)
uncP = jm.project_uncertainty(fitP)
bsP = jm.prize_probabilities(fitP, uncP, top_n=3, draws=4000, seed=3)
print("P(top-3 overall), P(#1) for projects with P(top-3) > 0.05:")
for p in sorted(bsP["p_top"], key=lambda q: -bsP["p_top"][q]):
    if bsP["p_top"][p] > 0.05:
        print("   %-22s score %.2f se %.2f n=%d  P(top3)=%.2f  P(#1)=%.2f" % (
            lab(p), fitP["score"][p], uncP["se"][p], fitP["n_reviews"][p], bsP["p_top"][p], bsP["p_first"][p]))
load = defaultdict(int)
for r in reviews:
    load[r["judge"]] += 1
cap = {j["id"]: max(0, 8 - load[j["id"]]) for j in fx["judges"]}
plan = jm.plan_next_round(fitP, bsP, 10, fx["judges"], cap, project_track=TRACK, seed=1)
print("next round (budget 10, capacity 8 per judge, fixture track memberships):")
for j, p in plan:
    print("   %s -> %s (P(top3)=%.2f, n=%d)" % (j, lab(p), bsP["p_top"][p], fitP["n_reviews"][p]))
OUT["plan_demo"] = plan
OUT["prize_probs"] = {p: (bsP["p_top"][p], bsP["p_first"][p]) for p in bsP["p_top"] if bsP["p_top"][p] > 0.05}

with open("results_fixture.json", "w") as fh:
    json.dump(OUT, fh, indent=1, default=str)
print("\nwrote results_fixture.json")
