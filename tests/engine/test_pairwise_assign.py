import random

from hypothesis import given, settings
from hypothesis import strategies as st

from engine.assign import assign, design_diagnostics
from engine.pairwise import bt_fit, fuse_pairwise_with_prior, next_pair, order_probability


def test_bt_exists_for_all_wins_and_disconnected_graphs():
    comps = [{"a": "x", "b": "y", "outcome": 1.0}] * 5 + [{"a": "p", "b": "q", "outcome": 0.0}] * 3
    f = bt_fit(comps)
    assert f["strength"]["x"] > f["strength"]["y"] and f["strength"]["q"] > f["strength"]["p"]
    assert all(abs(v) < 10 for v in f["strength"].values())


def test_bt_ties_are_half_wins():
    f = bt_fit([{"a": "x", "b": "y", "outcome": 0.5}] * 4)
    assert abs(f["strength"]["x"] - f["strength"]["y"]) < 1e-9


def test_next_pair_never_repeats_for_a_judge():
    items = ["a", "b", "c", "d"]
    hist = []
    for _ in range(6):
        p = next_pair("j", {"t": items}, hist, rng=random.Random(1))
        assert p is not None
        assert frozenset(p) not in {frozenset((h["a"], h["b"])) for h in hist}
        hist.append({"a": p[0], "b": p[1], "judge": "j"})
    assert next_pair("j", {"t": items}, hist) is None


def test_fusion_moves_toward_pairwise_evidence():
    prior = {"a": 4.0, "b": 4.0}
    var = {"a": 0.1, "b": 0.1}
    fused = fuse_pairwise_with_prior(prior, var, [{"a": "b", "b": "a", "outcome": 1.0}] * 6, 0.8)
    assert fused["mean"]["b"] > fused["mean"]["a"]
    assert order_probability(fused, "b", "a") > 0.8


@settings(max_examples=60, deadline=None)
@given(n_projects=st.integers(5, 40), n_judges=st.integers(4, 30), k=st.integers(1, 4), n_tracks=st.integers(1, 5),
       seed=st.integers(0, 10_000))
def test_assign_properties(n_projects, n_judges, k, n_tracks, seed):
    rng = random.Random(seed)
    tracks = [f"t{i}" for i in range(n_tracks)]
    projects = [{"id": f"p{i}", "track": rng.choice(tracks)} for i in range(n_projects)]
    judges = [{"id": f"j{i}", "tracks": rng.sample(tracks, rng.randint(1, n_tracks))} for i in range(n_judges)]
    conflicts = {(f"j{rng.randrange(n_judges)}", f"p{rng.randrange(n_projects)}") for _ in range(n_projects // 3)}
    res = assign(projects, judges, k=k, conflicts=conflicts, seed=seed)
    tracks_of = {j["id"]: set(j["tracks"]) for j in judges}
    ptrack = {p["id"]: p["track"] for p in projects}
    cap = -(-k * n_projects // n_judges) + 1
    for pid, js in res["assignment"].items():
        assert len(js) == len(set(js)), "no judge twice on a project"
        for j in js:
            assert (j, pid) not in conflicts, "conflicts are never assigned"
            assert ptrack[pid] in tracks_of[j], "judges only see their tracks"
    assert all(v <= cap for v in res["load"].values()), "capacity respected"
    filled = sum(len(v) for v in res["assignment"].values())
    assert filled + len(res["unfilled"]) == k * n_projects, "every slot is either filled or reported"


def test_assign_connects_a_single_track_design():
    projects = [{"id": f"p{i}", "track": "t"} for i in range(40)]
    judges = [{"id": f"j{i}", "tracks": ["t"]} for i in range(30)]
    res = assign(projects, judges, k=3, seed=1)
    pairs = [(j, p) for p, js in res["assignment"].items() for j in js]
    assert design_diagnostics(pairs)["components"] == 1
