"""The normalization and allocation proofs, as tests (fewer simulations than docs/proof,
same design: the DOGFOOD fixture's exact 123 judge-project pairs, 3 integer criteria,
jdg_07 flat). Ties in any method's scores are broken by the same random jitter, because
Kendall's tau-b otherwise rewards the many exact ties of raw integer means."""

import json
import random
import statistics as st
from pathlib import Path

from engine.calibrate import fit_judge_effects
from engine.compare import kendall_tau_b, raw_scores, zscore_scores
from engine.fixtures import from_dogfood_fixture

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "fixtures.json").read_text())
DESIGN = from_dogfood_fixture(FIXTURE)[0]["reviews"]
C = ["functionality", "quality", "innovation"]
W = {c: 1 for c in C}


def simulate(seed, leniency_sd, noise=0.9, flat="jdg_07"):
    rng = random.Random(seed)
    ps = sorted({r["project"] for r in DESIGN})
    js = sorted({r["judge"] for r in DESIGN})
    th = {p: rng.gauss(0, 0.45) for p in ps}
    d = {(p, c): rng.gauss(0, 0.3) for p in ps for c in C}
    b = {j: rng.gauss(0, leniency_sd) for j in js}
    reviews = []
    for r in DESIGN:
        j, p = r["judge"], r["project"]
        crit = {c: 3.0 if j == flat else float(min(5, max(1, round(3.4 + th[p] + d[(p, c)] + b[j] + rng.gauss(0, noise)))))
                for c in C}
        reviews.append({"judge": j, "project": p, "criteria": crit})
    return {p: th[p] + st.mean(d[(p, c)] for c in C) for p in ps}, reviews


def tau(truth, scores, seed):
    rng = random.Random(seed)
    ps = sorted(truth)
    jitter = {p: rng.random() * 1e-9 for p in ps}
    return kendall_tau_b([truth[p] for p in ps], [scores[p] + jitter[p] for p in ps])


def gains(leniency_sd, n=80):
    vs_raw, vs_z = [], []
    for s in range(n):
        truth, rv = simulate(s, leniency_sd)
        cal = fit_judge_effects(rv, W)["score"]
        z, _ = zscore_scores(rv, W)
        vs_raw.append(tau(truth, cal, s) - tau(truth, raw_scores(rv, W), s))
        vs_z.append(tau(truth, cal, s) - tau(truth, z, s))
    return st.mean(vs_raw), st.mean(vs_z)


def test_calibration_beats_raw_and_zscore_when_judges_differ():
    raw, z = gains(0.6)
    assert raw > 0, f"calibration should beat the raw mean when judges differ (got {raw:+.3f})"
    assert z > 0, f"calibration should beat per-judge z-scores (got {z:+.3f})"


def test_calibration_costs_nothing_when_judges_agree_on_level():
    raw, z = gains(0.0, n=60)
    assert raw > -0.01, f"with no leniency differences calibration must not hurt (got {raw:+.3f})"
    assert z > 0, "per-judge z-scores are worse than calibration even then"


def test_focus_allocation_targets_uncertain_prize_membership():
    from engine.allocate import plan_next_round

    fit = {"flat_judges": {}, "reviews": []}
    p_prize = {"a": 0.5, "b": 0.02, "c": 0.97, "d": 0.45}
    se = {"a": 0.4, "b": 0.4, "c": 0.4, "d": 0.4}
    judges = [{"id": f"j{i}", "tracks": None} for i in range(4)]
    plan = plan_next_round(fit, p_prize, se, 0.6, 3, judges, {j["id"]: 5 for j in judges}, existing=set())
    targets = [r["project"] for r in plan]
    assert set(targets) <= {"a", "d"} and "b" not in targets and "c" not in targets
