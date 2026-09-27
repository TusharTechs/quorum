
from ninja import Router, Schema

from quorum.events import services as ev_services
from quorum.events.models import Project
from quorum.judging import feedback as fb
from quorum.judging.models import Review
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound
from quorum.results.service import latest_run

from .common import get_event

router = Router(tags=["feedback"])


class ModerateIn(Schema):
    action: str
    text: str = ""
    note: str = ""


@router.get("/events/{e}/feedback", summary="Feedback items and coverage (organizers)")
@policy("authenticated")
def list_feedback(request, e: str, status: str = "pending"):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    qs = Review.objects.filter(event=ev, status="submitted").exclude(feedback_to_team="").select_related("project", "judge_role")
    if status != "all":
        qs = qs.filter(moderation=status)
    return {"items": [{"review": str(r.pk), "project": r.project.ref, "judge": r.judge_role.ref, "text": r.feedback_to_team,
                       "moderation": r.moderation, "quotable": r.quotable} for r in qs],
            "coverage": [{"project": x["project"].ref, "with_text": x["with_text"], "reviews": x["reviews"]}
                         for x in fb.coverage_rows(ev)]}


@router.post("/reviews/{rid}/moderate", summary="Approve, edit or hide one judge's feedback")
@policy("authenticated")
def moderate(request, rid: str, payload: ModerateIn):
    rv = Review.objects.select_related("event", "project").filter(pk=rid).first()
    if not rv:
        raise NotFound("No such review.")
    request.actor.require_organizer(rv.event)
    fb.moderate(request.actor, rv, payload.action, payload.text, payload.note)
    return {"moderation": rv.moderation}


@router.post("/events/{e}/feedback/approve-all", summary="Approve every pending feedback item")
@policy("authenticated")
def approve_all(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return {"approved": fb.approve_all_pending(request.actor, ev)}


@router.post("/events/{e}/feedback/release", summary="Release scorecards and feedback to every team (e-mails)")
@policy("authenticated")
def release(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return {"recipients": fb.release_feedback(ev, request.actor)}


@router.get("/me/scorecard", summary="My team's scorecard (after release)")
@policy("authenticated")
def my_scorecard(request, event: str):
    ev = get_event(event)
    team = ev_services.team_of(request.user, ev)
    if not team:
        raise Forbidden("Scorecards are for teams in this event.")
    if not ev.feedback_released_at:
        raise Forbidden("Feedback has not been released yet.", code="not_released")
    p = Project.objects.filter(event=ev, team=team, duplicate_of__isnull=True).exclude(status="withdrawn").first()
    if not p:
        raise NotFound("Your team has no submission.")
    pub = getattr(ev, "publication", None)
    sc = fb.scorecard(ev, p, pub.run if pub else latest_run(ev))
    e_ = sc["entry"] or {}
    place = (pub.final_order.index(p.ref) + 1) if pub and p.ref in (pub.final_order or []) else e_.get("rank")
    return {"project": p.ref, "title": p.title, "calibrated": e_.get("calibrated"), "se": e_.get("se"),
            "rank": place if ev.rank_display == "exact" else None,
            "band": sc["band"] if ev.rank_display != "none" else None, "criteria": e_.get("criteria"),
            "percentiles_in_track": sc["percentiles"], "feedback": sc["feedback"], "explanation": sc["explanation"]}
