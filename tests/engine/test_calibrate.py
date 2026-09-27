"""Engine correctness: the estimator is what JUDGING.md says it is."""

import json
import math
import random
from pathlib import Path

import pytest

from engine.calibrate import design, explain_project, fit_additive, fit_judge_effects, gls_pieces
from engine.fixtures import from_dogfood_fixture
from engine.linalg import chol_solve, cholesky
from engine.pipeline import compute
from engine.prepare import detect_flat_judges, find_duplicates

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "fixtures.json").read_text())
W = {"functionality": 1.0, "quality": 1.0, "innovation": 1.0}


@pytest.fixture(scope="module")
def inp():
    return from_dogfood_fixture(FIXTURE)[0]


@pytest.fixture(scope="module")
def fit(inp):
    return fit_judge_effects(inp["reviews"], W)


def test_duplicate_detected_and_latest_on_time_copy_is_canonical():
    dups = find_duplicates(FIXTURE["projects"], FIXTURE["event"]["submissions_close"])
    assert dups == [{"canonical": "prj_41", "superseded": ["prj_07"], "reason": "same team and same repository or title"}]


def test_same_judge_on_both_copies_counts_once(inp):
    pairs = [(r["judge"], r["project"]) for r in inp["reviews"]]
    assert len(pairs) == len(set(pairs)) == 123
    assert not any(r["project"] == "prj_07" for r in inp["reviews"])


def test_flat_judge_detected(inp):
    assert set(detect_flat_judges(inp["reviews"])) == {"jdg_07"}


def test_als_equals_direct_gls_solve(fit):
    obs = fit["used_obs_totals"]
    projects, A, c, _, _ = gls_pieces(obs, 1.0 / fit["k"])
    beta = chol_solve(cholesky(A), c)
    assert max(abs(beta[i] - fit["score"][p]) for i, p in enumerate(projects)) < 1e-10


def test_explanations_sum_exactly(fit):
    for p in fit["score"]:
        e = explain_project(fit, p)
        assert e["exact"], p
        assert abs(e["raw_mean"] + sum(l["contribution"] for l in e["lines"]) - e["final"]) < 1e-9


def test_offsets_sum_to_zero_per_criterion(fit):
    for c, pc in fit["per_criterion"].items():
        assert abs(sum(pc["offset"].values())) < 1e-9


def test_k_infinity_is_raw_mean():
    obs = [("a", "p", 3.0), ("b", "p", 5.0), ("a", "q", 2.0)]
    s, b, _ = fit_additive(obs, math.inf)
    assert s == {"p": 4.0, "q": 2.0} and all(v == 0 for v in b.values())


def test_shifting_one_judge_does_not_change_the_ranking():
    """A harsh judge is corrected away: adding a constant to one judge's scores moves every
    project by the same amount (the reference is the average judge), so every score
    difference and the ranking are unchanged."""
    rng = random.Random(3)
    judges, projects = [f"j{i}" for i in range(8)], [f"p{i}" for i in range(12)]
    truth = {p: rng.gauss(3, 0.6) for p in projects}
    reviews = []
    for i, p in enumerate(projects):
        for j in {judges[i % 8], judges[(i + 3) % 8], judges[(i + 5) % 8]}:
            reviews.append({"judge": j, "project": p, "criteria": {"x": truth[p] + rng.gauss(0, 0.3)}})
    base = fit_judge_effects(reviews, {"x": 1}, k=1e-9, flat_rule=False)
    shifted = [dict(r, criteria={"x": r["criteria"]["x"] + (1.5 if r["judge"] == "j2" else 0)}) for r in reviews]
    after = fit_judge_effects(shifted, {"x": 1}, k=1e-9, flat_rule=False)
    deltas = [after["score"][p] - base["score"][p] for p in projects]
    assert max(deltas) - min(deltas) < 1e-6
    rank = lambda f: sorted(projects, key=lambda p: -f["score"][p])
    assert rank(base) == rank(after)


def test_flat_judge_has_zero_weight(fit):
    e = explain_project(fit, "prj_19")
    flat = [l for l in e["lines"] if l["judge"] == "jdg_07"]
    assert flat and flat[0]["tag"] == "flat"
    assert round(e["final"], 3) == 3.333


def test_single_review_judge_is_shrunk_not_undefined(fit):
    assert "jdg_01" in fit["offset"] and abs(fit["offset"]["jdg_01"]) < 0.2


def test_pipeline_is_deterministic(inp):
    a, b = compute(inp, heavy=False), compute(inp, heavy=False)
    assert a["output_hash"] == b["output_hash"]
    assert a["signal"]["verdict"] == "no_signal" and a["signal"]["icc1"] < 0.05
    assert all(e["tied_with_next"] for e in a["entries"][:-1])  # the fixture separates nothing


def test_weights_are_linear_normalize_then_weight(inp):
    f1 = fit_judge_effects(inp["reviews"], {"functionality": 2, "quality": 1, "innovation": 1}, k=5)
    for p, s in f1["score"].items():
        crit = f1["criterion_score"][p]
        assert abs(s - (2 * crit["functionality"] + crit["quality"] + crit["innovation"]) / 4) < 1e-12
