"""Comparative (pairwise) judging per track: the optional second method (spec 9.7).

An organizer can switch comparative judging on for a track. Each judge then compares pairs
drawn from the projects they have already reviewed in that track (their own batch), so every
choice is an informed one and the judge's leniency cancels inside the comparison. Pairs come
from engine.pairwise.next_pair: never a repeat for the judge, uncertain pairs first, the Gavel
chain (the project just seen stays on screen) and pairs that join disconnected parts of the
comparison graph. Sides are shuffled per judge and pair so position bias averages out.

The Bradley-Terry fit is shown to organizers beside the calibrated rubric ranking, with
Kendall tau and flagged disagreements. It is advisory: it never changes the official ranking.
The one place pairwise evidence is fused with the rubric is a tie-break round (results.decide).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from django.db import transaction

from engine.compare import kendall_tau_b
from engine.pairwise import bt_fit, bt_standard_errors, components, next_pair
from quorum.audit import service as audit
from quorum.core.mail import queue_email
from quorum.events.models import EventRole, Project, Track
from quorum.policy.errors import Conflict, NotFound

from . import ops
from .models import Assignment, PairwiseComparison

TARGET = 10     # comparisons asked of each judge per track (spec: worth it at 5-10 per judge)
MIN_POOL = 3    # below this a judge's batch in the track is too small to compare
DRAWS = 2000    # rank-interval simulation
OUTCOME = {"a": 1.0, "b": 0.0, "tie": 0.5}


def get_track(event, ref: str) -> Track:
    t = Track.objects.filter(event=event, ref=ref).first()
    if not t:
        raise NotFound("No such track.")
    return t


def _jref(role) -> str:
    return role.ref or str(role.pk)


def pool(role, track) -> list[str]:
    """Projects this judge may compare in this track: the ones they have reviewed."""
    return sorted(set(
        Assignment.objects.filter(judge_role=role, event_id=track.event_id, status="submitted", project__track=track,
                                  project__status="submitted", project__duplicate_of__isnull=True)
        .values_list("project__ref", flat=True)))


def history(track) -> list[dict]:
    qs = (PairwiseComparison.objects.filter(event_id=track.event_id, tiebreak__isnull=True,
                                            project_a__track=track, project_b__track=track)
          .select_related("project_a", "project_b", "judge_role").order_by("created_at", "id"))
    return [{"a": c.project_a.ref, "b": c.project_b.ref, "judge": _jref(c.judge_role), "outcome": OUTCOME[c.outcome]}
            for c in qs]


def judge_tracks(event, role) -> list[dict]:
    """The comparative tracks a judge takes part in, with progress and the next pair."""
    out = []
    for jt in role.judge_tracks.select_related("track").filter(track__pairwise=True).order_by("track__position"):
        out.append(next_for(role, jt.track))
    return out


def next_for(role, track) -> dict:
    refs = pool(role, track)
    hist = history(track)
    me = _jref(role)
    mine = [h for h in hist if h["judge"] == me]
    pairs = len(refs) * (len(refs) - 1) // 2
    state = {"track": track, "pool": refs, "done": len(mine), "target": min(TARGET, pairs), "pairs": pairs,
             "pair": None, "ready": len(refs) >= MIN_POOL}
    if not state["ready"]:
        return state
    strength = bt_fit(hist, items=refs)["strength"] if hist else None
    last = None
    if mine:  # keep the project the judge preferred last time on screen (one new project to read)
        last = mine[-1]["a"] if mine[-1]["outcome"] >= 0.5 else mine[-1]["b"]
    pair = next_pair(me, {track.ref: refs}, hist, strength=strength, last_seen=last, rng=random.Random(len(hist)))
    if pair:
        a, b = pair
        if random.Random(f"{me}|{a}|{b}").random() < 0.5:
            a, b = b, a
        state["pair"] = (a, b)
    return state


@transaction.atomic
def set_mode(event, actor, track, enabled: bool) -> Track:
    if hasattr(event, "publication"):
        raise Conflict("Results are locked; the judging method can no longer change.", code="results_locked")
    enabled = bool(enabled)
    if track.pairwise == enabled:
        return track
    track.pairwise = enabled
    track.save(update_fields=["pairwise"])
    audit.record("PAIRWISE_MODE_CHANGED",
                 f"Comparative judging {'switched on' if enabled else 'switched off'} for track {track.name}",
                 event=event, actor=actor, actor_role="organizer", target=track,
                 data={"track": track.ref, "enabled": enabled})
    if enabled:
        roles = EventRole.objects.filter(event=event, role="judge", judge_tracks__track=track).select_related("user")
        for r in roles:
            n = len(pool(r, track))
            if n < MIN_POOL:
                continue
            queue_email(r.user.email, f"{event.name}: optional comparisons for {track.name}",
                        f"Hello {r.user.display},\n\nThe organizers switched on comparative judging for {track.name}. "
                        f"You will see two projects you have already reviewed and choose the stronger one; about "
                        f"{min(TARGET, n * (n - 1) // 2)} choices, a minute each. Your rubric scores are unchanged.\n\n"
                        f"{ops.judge_link(event)}\n")
    return track


def _rank_intervals(strength, se, items, draws=DRAWS, seed=0):
    rng = random.Random(seed)
    lo = {i: len(items) for i in items}
    hi = {i: 1 for i in items}
    hits = defaultdict(list)
    for _ in range(draws):
        x = {i: strength[i] + se.get(i, 0.0) * rng.gauss(0.0, 1.0) for i in items}
        for r, i in enumerate(sorted(items, key=lambda k: -x[k]), 1):
            hits[i].append(r)
    for i in items:
        rs = sorted(hits[i])
        lo[i] = rs[int(0.05 * (len(rs) - 1))]
        hi[i] = rs[int(0.95 * (len(rs) - 1))]
    return lo, hi


def leaderboard(event, track, run=None) -> dict:
    """Bradley-Terry order for a track, side by side with the calibrated rubric order."""
    from quorum.results.service import latest_run

    hist = history(track)
    compared = sorted({h["a"] for h in hist} | {h["b"] for h in hist})
    titles = {p.ref: p for p in Project.objects.filter(event=event, ref__in=compared).select_related("team")}
    base = {"track": {"ref": track.ref, "name": track.name, "enabled": track.pairwise}, "comparisons": len(hist),
            "judges": len({h["judge"] for h in hist}), "projects": [], "tau": None, "components": 0,
            "flagged": 0, "per_judge": {}}
    if not hist:
        return base
    fit = bt_fit(hist, items=compared)
    se = bt_standard_errors(hist, fit)
    mean = sum(fit["strength"].values()) / len(compared)
    strength = {i: fit["strength"][i] - mean for i in compared}
    lo, hi = _rank_intervals(strength, se, compared, seed=len(hist))
    record = {i: [0.0, 0.0, 0] for i in compared}  # wins, losses, ties
    per_judge = defaultdict(int)
    for h in hist:
        per_judge[h["judge"]] += 1
        if h["outcome"] == 0.5:
            record[h["a"]][2] += 1
            record[h["b"]][2] += 1
        else:
            w, lz = (h["a"], h["b"]) if h["outcome"] == 1.0 else (h["b"], h["a"])
            record[w][0] += 1
            record[lz][1] += 1
    comp = components(compared, [(h["a"], h["b"]) for h in hist])
    run = run or latest_run(event)
    entries = {e["project"]: e for e in (run.output.get("entries") or [])} if run else {}
    on_rubric = [i for i in compared if i in entries and entries[i].get("calibrated") is not None]
    rubric_rank = {i: r for r, i in enumerate(sorted(on_rubric, key=lambda i: -entries[i]["calibrated"]), 1)}
    bt_order = sorted(compared, key=lambda i: (-strength[i], i))
    rows = []
    for rank, i in enumerate(bt_order, 1):
        w, lz, t = record[i]
        rr = rubric_rank.get(i)
        n = int(w + lz + t)
        flag = rr is not None and n >= 2 and not (lo[i] <= rr <= hi[i])
        rows.append({"ref": i, "title": titles[i].title if i in titles else i,
                     "team": titles[i].team.name if i in titles else "", "rank": rank, "rank_lo": lo[i],
                     "rank_hi": hi[i], "strength": strength[i], "se": se.get(i), "wins": int(w), "losses": int(lz),
                     "ties": t, "n": n, "p_vs_typical": 1.0 / (1.0 + math.exp(-strength[i])),
                     "rubric_rank": rr, "calibrated": entries.get(i, {}).get("calibrated"),
                     "delta": (rr - rank) if rr is not None else None, "flag": flag})
    tau = None
    if len(on_rubric) >= 3:
        tau = kendall_tau_b([strength[i] for i in on_rubric], [entries[i]["calibrated"] for i in on_rubric])
    base.update({"projects": rows, "tau": tau, "components": len(set(comp.values())),
                 "flagged": sum(1 for r in rows if r["flag"]), "per_judge": dict(sorted(per_judge.items())),
                 "run": run.output_hash if run else None})
    return base


def event_board(event) -> list[dict]:
    """Every track that is in comparative mode or already holds comparisons."""
    tracks = list(event.tracks.all())
    with_data = set(PairwiseComparison.objects.filter(event=event, tiebreak__isnull=True)
                    .values_list("project_a__track_id", flat=True))
    return [leaderboard(event, t) for t in tracks if t.pairwise or t.pk in with_data]
