"""Pillar 4 — Answer: every team gets its score and written feedback, placed or not.

That is Hackathon Raptors' published promise, and today it is done by hand. Here it is a
pipeline: judges must write feedback (minimum length), organizers see which teams would
receive none, moderate (approve / edit with a note / hide), then release. Release e-mails
every team member a sign-in link straight to their scorecard.
"""

from __future__ import annotations

import string
from collections import defaultdict

from django.db import transaction

from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.events.models import Project, TeamMember
from quorum.policy.errors import Conflict, Invalid

from .models import Review


@transaction.atomic
def moderate(actor, review: Review, action: str, text: str = "", note: str = ""):
    if action == "approve":
        review.moderation = Review.Moderation.APPROVED
    elif action == "edit":
        if not text.strip():
            raise Invalid("Edited feedback cannot be empty; hide it instead.")
        review.moderation = Review.Moderation.EDITED
        review.moderated_text = text.strip()[:5000]
    elif action == "hide":
        review.moderation = Review.Moderation.HIDDEN
    else:
        raise Invalid("Unknown moderation action.")
    review.moderation_note = (note or "").strip()[:300]
    review.moderated_by = getattr(actor, "user", actor)
    review.save(update_fields=["moderation", "moderated_text", "moderation_note", "moderated_by", "updated_at"])
    audit.record("FEEDBACK_MODERATED", f"Feedback on “{review.project.title}” {action}d", event=review.event,
                 actor=actor, actor_role="organizer", target=review.project,
                 data={"review": str(review.pk), "action": action, "note": review.moderation_note})


@transaction.atomic
def approve_all_pending(actor, event) -> int:
    n = Review.objects.filter(event=event, status="submitted", moderation="pending").update(
        moderation="approved", moderated_by=getattr(actor, "user", actor))
    audit.record("FEEDBACK_MODERATED", f"{n} pending feedback item(s) approved in bulk", event=event, actor=actor,
                 actor_role="organizer", data={"bulk": n})
    return n


@transaction.atomic
def release_feedback(event, actor) -> int:
    from quorum.accounts.services import issue_magic_link

    pending = Review.objects.filter(event=event, status="submitted", moderation="pending").count()
    if pending and not event.auto_approve_feedback:
        raise Conflict(f"{pending} feedback item(s) still await moderation.")
    event.feedback_released_at = now()
    event.save(update_fields=["feedback_released_at"])
    n = 0
    teams = Project.objects.filter(event=event, status="submitted", duplicate_of__isnull=True).values_list("team_id", flat=True)
    for m in TeamMember.objects.filter(event=event, team_id__in=teams).select_related("user", "team"):
        issue_magic_link(m.user.email, "scorecard", next_url=f"/me/scorecard/{event.slug}", ttl_minutes=60 * 24 * 14,
                         subject=f"{event.name}: your scorecard and judges' feedback",
                         intro=(f"Hello {m.user.display},\n\nThe judges' feedback for {m.team.name} is ready: your "
                                f"calibrated score, how it was computed, and every judge's written comments.\n\n"
                                f"This link opens your scorecard (valid 14 days):"))
        n += 1
    audit.record("FEEDBACK_RELEASED", f"Feedback released to {n} participant(s)", event=event, actor=actor,
                 actor_role="organizer", data={"recipients": n})
    return n


def judge_labels(reviews) -> dict:
    """Stable pseudonyms per project: Judge A, B, C... in submission order."""
    out = {}
    for i, r in enumerate(sorted(reviews, key=lambda r: (r.submitted_at or now(), str(r.pk)))):
        out[r.pk] = "Judge " + (string.ascii_uppercase[i] if i < 26 else str(i + 1))
    return out


def scorecard(event, project: Project, run) -> dict:
    """Everything a team is entitled to see after release, and nothing more: no judge
    identities, no other team's feedback, no raw peer-review internals."""
    entries = {e["project"]: e for e in (run.output.get("entries") or [])} if run else {}
    e = entries.get(project.ref)
    reviews = list(Review.objects.filter(event=event, project=project, status="submitted").exclude(moderation="hidden"))
    labels = judge_labels(reviews)
    feedback = [{"judge": labels[r.pk], "text": r.feedback_for_team} for r in reviews if r.feedback_for_team.strip()]
    crit = list(event.criteria.order_by("position"))
    percentiles = {}
    if e and run:
        same_track = [x for x in entries.values() if x["track"] == e["track"]]
        for c in crit:
            vals = [x["criteria"].get(c.key) for x in same_track if x["criteria"].get(c.key) is not None]
            mine = e["criteria"].get(c.key)
            if mine is not None and vals:
                percentiles[c.key] = sum(1 for v in vals if v <= mine) / len(vals)
    n = len(entries)
    band = None
    if e:
        frac = e["rank"] / max(n, 1)
        band = "top 10%" if frac <= 0.10 else "top 25%" if frac <= 0.25 else "top half" if frac <= 0.5 else "bottom half"
    expl = (run.output.get("explanations") or {}).get(project.ref) if run else None
    if expl:
        # replace judge ids by the same pseudonyms used for feedback
        by_ref = {r.judge_role.ref or str(r.judge_role_id): labels[r.pk] for r in reviews}
        expl = {**expl, "lines": [{**l, "judge": by_ref.get(l["judge"], "A judge")} for l in expl["lines"]]}
    return {"entry": e, "n_projects": n, "band": band, "criteria": crit, "percentiles": percentiles,
            "feedback": feedback, "n_reviews": len(reviews), "explanation": expl}


def coverage_rows(event):
    by_p = defaultdict(list)
    for r in Review.objects.filter(event=event, status="submitted").select_related("project", "judge_role__user"):
        by_p[r.project_id].append(r)
    rows = []
    for p in Project.objects.filter(event=event, status="submitted", duplicate_of__isnull=True).select_related("team").order_by("ref"):
        rs = by_p.get(p.pk, [])
        texts = [r.feedback_for_team.strip() for r in rs if r.feedback_for_team.strip()]
        rows.append({"project": p, "reviews": len(rs), "with_text": len(texts),
                     "longest": max((len(t) for t in texts), default=0),
                     "pending": sum(1 for r in rs if r.moderation == "pending")})
    return rows


def pending_moderation(event) -> int:
    """Submitted reviews whose feedback awaits an organizer's moderation (organizer-facing count)."""
    return Review.objects.filter(event=event, status="submitted", moderation="pending").count()
