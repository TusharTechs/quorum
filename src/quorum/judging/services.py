"""Judge-side rules: open, autosave, submit and edit a review; record a pairwise choice."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.core.text import clean_line
from quorum.policy.errors import Conflict, Forbidden, Invalid, WindowClosed

from .models import Assignment, PairwiseComparison, Review, ReviewScore

LOCKED_PHASES = {"locked", "published", "archived"}


def guard_judging_open(event, at=None):
    at = at or now()
    if event.phase in LOCKED_PHASES:
        raise WindowClosed("Results are locked; reviews can no longer change.", code="results_locked")
    if at < event.judging_opens_at:
        raise WindowClosed("Judging has not opened yet.", code="judging_not_open")
    if at >= event.judging_closes_at and event.phase not in ("focus", "deliberation"):
        raise WindowClosed("The judging window has closed. Ask an organizer to reopen it.", code="judging_closed")


@transaction.atomic
def open_review(actor, a: Assignment) -> Review:
    rv = Review.objects.filter(assignment=a).first()
    if rv:
        return rv
    guard_judging_open(a.event)
    rv = Review.objects.create(assignment=a, event=a.event, judge_role=a.judge_role, project=a.project)
    if a.status == Assignment.Status.PENDING:
        a.status = Assignment.Status.IN_PROGRESS
        a.save(update_fields=["status"])
    audit.record("REVIEW_OPENED", f"{a.judge_role.user} opened “{a.project.title}”", event=a.event, actor=actor,
                 actor_role="judge", target=a.project)
    return rv


def _parse_scores(event, raw: dict) -> dict:
    out = {}
    for c in event.criteria.all():
        v = raw.get(c.key)
        if v in (None, ""):
            continue
        try:
            d = Decimal(str(v))
        except Exception:
            raise Invalid(f"{c.name}: not a number.")
        if d < c.scale_min or d > c.scale_max:
            raise Invalid(f"{c.name}: must be between {c.scale_min} and {c.scale_max}.")
        out[c] = d
    return out


@transaction.atomic
def save_review(actor, a: Assignment, *, scores: dict, feedback: str | None = None, note: str | None = None,
                quotable: str | None = None, active_seconds: int = 0, submit: bool = False) -> Review:
    event = a.event
    guard_judging_open(event)
    rv = open_review(actor, a)
    parsed = _parse_scores(event, scores or {})
    criteria = list(event.criteria.all())
    old = {s.criterion.key: str(s.value) for s in rv.scores.select_related("criterion")}
    for c, v in parsed.items():
        ReviewScore.objects.update_or_create(review=rv, criterion=c, defaults={"value": v})
    if feedback is not None:
        rv.feedback_to_team = feedback[:5000]
    if note is not None:
        rv.note_to_organizers = note[:3000]
    if quotable in dict(Review.Quotable.choices):
        rv.quotable = quotable
    rv.active_seconds = max(rv.active_seconds, int(active_seconds or 0))
    new = {s.criterion.key: str(s.value) for s in rv.scores.select_related("criterion")}
    was_submitted = rv.status == Review.Status.SUBMITTED
    if submit or was_submitted:
        missing = [c.name for c in criteria if c.key not in new]
        if missing:
            raise Invalid("Score every criterion before submitting: " + ", ".join(missing))
        if len((rv.feedback_to_team or "").strip()) < event.min_feedback_chars:
            raise Invalid(f"Write at least {event.min_feedback_chars} characters of feedback for the team. "
                          "Every team receives it, placed or not.", code="feedback_too_short")
    if submit and not was_submitted:
        rv.status = Review.Status.SUBMITTED
        rv.submitted_at = now()
        a.status = Assignment.Status.SUBMITTED
        a.save(update_fields=["status"])
    rv.moderation = Review.Moderation.APPROVED if event.auto_approve_feedback else (
        Review.Moderation.PENDING if (rv.feedback_to_team or "").strip() else Review.Moderation.APPROVED)
    rv.save()
    role = a.judge_role
    role.last_activity_at = now()
    role.save(update_fields=["last_activity_at"])
    if submit and not was_submitted:
        audit.record("REVIEW_SUBMITTED", f"{role.user} submitted a review of “{a.project.title}”", event=event,
                     actor=actor, actor_role="judge", target=a.project,
                     data={"scores": new, "feedback_chars": len(rv.feedback_to_team or ""), "active_seconds": rv.active_seconds})
    elif was_submitted and old != new:
        audit.record("REVIEW_UPDATED", f"{role.user} changed scores on “{a.project.title}”: " +
                     ", ".join(f"{k} {old.get(k, '–')}→{new[k]}" for k in new if old.get(k) != new[k]),
                     event=event, actor=actor, actor_role="judge", target=a.project, data={"old": old, "new": new})
    return rv


@transaction.atomic
def record_comparison(actor, event, role, a_project, b_project, outcome: str, tiebreak=None, reason: str = "",
                      active_seconds: int = 0) -> PairwiseComparison:
    if outcome not in ("a", "b", "tie"):
        raise Invalid("Outcome must be a, b or tie.")
    if a_project.pk == b_project.pk:
        raise Invalid("Compare two different projects.")
    if tiebreak is not None:
        if tiebreak.status != "open":
            raise WindowClosed("This tie-break round is closed.", code="tiebreak_closed")
        if not tiebreak.judge_roles.filter(pk=role.pk).exists():
            raise Forbidden("You are not on this tie-break panel.")
        ids = set(tiebreak.projects.values_list("pk", flat=True))
        if a_project.pk not in ids or b_project.pk not in ids:
            raise Forbidden("Both projects must be part of the tie-break.")
    else:  # comparative judging within a track (judging.pairwise); authorization before state
        from .pairwise import pool

        if a_project.track_id is None or a_project.track_id != b_project.track_id:
            raise Forbidden("Comparisons are between two projects of the same track.")
        track = a_project.track
        mine = set(pool(role, track))
        if a_project.ref not in mine or b_project.ref not in mine:
            raise Forbidden("You can only compare projects you have reviewed.")
        if not track.pairwise:
            raise Conflict("Comparative judging is not switched on for this track.", code="pairwise_disabled")
        guard_judging_open(event)
    x, y = sorted([a_project, b_project], key=lambda p: str(p.pk))
    if x.pk != a_project.pk:
        outcome = {"a": "b", "b": "a", "tie": "tie"}[outcome]
    if PairwiseComparison.objects.filter(judge_role=role, tiebreak=tiebreak, project_a=x, project_b=y).exists():
        raise Conflict("You already compared these two.", code="duplicate_comparison")
    c = PairwiseComparison.objects.create(event=event, tiebreak=tiebreak, judge_role=role, project_a=x, project_b=y,
                                          outcome=outcome, reason=clean_line(reason, 500),
                                          active_seconds=int(active_seconds or 0))
    role.last_activity_at = now()
    role.save(update_fields=["last_activity_at"])
    audit.record("PAIRWISE_RECORDED", f"{role.user} compared “{x.title}” and “{y.title}”", event=event, actor=actor,
                 actor_role="judge", target=c, data={"a": x.ref, "b": y.ref, "outcome": outcome,
                                                     "tiebreak": str(tiebreak.pk) if tiebreak else None})
    return c
