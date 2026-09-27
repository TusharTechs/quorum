"""One call that turns locked method + reviews into a complete, explainable ranking run.

The web app, the worker, the CLI and the exported-bundle verifier all call `compute()`,
so there is exactly one implementation of the maths. The output is plain JSON; its
canonical form is hashed and stored with the run.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict

from . import ENGINE_VERSION
from .bundle import canonical_json, sha256_hex
from .calibrate import explain_project, fit_judge_effects, rank_order
from .compare import rank_agreement, raptors_classic, raw_scores, spearman, zscore_scores
from .pairwise import bt_fit, induced_comparisons
from .prepare import weighted_total
from .uncertainty import (leave_one_judge_out, project_uncertainty, signal_check,
                          simulate_rankings, tie_groups, weight_sensitivity)

DEFAULT_METHOD = {
    "calibration": "offset-reml/v1",
    "flat_rule": True,
    "flat_min_reviews": 3,
    "k": None,
    "prize_n": 3,
    "tie_z": 1.96,
    "legacy_k": 10,
    "draws": 4000,
    "permutations": 2000,
}


def input_hash(inp: dict) -> str:
    return sha256_hex(inp)


def _std(v):
    if len(v) < 2:
        return 0.0
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def compute(inp: dict, *, heavy: bool = True) -> dict:
    """inp = {"method": {...}, "criteria": [{"key","weight"}], "projects": [{"id","track",...}],
              "reviews": [{"judge","project","criteria":{...}}]}
    heavy=False skips leave-one-judge-out and weight sensitivity (for fast previews)."""
    method = {**DEFAULT_METHOD, **(inp.get("method") or {})}
    weights = {c["key"]: float(c["weight"]) for c in inp["criteria"] if float(c["weight"]) > 0}
    tracks = {p["id"]: p.get("track") for p in inp["projects"]}
    known = set(tracks)
    reviews = [r for r in inp["reviews"] if r["project"] in known and r.get("criteria")]
    h = input_hash(inp)
    seed = int(h[:12], 16)
    out: dict = {"engine": ENGINE_VERSION, "input_hash": h, "method": method,
                 "weights": weights, "n_projects": len(known), "n_reviews": len(reviews)}
    if len({r["project"] for r in reviews}) < 2 or len(reviews) < 3:
        out.update({"status": "insufficient", "entries": [], "judges": [], "explanations": {}})
        return _finish(out)

    calib = method["calibration"]
    fit = fit_judge_effects(reviews, weights, k=method["k"], flat_rule=method["flat_rule"],
                            flat_min_reviews=method["flat_min_reviews"])
    raw = raw_scores(reviews, weights)
    classic = raptors_classic(reviews, weights, k=method["legacy_k"])
    zs, z_degenerate = zscore_scores(reviews, weights)
    primary = {"offset-reml/v1": fit["score"], "raw": raw, "raptors-classic": classic}.get(calib, fit["score"])

    unc = project_uncertainty(fit, z=method["tie_z"])
    # uncertainty is computed for the calibrated fit; for other primaries we re-order
    groups = tie_groups(unc)
    sims = simulate_rankings(fit, unc, top_n=method["prize_n"], draws=method["draws"],
                             seed=seed, tracks=tracks)
    sig = signal_check(reviews, weights, permutations=method["permutations"], seed=seed)

    ranks = rank_order(primary)
    raw_rank = rank_order(raw)
    classic_rank = rank_order(classic)
    z_rank = rank_order(zs)
    track_rank = {}
    by_track = defaultdict(dict)
    for p, s in primary.items():
        by_track[tracks.get(p)][p] = s
    for t, d in by_track.items():
        track_rank.update(rank_order(d))
    adj = {a["higher"]: a for a in unc["adjacent"]}
    n_rev = fit["n_reviews"]
    entries = []
    for p in sorted(primary, key=lambda p: (ranks[p], p)):
        a = adj.get(p)
        lo, hi = sims["rank_interval"].get(p, (None, None))
        entries.append({
            "project": p,
            "track": tracks.get(p),
            "rank": ranks[p],
            "track_rank": track_rank.get(p),
            "score": primary[p],
            "calibrated": fit["score"].get(p),
            "raw": raw.get(p),
            "classic": classic.get(p),
            "zscore": zs.get(p),
            "raw_rank": raw_rank.get(p),
            "classic_rank": classic_rank.get(p),
            "zscore_rank": z_rank.get(p),
            "move_vs_raw": (raw_rank.get(p) or 0) - ranks[p],
            "criteria": fit["criterion_score"].get(p, {}),
            "se": unc["se"].get(p),
            "n_reviews": n_rev.get(p, 0),
            "n_effective": unc["effective_n"].get(p),
            "p_prize": sims["p_top"].get(p),
            "p_first": sims["p_first"].get(p),
            "p_track_first": sims["p_track_first"].get(p),
            "rank_lo": lo,
            "rank_hi": hi,
            "tie_group": groups.get(p),
            "p_above_next": a["p_higher_better"] if a else None,
            "tied_with_next": a["tie"] if a else False,
            "flat_only": p in fit["orphan_projects"],
        })
    explanations = {p: explain_project(fit, p) for p in fit["score"]}

    # judge cards: descriptive statistics and signals, never verdicts
    per_j = defaultdict(list)
    for r in reviews:
        t = weighted_total(r["criteria"], weights)
        if t is not None:
            per_j[r["judge"]].append((r["project"], t))
    by_p = defaultdict(set)
    for r in reviews:
        by_p[r["project"]].add(r["judge"])
    flat = fit["flat_judges"]
    judges = []
    for j in sorted(per_j):
        vals = [t for _, t in per_j[j]]
        info = len(vals) - sum(1.0 / len(by_p[p]) for p, _ in per_j[j])
        # agreement with co-judges: Spearman(own totals, calibrated score of those projects)
        agree = spearman(vals, [fit["score"][p] for p, _ in per_j[j]]) if len(vals) >= 4 else None
        signals = []
        if j in flat:
            signals.append("flat")
        elif len(vals) >= 4 and _std(vals) < 0.35:
            signals.append("low_spread")
        if abs(fit["offset"].get(j, 0.0)) >= 0.5:
            signals.append("strong_offset")
        if info < 1.5:
            signals.append("low_information")
        if agree is not None and agree < 0:
            signals.append("low_agreement")
        judges.append({"judge": j, "n": len(vals), "mean": sum(vals) / len(vals), "sd": _std(vals),
                       "offset": fit["offset"].get(j, 0.0), "info": info, "agreement": agree,
                       "flat": j in flat, "signals": signals})

    induced = bt_fit(induced_comparisons(fit["used_obs_totals"]))
    agreement = {
        "raw_vs_calibrated": rank_agreement(raw, fit["score"]).get("overall"),
        "zscore_vs_calibrated": rank_agreement(zs, fit["score"]).get("overall"),
        "classic_vs_calibrated": rank_agreement(classic, fit["score"]).get("overall"),
        "ordinal_vs_calibrated": rank_agreement(induced["strength"], fit["score"]).get("overall"),
    }
    out.update({
        "status": "ok",
        "k": fit["k"],
        "k_source": fit["k_source"],
        "reml": ({"k": fit["reml"]["k"], "k_interval_95": list(fit["reml"]["k_interval_95"]),
                  "sigma2": fit["reml"]["sigma2"], "tau2": fit["reml"]["tau2"]} if fit["reml"] else None),
        "sigma2": unc["sigma2"],
        "signal": sig,
        "flat_judges": flat,
        "zscore_degenerate_judges": z_degenerate,
        "orphan_projects": fit["orphan_projects"],
        "entries": entries,
        "explanations": explanations,
        "judges": judges,
        "agreement": agreement,
        "adjacent": unc["adjacent"],
    })
    if heavy:
        out["loo"] = leave_one_judge_out(reviews, weights, fit["k"], prize_n=method["prize_n"],
                                         flat_rule=method["flat_rule"])
        out["sensitivity"] = weight_sensitivity(reviews, weights, fit["k"], prize_n=method["prize_n"],
                                                flat_rule=method["flat_rule"])
    return _finish(out)


def _finish(out: dict) -> dict:
    """Return the canonical, JSON-safe form (floats rounded, inf -> "inf") plus its hash,
    so what is stored, exported and recomputed is byte-identical."""
    clean = json.loads(canonical_json({k: v for k, v in out.items() if k != "output_hash"}))
    clean["output_hash"] = sha256_hex(clean)
    return clean


def verify(inp: dict, expected_output_hash: str) -> dict:
    """Recompute a run from its canonical input and compare output hashes."""
    res = compute(inp, heavy=True)
    return {"match": res["output_hash"] == expected_output_hash, "output_hash": res["output_hash"],
            "input_hash": res["input_hash"]}


__all__ = ["compute", "verify", "input_hash", "canonical_json", "DEFAULT_METHOD"]
