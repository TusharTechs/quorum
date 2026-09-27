"""Organizer-side judge management and judge-side reviewing."""

from typing import Optional

from ninja import Router, Schema

from quorum.events import organize
from quorum.events.models import EventRole, Project
from quorum.judging import ops
from quorum.judging import services as judging
from quorum.judging.models import Conflict, Review
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound
from quorum.policy.repos import judge_assignment

from .common import get_event

router = Router(tags=["judging"])


class InviteIn(Schema):
    email: str
    name: str = ""
    tracks: list[str] = []
    capacity: Optional[int] = None


class JudgePatch(Schema):
    available: Optional[bool] = None
    capacity: Optional[int] = None


class ConflictIn(Schema):
    judge: str
    team: str
    note: str = ""


class ReviewIn(Schema):
    scores: dict[str, float] = {}
    feedback_to_team: Optional[str] = None
    note_to_organizers: Optional[str] = None
    quotable: Optional[str] = None
    active_seconds: int = 0
    submit: bool = False


class RecuseIn(Schema):
    note: str = ""


def _judge(ev, key) -> EventRole:
    r = EventRole.objects.filter(event=ev, role="judge").filter(ref=key).first() or \
        EventRole.objects.filter(event=ev, role="judge", pk=key if len(key) > 30 else None).first()
    if not r:
        raise NotFound("No such judge.")
    return r


@router.get("/events/{e}/judges", summary="Judges with progress and status (organizers)")
@policy("authenticated")
def list_judges(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return [{"ref": p["ref"], "name": p["name"], "email": p["email"], "tracks": p["tracks"], "assigned": p["assigned"],
             "submitted": p["submitted"], "pending": p["pending"], "status": p["status"],
             "last_activity": p["last_activity"], "available": p["role"].available} for p in ops.judge_progress(ev)]


@router.post("/events/{e}/judges", response={201: dict}, summary="Invite a judge (magic-link e-mail)")
@policy("authenticated")
def invite(request, e: str, payload: InviteIn):
    r = organize.invite_judge(request.actor, get_event(e), payload.email, payload.name, payload.tracks, payload.capacity)
    return 201, {"ref": r.ref, "email": r.user.email}


@router.patch("/events/{e}/judges/{j}", summary="Mark a judge available/unavailable or change capacity")
@policy("authenticated")
def patch_judge(request, e: str, j: str, payload: JudgePatch):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    r = _judge(ev, j)
    if payload.available is not None:
        ops.set_availability(ev, request.actor, r, payload.available)
    if payload.capacity is not None:
        r.capacity = payload.capacity
        r.save(update_fields=["capacity"])
    return {"ref": r.ref, "available": r.available, "capacity": r.capacity}


@router.get("/events/{e}/conflicts", summary="Conflicts of interest (organizers)")
@policy("authenticated")
def conflicts(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return [{"id": str(c.pk), "judge": c.judge_role.ref, "team": c.team.ref, "source": c.source, "status": c.status,
             "note": c.note} for c in Conflict.objects.filter(event=ev).select_related("judge_role", "team")]


@router.post("/events/{e}/conflicts", response={201: dict}, summary="Record a confirmed conflict (organizers)")
@policy("authenticated")
def add_conflict(request, e: str, payload: ConflictIn):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    from quorum.events.models import Team

    team = Team.objects.filter(event=ev, ref=payload.team).first()
    if not team:
        raise NotFound("No such team.")
    c, _ = Conflict.objects.update_or_create(judge_role=_judge(ev, payload.judge), team=team, defaults={
        "event": ev, "source": "organizer", "status": "confirmed", "note": payload.note[:300]})
    return 201, {"id": str(c.pk), "status": c.status}


@router.get("/me/assignments", summary="The caller's review queue")
@policy("authenticated")
def my_assignments(request, event: str = None):
    from quorum.judging.models import Assignment

    qs = Assignment.objects.filter(judge_role__user=request.user).exclude(status="reassigned").select_related(
        "project", "event", "project__track")
    if event:
        qs = qs.filter(event=get_event(event))
    if not request.user.event_roles.filter(role="judge").exists():
        raise Forbidden("Only judges have assignments.")
    return [{"id": str(a.pk), "event": a.event.ref, "project": a.project.ref, "title": a.project.title,
             "track": a.project.track.name if a.project.track else None, "status": a.status,
             "due_at": a.due_at} for a in qs.order_by("status", "assigned_at")]


@router.get("/assignments/{aid}/review", summary="Read my review draft for an assignment")
@policy("authenticated")
def get_review(request, aid: str):
    a = judge_assignment(request.actor, aid)
    rv = Review.objects.filter(assignment=a).first()
    if not rv:
        return {"status": "not_started", "scores": {}}
    return {"status": rv.status, "scores": {s.criterion.key: float(s.value) for s in rv.scores.select_related("criterion")},
            "feedback_to_team": rv.feedback_to_team, "note_to_organizers": rv.note_to_organizers, "quotable": rv.quotable}


@router.put("/assignments/{aid}/review", summary="Save (and optionally submit) my review")
@policy("authenticated")
def put_review(request, aid: str, payload: ReviewIn):
    a = judge_assignment(request.actor, aid)
    rv = judging.save_review(request.actor, a, scores=payload.scores, feedback=payload.feedback_to_team,
                             note=payload.note_to_organizers, quotable=payload.quotable,
                             active_seconds=payload.active_seconds, submit=payload.submit)
    return {"status": rv.status, "moderation": rv.moderation}


@router.post("/assignments/{aid}/recuse", summary="Declare a conflict of interest and hand the project back")
@policy("authenticated")
def recuse(request, aid: str, payload: RecuseIn):
    a = judge_assignment(request.actor, aid)
    ops.recuse(request.actor, a, payload.note)
    return {"ok": True}
