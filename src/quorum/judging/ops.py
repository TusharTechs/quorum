"""Pillar 1 — Quorum: keep every project at its review target across an async window.

Hackathon Raptors judges async: batches of 10-12 sent a day after freeze, ~10 days, 1-2
hours per judge, and some judges never finish (the fixture has two such batches). This
module is the organizer's cockpit for that: progress, coverage, a burn-down with a
completion forecast, stalled-batch detection, reminders, and self-healing reassignment
that never drops a project below its target and never breaks conflict-of-interest rules.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q

from engine.assign import assign, design_diagnostics
from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.core.mail import queue_email
from quorum.events.models import Event, EventRole, Project
from quorum.policy.errors import Conflict as ConflictError

from .models import Assignment, Conflict, JudgeBatch, Reminder, ReminderDelivery, Review

LIVE = ~Q(status__in=[Assignment.Status.REASSIGNED, Assignment.Status.RECUSED])


def canonical_projects(event):
    return list(Project.objects.filter(event=event, status="submitted", duplicate_of__isnull=True)
                .select_related("track", "team").order_by("ref"))


def judge_roles(event):
    return list(EventRole.objects.filter(event=event, role="judge").select_related("user")
                .prefetch_related("judge_tracks__track").order_by("ref", "created_at"))


def jkey(r: EventRole) -> str:
    return r.ref or str(r.pk)


def conflict_pairs(event) -> set:
    """(judge_key, project_ref) pairs that must never be assigned."""
    pairs = set()
    roles = {r.pk: jkey(r) for r in EventRole.objects.filter(event=event, role="judge")}
    team_projects = defaultdict(list)
    for p in Project.objects.filter(event=event).values("team_id", "ref"):
        team_projects[p["team_id"]].append(p["ref"])
    for c in Conflict.objects.filter(event=event, status=Conflict.Status.CONFIRMED):
        for ref in team_projects[c.team_id]:
            pairs.add((roles.get(c.judge_role_id), ref))
    return pairs


# --------------------------------------------------------------------------- progress

def judge_progress(event: Event, at=None) -> list[dict]:
    at = at or now()
    rows = []
    live = defaultdict(lambda: {"assigned": 0, "submitted": 0, "pending": 0})
    for a in Assignment.objects.filter(event=event).filter(LIVE).values("judge_role_id", "status"):
        d = live[a["judge_role_id"]]
        d["assigned"] += 1
        if a["status"] == Assignment.Status.SUBMITTED:
            d["submitted"] += 1
        else:
            d["pending"] += 1
    feedback_empty = defaultdict(int)
    for r in Review.objects.filter(event=event, status="submitted").values("judge_role_id", "feedback_to_team"):
        if not (r["feedback_to_team"] or "").strip():
            feedback_empty[r["judge_role_id"]] += 1
    opens, closes = event.judging_opens_at, event.judging_closes_at
    elapsed_days = max((min(at, closes) - opens).total_seconds() / 86400, 0.25)
    left_days = max((closes - at).total_seconds() / 86400, 0.0)
    for r in judge_roles(event):
        d = live[r.pk]
        rate = d["submitted"] / elapsed_days
        need = d["pending"]
        projected_days = (need / rate) if rate > 0 else (math.inf if need else 0.0)
        idle_h = ((at - r.last_activity_at).total_seconds() / 3600) if r.last_activity_at else None
        if not r.available:
            status = "unavailable"
        elif d["assigned"] and need == 0:
            status = "done"
        elif need and (r.last_activity_at is None and (at - opens) > timedelta(hours=48)
                       or (idle_h is not None and idle_h > 48)):
            status = "stalled"
        elif need and projected_days > left_days:
            status = "behind"
        elif d["assigned"] == 0:
            status = "unassigned"
        else:
            status = "on_track"
        rows.append({
            "role": r, "ref": jkey(r), "name": r.user.name or r.user.email, "email": r.user.email,
            "tracks": [jt.track.name for jt in r.judge_tracks.all()], **d,
            "pct": (d["submitted"] / d["assigned"]) if d["assigned"] else 0.0,
            "rate_per_day": rate, "projected_days": projected_days, "idle_hours": idle_h,
            "last_activity": r.last_activity_at, "status": status, "empty_feedback": feedback_empty[r.pk],
        })
    return rows


def coverage(event: Event) -> dict:
    k = event.reviews_per_project
    projects = canonical_projects(event)
    sub = defaultdict(int)
    pend = defaultdict(int)
    for a in Assignment.objects.filter(event=event).filter(LIVE).values("project_id", "status"):
        (sub if a["status"] == Assignment.Status.SUBMITTED else pend)[a["project_id"]] += 1
    rows = []
    for p in projects:
        s, q = sub[p.pk], pend[p.pk]
        state = "ok" if s >= k else ("pending" if s + q >= k else "below")
        rows.append({"project": p, "submitted": s, "pending": q, "target": k, "state": state})
    return {
        "rows": rows, "target": k,
        "ok": sum(1 for r in rows if r["state"] == "ok"),
        "pending": sum(1 for r in rows if r["state"] == "pending"),
        "below": sum(1 for r in rows if r["state"] == "below"),
        "total": len(rows),
    }


def feedback_coverage(event: Event) -> dict:
    projects = canonical_projects(event)
    by_p = defaultdict(list)
    for r in Review.objects.filter(event=event, status="submitted").values("project_id", "feedback_to_team", "moderation"):
        if r["moderation"] != "hidden":
            by_p[r["project_id"]].append((r["feedback_to_team"] or "").strip())
    none, short = [], []
    for p in projects:
        texts = [t for t in by_p[p.pk] if t]
        if not texts:
            none.append(p)
        elif max(len(t) for t in texts) < event.min_feedback_chars:
            short.append(p)
    return {"none": none, "short": short, "total": len(projects),
            "with_feedback": len(projects) - len(none), "min_chars": event.min_feedback_chars}


def burndown(event: Event, at=None, samples=48) -> dict:
    at = at or now()
    opens, closes = event.judging_opens_at, event.judging_closes_at
    span = max((closes - opens).total_seconds(), 1)
    done_at = {rv.assignment_id: rv.submitted_at for rv in Review.objects.filter(event=event, status="submitted")
               .only("assignment_id", "submitted_at")}
    rows = list(Assignment.objects.filter(event=event).filter(LIVE).select_related("batch")
                .only("id", "assigned_at", "batch__sent_at"))
    pts_src = [((a.batch.sent_at if a.batch and a.batch.sent_at else a.assigned_at), done_at.get(a.id)) for a in rows]
    total = len(pts_src)
    upto = min(max((at - opens).total_seconds() / span, 0.0), 1.0)
    points = []
    for s in range(samples + 1):
        frac = upto * s / samples
        t = opens + timedelta(seconds=span * frac)
        remaining = sum(1 for st, dn in pts_src if st <= t and (dn is None or dn > t))
        points.append((frac, remaining))
    remaining_now = sum(1 for _, dn in pts_src if dn is None)
    return {"points": points, "total": total, "remaining": remaining_now, "elapsed_frac": upto}


def forecast(event: Event, progress: list[dict] | None = None, at=None) -> dict:
    """Which projects will end the window below their target at the current pace?"""
    at = at or now()
    progress = progress or judge_progress(event, at)
    left_days = max((event.judging_closes_at - at).total_seconds() / 86400, 0.0)
    risky_judges = {p["role"].pk for p in progress if p["status"] in ("stalled", "behind", "unavailable")}
    at_risk = []
    for row in coverage(event)["rows"]:
        if row["state"] == "ok":
            continue
        pend = Assignment.objects.filter(event=event, project=row["project"]).exclude(
            status__in=["submitted", "reassigned", "recused"]).values_list("judge_role_id", flat=True)
        safe_pending = sum(1 for j in pend if j not in risky_judges)
        if row["submitted"] + safe_pending < row["target"]:
            at_risk.append({**row, "safe_pending": safe_pending,
                            "short_by": row["target"] - row["submitted"] - safe_pending})
    return {"at_risk": at_risk, "days_left": left_days, "risky_judges": len(risky_judges)}


# --------------------------------------------------------------------------- planning

def inactive_role_ids(event) -> set[str]:
    """Judges who should not receive new work: stalled, or marked unavailable."""
    return {str(p["role"].pk) for p in judge_progress(event) if p["status"] in ("stalled", "unavailable")}


def _engine_inputs(event, exclude_roles=(), drop_pending_of=()):
    projects = canonical_projects(event)
    pmap = {p.ref: p for p in projects}
    exclude = {str(x) for x in exclude_roles} | inactive_role_ids(event)
    drop = {str(x) for x in drop_pending_of}
    roles = [r for r in judge_roles(event) if r.available and str(r.pk) not in exclude]
    judges = [{"id": jkey(r), "tracks": [jt.track.ref for jt in r.judge_tracks.all()] or None} for r in roles]
    existing = defaultdict(list)
    load = defaultdict(int)
    for a in (Assignment.objects.filter(event=event).filter(LIVE).select_related("judge_role", "project")):
        if str(a.judge_role_id) in drop and a.status != Assignment.Status.SUBMITTED:
            continue
        existing[a.project.ref].append(jkey(a.judge_role))
        load[jkey(a.judge_role)] += 1
    default_cap = max(event.batch_size, math.ceil(event.reviews_per_project * len(projects) / max(1, len(roles))) + 1)
    capacity = {jkey(r): (r.capacity or default_cap) for r in roles}
    for a_key in load:  # excluded judges keep their existing (submitted) work but get no new slots
        capacity.setdefault(a_key, 0)
    return projects, pmap, roles, judges, existing, capacity


def plan_assignments(event: Event, k: int | None = None, seed: int = 0, exclude_roles=(), drop_pending_of=(),
                     kind: str = "baseline") -> dict:
    """Dry run: top every eligible project up to k live reviews. Nothing is written."""
    k = k or event.reviews_per_project
    projects, pmap, roles, judges, existing, capacity = _engine_inputs(event, exclude_roles, drop_pending_of)
    res = assign([{"id": p.ref, "track": p.track.ref if p.track else None} for p in projects], judges, k=k,
                 capacity=capacity, conflicts=conflict_pairs(event), seed=seed,
                 existing={p: [j for j in js if j in capacity] for p, js in existing.items()})
    pairs = [(j, p) for p, js in res["assignment"].items() for j in js]
    diag = design_diagnostics(pairs, {p.ref: (p.track.ref if p.track else None) for p in projects})
    loads = [res["load"].get(jkey(r), 0) for r in roles]
    return {
        "kind": kind, "k": k, "seed": seed, "new": res["new"], "unfilled": res["unfilled"],
        "metrics": {
            "new_assignments": len(res["new"]), "projects": len(projects),
            "projects_at_target": sum(1 for p, js in res["assignment"].items() if len(js) >= k),
            "components": diag["components"], "judge_pairs_sharing": diag["judge_pairs_sharing"],
            "judge_pairs_total": diag["judge_pairs_total"], "load_min": min(loads) if loads else 0,
            "load_max": max(loads) if loads else 0, "conflicts_violated": sum(
                1 for j, p in res["new"] if (j, p) in conflict_pairs(event)),
            "min_offset_information": round(min(diag["offset_information"].values()), 2)
            if diag["offset_information"] else None,
        },
        "exclude_roles": [str(x) for x in exclude_roles], "drop_pending_of": [str(x) for x in drop_pending_of],
    }


@transaction.atomic
def commit_plan(event: Event, actor, plan: dict, source: str = "algorithm", batch_kind: str | None = None) -> int:
    roles = {jkey(r): r for r in judge_roles(event)}
    projects = {p.ref: p for p in canonical_projects(event)}
    batch_kind = batch_kind or plan.get("kind", "baseline")
    # rebalance: retire the pending work being moved
    moved = {}
    if plan.get("drop_pending_of"):
        for a in Assignment.objects.filter(event=event, judge_role_id__in=[str(x) for x in plan["drop_pending_of"]]).exclude(
                status__in=["submitted", "reassigned", "recused"]).select_related("project"):
            a.status = Assignment.Status.REASSIGNED
            a.save(update_fields=["status"])
            moved[a.project.ref] = a
    batches = {}
    created = 0
    due = event.judging_closes_at
    forbidden = conflict_pairs(event)
    live = {(ops_key, pref) for ops_key, pref in
            Assignment.objects.filter(event=event).filter(LIVE).values_list("judge_role__ref", "project__ref")}
    skipped = []
    for jref, pref in plan["new"]:
        role, project = roles.get(jref), projects.get(pref)
        if not role or not project:
            skipped.append((jref, pref, "unknown"))
            continue
        # plans can arrive from API clients: re-validate every pair before writing
        tracks = {jt.track_id for jt in role.judge_tracks.all()}
        if (jref, pref) in forbidden:
            skipped.append((jref, pref, "conflict of interest"))
            continue
        if (jref, pref) in live:
            skipped.append((jref, pref, "already assigned"))
            continue
        if batch_kind not in ("focus", "tiebreak") and tracks and project.track_id not in tracks:
            skipped.append((jref, pref, "outside the judge's tracks"))
            continue
        if role.pk not in batches:
            batches[role.pk] = JudgeBatch.objects.create(event=event, judge_role=role, kind=batch_kind,
                                                         round=JudgeBatch.objects.filter(event=event, judge_role=role).count() + 1,
                                                         due_at=due)
        Assignment.objects.create(event=event, batch=batches[role.pk], judge_role=role, project=project,
                                  source=source, strategy="overlap-greedy/v1", seed=plan.get("seed"), due_at=due,
                                  reassigned_from=moved.get(pref))
        created += 1
    plan["skipped"] = skipped
    if created == 0 and not moved:
        return 0
    action = {"rebalance": "ASSIGNMENTS_REBALANCED", "focus": "FOCUS_COMMITTED"}.get(batch_kind, "JUDGES_ASSIGNED")
    audit.record(action, f"{created} assignments created ({batch_kind}); "
                         f"{plan['metrics']['projects_at_target']}/{plan['metrics']['projects']} projects at target, "
                         f"{plan['metrics']['components']} judge-graph component(s), 0 conflicts",
                 event=event, actor=actor, actor_role="organizer", data={"metrics": plan["metrics"], "seed": plan.get("seed"),
                                                                         "moved": len(moved)})
    if event.judging_opens_at <= now():
        send_batches(event, actor, only_new=True)
    return created


def plan_rebalance(event: Event, role_ids: list) -> dict:
    """Move the pending work of stalled/unavailable judges to judges with spare capacity."""
    plan = plan_assignments(event, exclude_roles=role_ids, drop_pending_of=role_ids, kind="rebalance",
                            seed=int(now().timestamp()) % 100000)
    return plan


# --------------------------------------------------------------------------- batches & reminders

def judge_link(event: Event) -> str:
    return f"{settings.PUBLIC_ORIGIN}/j/{event.slug}"


@transaction.atomic
def send_batches(event: Event, actor=None, only_new: bool = False) -> int:
    from quorum.accounts.services import issue_magic_link

    t = now()
    qs = JudgeBatch.objects.filter(event=event, sent_at__isnull=True).select_related("judge_role__user")
    n = 0
    for b in qs:
        count = b.assignments.exclude(status__in=["reassigned", "recused"]).count()
        if not count:
            continue
        b.sent_at = t
        b.due_at = b.due_at or event.judging_closes_at
        b.save(update_fields=["sent_at", "due_at"])
        u = b.judge_role.user
        issue_magic_link(u.email, "login", next_url=f"/j/{event.slug}", ttl_minutes=60 * 24 * 7,
                         subject=f"{event.name}: your judging batch ({count} project{'s' if count != 1 else ''})",
                         intro=(f"Hello {u.display},\n\nYour batch for {event.name} is ready: {count} project"
                                f"{'s' if count != 1 else ''}, due {b.due_at:%d %b %Y %H:%M} UTC. It takes about "
                                f"{max(1, round(count * 8 / 60))} hour(s). Keyboard: 1-5 to score, J/K to move, "
                                f"Cmd/Ctrl+Enter to submit.\n\nThis link signs you straight in (valid 7 days):"))
        n += 1
    if n:
        ensure_default_reminders(event)
        audit.record("BATCHES_SENT", f"{n} judging batch e-mail(s) sent", event=event, actor=actor,
                     actor_role="organizer" if actor else "system", data={"batches": n})
    return n


def ensure_default_reminders(event: Event):
    opens, closes = event.judging_opens_at, event.judging_closes_at
    wanted = [
        (Reminder.Kind.MIDPOINT, opens + (closes - opens) / 2),
        (Reminder.Kind.DUE_24H, closes - timedelta(hours=24)),
        (Reminder.Kind.OVERDUE, closes + timedelta(hours=2)),
    ]
    for kind, when in wanted:
        Reminder.objects.get_or_create(event=event, kind=kind, defaults={"send_at": when})


REMINDER_TEXT = {
    "midpoint": "Halfway through the judging window: you have {pending} review(s) left in {event}.",
    "due_24h": "24 hours left: {pending} review(s) still open in {event}.",
    "overdue": "The judging window for {event} has closed and {pending} review(s) are still open. "
               "If you cannot finish, reply and we will reassign them; nothing is lost.",
    "custom": "{message}",
    "batch_sent": "Your batch for {event} is waiting: {pending} review(s).",
}


def _pending_by_role(event):
    d = defaultdict(int)
    for a in Assignment.objects.filter(event=event).exclude(
            status__in=["submitted", "reassigned", "recused"]).values("judge_role_id"):
        d[a["judge_role_id"]] += 1
    return d


def deliver_reminder(rem: Reminder, roles=None, actor=None) -> int:
    event = rem.event
    pending = _pending_by_role(event)
    targets = EventRole.objects.filter(event=event, role="judge", available=True).select_related("user")
    if roles:
        targets = targets.filter(pk__in=roles)
    n = 0
    for r in targets:
        if rem.only_incomplete and not pending.get(r.pk):
            continue
        _, created = ReminderDelivery.objects.get_or_create(reminder=rem, judge_role=r)
        if not created:
            continue
        text = REMINDER_TEXT.get(rem.kind, "{message}").format(pending=pending.get(r.pk, 0), event=event.name,
                                                               message=rem.message)
        queue_email(r.user.email, f"{event.name}: judging reminder",
                    f"Hello {r.user.display},\n\n{text}\n\nOpen your queue: {judge_link(event)}\n")
        n += 1
    rem.sent_at = rem.sent_at or now()
    rem.save(update_fields=["sent_at"])
    audit.record("REMINDER_SENT", f"{rem.get_kind_display()} reminder sent to {n} judge(s)", event=event, actor=actor,
                 actor_role="organizer" if actor else "system", target=rem, data={"recipients": n, "kind": rem.kind})
    return n


def send_due_reminders():
    for rem in Reminder.objects.filter(sent_at__isnull=True, send_at__lte=now()).select_related("event"):
        with transaction.atomic():
            deliver_reminder(rem)


@transaction.atomic
def nudge_now(event: Event, actor, role_ids=None, message: str = "") -> int:
    rem = Reminder.objects.create(event=event, kind=Reminder.Kind.CUSTOM, send_at=now(), message=message or
                                  "A friendly nudge: you still have {pending} review(s) waiting.".replace(
                                      "{pending}", "some"))
    return deliver_reminder(rem, roles=role_ids, actor=actor)


# --------------------------------------------------------------------------- availability & conflicts

@transaction.atomic
def set_availability(event: Event, actor, role: EventRole, available: bool):
    role.available = available
    role.save(update_fields=["available"])
    audit.record("JUDGE_AVAILABILITY", f"{role.user} marked {'available' if available else 'unavailable'}",
                 event=event, actor=actor, actor_role="organizer", target=role)


@transaction.atomic
def recuse(actor, assignment: Assignment, note: str = ""):
    """A judge declares a conflict: the assignment is retired, a confirmed conflict is
    recorded for the whole team, and the project is re-queued to another judge."""
    event = assignment.event
    if assignment.status == Assignment.Status.SUBMITTED:
        raise ConflictError("You already submitted this review; ask an organizer to handle the conflict.")
    assignment.status = Assignment.Status.RECUSED
    assignment.save(update_fields=["status"])
    Conflict.objects.update_or_create(judge_role=assignment.judge_role, team=assignment.project.team,
                                      defaults={"event": event, "source": "declared", "status": "confirmed",
                                                "note": note[:300]})
    audit.record("CONFLICT_DECLARED", f"{assignment.judge_role.user} recused from “{assignment.project.title}”",
                 event=event, actor=actor, actor_role="judge", target=assignment.project, data={"note": note[:300]})
    plan = plan_assignments(event, kind="rebalance", seed=int(now().timestamp()) % 100000)
    if plan["new"]:
        plan["new"] = [(j, p) for j, p in plan["new"] if p == assignment.project.ref][:1]
        if plan["new"]:
            commit_plan(event, None, plan, source="rebalance", batch_kind="rebalance")


def suggest_conflicts(event: Event) -> int:
    """Suggest (never confirm) conflicts from shared e-mail domains, ignoring free-mail and
    the fixture's placeholder domains. Organizers confirm or dismiss."""
    free = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me",
            "protonmail.com", "example.org", "example.com", "demo.local"}
    from quorum.events.models import TeamMember

    teams_by_domain = defaultdict(set)
    for m in TeamMember.objects.filter(event=event).select_related("user"):
        d = m.user.email.split("@")[-1].lower()
        if d not in free:
            teams_by_domain[d].add(m.team_id)
    n = 0
    for r in EventRole.objects.filter(event=event, role="judge").select_related("user"):
        d = r.user.email.split("@")[-1].lower()
        for team_id in teams_by_domain.get(d, ()):
            _, created = Conflict.objects.get_or_create(judge_role=r, team_id=team_id, defaults={
                "event": event, "source": "same_domain", "status": "suggested", "note": f"shared domain @{d}"})
            n += created
    return n


def submitted_review_count() -> int:
    """Platform-wide count for operational metrics (no per-event or per-judge detail)."""
    return Review.objects.filter(status="submitted").count()
