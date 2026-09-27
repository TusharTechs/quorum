"""Pillars 2 and 3: focus rounds, tie-break rounds, lock and publish.

Focus: after the baseline, spend spare judge capacity on projects whose prize membership
is still uncertain (engine.allocate). Tie-break: when a prize boundary falls inside a
statistical tie, a small panel compares the tied projects head to head; Bradley-Terry /
Thurstone evidence is fused with the rubric posterior (engine.pairwise), and the outcome
is published as a tie-break with its numbers. Both are part of the pre-registered method.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict

from django.db import transaction

from engine.allocate import plan_next_round
from engine.pairwise import fuse_pairwise_with_prior, next_pair, order_probability
from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.core.mail import queue_email
from quorum.events.models import EventRole, Project
from quorum.judging import ops
from quorum.judging.models import Assignment, PairwiseComparison
from quorum.policy.errors import Conflict, Invalid

from .models import FocusRound, RankingRun, ResultPublication, TiebreakRound
from .service import compute_run, latest_run

RESOLVE_AT = 0.80
MAX_TIE = 6


# --------------------------------------------------------------------------- ties

def boundary_ties(run: RankingRun, prize_n: int) -> list[dict]:
    """Tie groups that straddle a prize boundary (between rank k and k+1 for k <= prize_n)."""
    entries = run.output.get("entries") or []
    if not entries:
        return []
    groups = defaultdict(list)
    for e in entries:
        groups[e["tie_group"]].append(e)
    out = []
    for gid, members in sorted(groups.items(), key=lambda kv: min(m["rank"] for m in kv[1])):
        ranks = sorted(m["rank"] for m in members)
        lo, hi = ranks[0], ranks[-1]
        # Places 1..prize_n pay differently, so any boundary k (between k and k+1, k <= prize_n)
        # that falls inside a tie group is a decision the scores cannot make on their own.
        if len(members) < 2 or lo > prize_n or hi == lo:
            continue
        boundary = lo
        contenders = sorted([m for m in members if m["rank"] <= prize_n or (m.get("p_prize") or 0) > 0.05],
                            key=lambda m: m["rank"])
        if len(contenders) > MAX_TIE:
            contenders = contenders[:MAX_TIE]  # the highest-ranked contenders decide the paid places
        if len(contenders) >= 2:
            out.append({"group": gid, "boundary": boundary, "ranks": [lo, hi],
                        "projects": [m["project"] for m in contenders], "entries": contenders})
    return out


def pick_panel(event, project_refs: list[str], n: int = 3, run: RankingRun | None = None) -> list[EventRole]:
    """Three conflict-free judges; prefer judges whose own calibration rests on the most
    evidence and who are not flat scorers; break ties by current load."""
    projects = list(Project.objects.filter(event=event, ref__in=project_refs).select_related("team"))
    team_ids = {p.team_id for p in projects}
    from quorum.judging.models import Conflict as JC

    conflicted = set(JC.objects.filter(event=event, team_id__in=team_ids, status="confirmed")
                     .values_list("judge_role_id", flat=True))
    stats = {j["judge"]: j for j in (run.output.get("judges") if run else []) or []}
    load = defaultdict(int)
    for a in Assignment.objects.filter(event=event).exclude(status__in=["submitted", "reassigned", "recused"]):
        load[a.judge_role_id] += 1
    cands = []
    for r in EventRole.objects.filter(event=event, role="judge", available=True).select_related("user"):
        if r.pk in conflicted:
            continue
        s = stats.get(r.ref or str(r.pk), {})
        if s.get("flat"):
            continue
        cands.append((-(s.get("info") or 0.0), load[r.pk], r.ref or str(r.pk), r))
    cands.sort(key=lambda t: t[:3])
    return [c[3] for c in cands[:n]]


@transaction.atomic
def open_tiebreak(event, actor, project_refs: list[str], boundary: int, n_judges: int = 3) -> TiebreakRound:
    if len(project_refs) < 2 or len(project_refs) > MAX_TIE:
        raise Invalid(f"A tie-break needs 2 to {MAX_TIE} projects.")
    run = compute_run(event, actor, kind=RankingRun.Kind.PREVIEW, heavy=False)
    panel = pick_panel(event, project_refs, n_judges, run)
    if len(panel) < 2:
        raise Conflict("Not enough conflict-free judges are available for a tie-break panel.")
    tb = TiebreakRound.objects.create(event=event, run=run, boundary=boundary, created_by=getattr(actor, "user", actor))
    tb.projects.set(Project.objects.filter(event=event, ref__in=project_refs))
    tb.judge_roles.set(panel)
    for r in panel:
        queue_email(r.user.email, f"{event.name}: tie-break comparison needed",
                    f"Hello {r.user.display},\n\nThe judges' scores could not separate {len(project_refs)} "
                    f"projects at prize position {boundary}. Please compare them head to head; it takes about "
                    f"{len(project_refs) * 4} minutes.\n\n{ops.judge_link(event)}\n")
    audit.record("TIEBREAK_OPENED", f"Tie-break opened at position {boundary} for {', '.join(project_refs)} "
                                    f"(panel of {len(panel)})", event=event, actor=actor, actor_role="organizer",
                 target=tb, data={"projects": project_refs, "panel": [r.ref for r in panel], "run": run.output_hash})
    return tb


def tiebreak_next_pair(tb: TiebreakRound, role: EventRole):
    refs = list(tb.projects.order_by("ref").values_list("ref", flat=True))
    history = [{"a": c.project_a.ref, "b": c.project_b.ref, "judge": c.judge_role.ref or str(c.judge_role_id)}
               for c in tb.comparisons.select_related("project_a", "project_b", "judge_role")]
    mine = [h for h in history if h["judge"] == (role.ref or str(role.pk))]
    last = mine[-1]["b"] if mine else None
    pair = next_pair(role.ref or str(role.pk), {"tb": refs}, history, last_seen=last, rng=random.Random(len(history)))
    return pair, len(mine), len(refs) * (len(refs) - 1) // 2


def tiebreak_state(tb: TiebreakRound) -> dict:
    """Current fused ordering (usable while the round is open, and final at resolution)."""
    entries = {e["project"]: e for e in tb.run.output.get("entries", [])}
    refs = list(tb.projects.values_list("ref", flat=True))
    sigma = math.sqrt(max(tb.run.output.get("sigma2") or 0.4, 1e-6))
    prior_mean = {r: entries[r]["calibrated"] for r in refs if r in entries}
    prior_var = {r: max((entries[r].get("se") or 0.4) ** 2, 1e-4) for r in refs if r in entries}
    comps = []
    per_judge = defaultdict(int)
    for c in tb.comparisons.select_related("project_a", "project_b"):
        per_judge[c.judge_role_id] += 1
    w = 2.0 / max(len(refs), 1)
    for c in tb.comparisons.select_related("project_a", "project_b"):
        comps.append({"a": c.project_a.ref, "b": c.project_b.ref, "weight": w,
                      "outcome": {"a": 1.0, "b": 0.0, "tie": 0.5}[c.outcome]})
    fused = fuse_pairwise_with_prior(prior_mean, prior_var, comps, sigma)
    order = sorted(prior_mean, key=lambda r: -fused["mean"][r])
    adjacent = [{"higher": a, "lower": b, "p": order_probability(fused, a, b)} for a, b in zip(order, order[1:])]
    wins = defaultdict(float)
    for c in comps:
        wins[c["a"]] += c["outcome"]
        wins[c["b"]] += 1 - c["outcome"]
    return {"order": order, "adjacent": adjacent, "fused": fused["mean"], "prior": prior_mean, "wins": dict(wins),
            "comparisons": len(comps), "expected": len(refs) * (len(refs) - 1) // 2 * tb.judge_roles.count(),
            "resolved": bool(adjacent) and all(a["p"] >= RESOLVE_AT for a in adjacent)}


@transaction.atomic
def resolve_tiebreak(tb: TiebreakRound, actor, decision_order: list[str] | None = None, note: str = ""):
    st = tiebreak_state(tb)
    result = {k: v for k, v in st.items() if k != "fused"} | {"fused": st["fused"]}
    if st["resolved"] and not decision_order:
        tb.status = TiebreakRound.Status.RESOLVED
        tb.result = {**result, "method": "rubric posterior + pairwise (Thurstone), P >= 0.80 on every adjacent pair"}
        summary = "Tie-break resolved by the panel: " + " > ".join(st["order"])
    else:
        if not decision_order:
            raise Conflict("The panel's comparisons do not separate the projects with P >= 0.80. "
                           "Record an organizer decision with a reason, or declare a shared prize.")
        if sorted(decision_order) != sorted(st["order"]) or len((note or "").strip()) < 10:
            raise Invalid("Give the full order of the tied projects and a reason (10+ characters).")
        tb.status = TiebreakRound.Status.ESCALATED
        tb.decision_note = note.strip()
        tb.result = {**result, "order": decision_order, "method": "organizer decision (recorded)", "evidence_order": st["order"]}
        summary = "Tie-break decided by organizer: " + " > ".join(decision_order) + f" ({note.strip()[:120]})"
    tb.resolved_at = now()
    tb.save()
    audit.record("TIEBREAK_RESOLVED", summary, event=tb.event, actor=actor, actor_role="organizer", target=tb,
                 data={"order": tb.result["order"], "status": tb.status})
    return tb


# --------------------------------------------------------------------------- focus

def plan_focus(event, budget: int | None = None, seed: int = 0) -> dict:
    run = compute_run(event, None, kind=RankingRun.Kind.PREVIEW, heavy=False)
    out = run.output
    if out.get("status") != "ok":
        raise Conflict("Not enough reviews yet to plan a focus round.")
    entries = out["entries"]
    baseline = sum(e["n_reviews"] for e in entries)
    budget = budget if budget is not None else max(1, round(baseline * event.focus_budget_pct / 100))
    p_prize = {e["project"]: e.get("p_prize") or 0.0 for e in entries}
    se = {e["project"]: e.get("se") or 0.5 for e in entries}
    roles = ops.judge_roles(event)
    load = defaultdict(int)
    existing = set()
    for a in Assignment.objects.filter(event=event).filter(ops.LIVE).select_related("judge_role", "project"):
        load[a.judge_role_id] += 1
        existing.add((ops.jkey(a.judge_role), a.project.ref))
    flat = set((out.get("flat_judges") or {}).keys())
    judges, capacity = [], {}
    default_cap = max(event.batch_size, 12)
    for r in roles:
        if not r.available or ops.jkey(r) in flat:
            continue
        judges.append({"id": ops.jkey(r), "tracks": [jt.track.ref for jt in r.judge_tracks.all()] or None})
        capacity[ops.jkey(r)] = max(0, (r.capacity or default_cap) - load[r.pk])
    project_track = {e["project"]: e["track"] for e in entries}
    fit_stub = {"flat_judges": {j: {} for j in flat}, "reviews": []}
    plan = plan_next_round(fit_stub, p_prize, se, out["sigma2"], budget, judges, capacity, project_track,
                           existing=existing, conflicts=ops.conflict_pairs(event), seed=seed)
    return {"run": str(run.pk), "run_hash": run.output_hash, "budget": budget, "baseline_reviews": baseline,
            "rows": plan, "seed": seed}


@transaction.atomic
def commit_focus(event, actor, plan: dict) -> FocusRound:
    if not plan["rows"]:
        raise Conflict("The focus plan is empty: no project is both uncertain and assignable.")
    run = RankingRun.objects.get(pk=plan["run"])
    fr = FocusRound.objects.create(event=event, run=run, budget=plan["budget"], plan=plan, committed_at=now(),
                                   created_by=getattr(actor, "user", actor))
    commit_plan = {"kind": "focus", "seed": plan["seed"], "new": [(r["judge"], r["project"]) for r in plan["rows"]],
                   "metrics": {"projects_at_target": "-", "projects": len({r["project"] for r in plan["rows"]}),
                               "components": "-"}}
    ops.commit_plan(event, actor, commit_plan, source="focus", batch_kind="focus")
    if event.phase == "judging":
        event.phase = "focus"
        event.save(update_fields=["phase"])
    return fr


# --------------------------------------------------------------------------- lock & publish

def final_order(event, run: RankingRun) -> list[str]:
    order = [e["project"] for e in sorted(run.output.get("entries", []), key=lambda e: (e["rank"], e["project"]))]
    for tb in event.tiebreaks.filter(status__in=["resolved", "escalated"]).order_by("resolved_at"):
        tied = tb.result.get("order") or []
        idx = sorted(order.index(r) for r in tied if r in order)
        for pos, ref in zip(idx, [r for r in tied if r in order]):
            order[pos] = ref
    return order


@transaction.atomic
def lock_results(event, actor) -> ResultPublication:
    if event.phase in ("locked", "published"):
        raise Conflict("Results are already locked.")
    open_tb = event.tiebreaks.filter(status="open").count()
    if open_tb:
        raise Conflict(f"{open_tb} tie-break round(s) are still open. Resolve them first.")
    run = compute_run(event, actor, kind=RankingRun.Kind.OFFICIAL, heavy=True)
    order = final_order(event, run)
    audit.record("RESULTS_LOCKED", f"Results locked on run {run.output_hash[:12]}…; podium: {', '.join(order[:event.prize_positions])}",
                 event=event, actor=actor, actor_role="organizer", target=run,
                 data={"output_hash": run.output_hash, "final_order": order})
    cp = audit.checkpoint(event, "results locked")
    pub, _ = ResultPublication.objects.update_or_create(event=event, defaults={
        "run": run, "locked_at": now(), "locked_by": getattr(actor, "user", actor), "audit_seq": cp.seq,
        "audit_head": cp.head, "checkpoint_signature": cp.signature, "final_order": order,
        "tiebreak_results": [{"id": str(t.pk), "status": t.status, "order": t.result.get("order"),
                              "method": t.result.get("method"), "note": t.decision_note}
                             for t in event.tiebreaks.exclude(status="open")],
    })
    event.phase = "locked"
    event.save(update_fields=["phase"])
    return pub


@transaction.atomic
def publish_results(event, actor, release_feedback: bool = True) -> ResultPublication:
    pub = getattr(event, "publication", None)
    if event.phase != "locked" or pub is None:
        raise Conflict("Lock the results before publishing.")
    event.results_published_at = now()
    event.phase = "published"
    event.save(update_fields=["results_published_at", "phase"])
    audit.record("RESULTS_PUBLISHED", f"Results published ({event.name})", event=event, actor=actor,
                 actor_role="organizer", target=pub.run, data={"output_hash": pub.run.output_hash})
    cp = audit.checkpoint(event, "results published")
    pub.published_at = now()
    pub.audit_seq, pub.audit_head, pub.checkpoint_signature = cp.seq, cp.head, cp.signature
    pub.save()
    if release_feedback and not event.feedback_released_at:
        from quorum.judging.feedback import release_feedback as release

        release(event, actor)
    from quorum.audit.certificates import issue_all

    issue_all(event)
    return pub


@transaction.atomic
def reopen_results(event, actor, reason: str):
    if len((reason or "").strip()) < 10:
        raise Invalid("Reopening needs a reason (10+ characters).")
    if event.phase not in ("locked",):
        raise Conflict("Only locked (unpublished) results can be reopened.")
    event.phase = "deliberation"
    event.save(update_fields=["phase"])
    audit.record("RESULTS_REOPENED", f"Results reopened: {reason.strip()[:200]}", event=event, actor=actor,
                 actor_role="organizer")
