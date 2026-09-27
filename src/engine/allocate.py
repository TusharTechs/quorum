"""Focus rounds: spend spare judge time where it can still change who wins.

After every project has its baseline reviews, most of the ranking is already settled
for the purpose that matters (prizes). Extra reviews on a project that is certainly in,
or certainly out, of the prize set change nothing. This planner sends each extra review
to the project whose prize membership is most uncertain AND whose score is least
precise, with diminishing returns as reviews are planned.

Simulation on the fixture's design (400 seeds, ICC 0.31): with the same 120 reviews,
"2 each + two targeted rounds of 20" picks the true winner 43.5% of the time vs 33.5%
for uniform 3 each; see JUDGING.md.
"""

from __future__ import annotations

import random
from collections import defaultdict


def plan_next_round(fit, p_prize, se, sigma2, budget, judges, capacity, project_track=None,
                    existing=None, conflicts=(), max_per_project=2, seed=0):
    """Plan `budget` extra (judge, project) reviews.

    fit       : calibrate.fit_judge_effects output (for flat judges + existing reviews)
    p_prize   : {project: P(project wins a prize)}
    se        : {project: standard error of its calibrated score}
    sigma2    : residual variance of one review
    judges    : [{"id","tracks": [...] | None}]
    capacity  : {judge: remaining review slots}
    project_track: {project: track}; None = no track constraint
    existing  : set of (judge, project) already reviewed or assigned
    conflicts : set of (judge, project) that are forbidden

    Greedy on the expected value of one more review:
        gain_p = P_p (1 - P_p) * v_p^2 / (v_p + sigma^2),   v_p = se_p^2
    After planning a review, v_p <- 1 / (1/v_p + 1/sigma^2) (diminishing returns).
    The judge for a slot is eligible (track, no conflict, capacity, not flat, has not seen
    the project), preferring the judge whose own offset rests on the most evidence.
    Returns [{"judge","project","p_prize","se_before","se_after"}].
    """
    rng = random.Random(seed)
    s2 = max(sigma2, 1e-9)
    v = {p: max(se.get(p, 1.0), 1e-9) ** 2 for p in p_prize}
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
        for p, P in p_prize.items():
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
                    and (tracks_of[j] is None or project_track is None or project_track.get(p) in tracks_of[j])]
            if not elig:
                dead.add(p)
                continue
            j = max(elig, key=lambda jj: (info.get(jj, 0.0), rng.random()))
            before = v[p] ** 0.5
            existing.add((j, p))
            cap[j] -= 1
            extra[p] += 1
            v[p] = 1.0 / (1.0 / v[p] + 1.0 / s2)
            plan.append({"judge": j, "project": p, "p_prize": p_prize[p],
                         "se_before": before, "se_after": v[p] ** 0.5})
            placed = True
            break
        if not placed:
            break
    return plan
