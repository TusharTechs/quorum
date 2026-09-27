"""Comparative (pairwise) judging per track: organizer switch and leaderboard, judge pairs."""

from ninja import Router, Schema

from quorum.events.models import Project
from quorum.judging import pairwise
from quorum.judging.services import record_comparison
from quorum.policy.decorators import policy
from quorum.policy.errors import NotFound

from .common import get_event

router = Router(tags=["judging"])


class ModeIn(Schema):
    enabled: bool


class PairIn(Schema):
    a: str
    b: str
    outcome: str
    reason: str = ""


@router.get("/events/{e}/pairwise", summary="Comparative judging: Bradley-Terry order per track beside the rubric")
@policy("authenticated")
def boards(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return {"tracks": pairwise.event_board(ev),
            "note": "Advisory: comparisons never change the official (rubric) ranking; only tie-breaks fuse them."}


@router.post("/events/{e}/tracks/{track}/pairwise", summary="Switch comparative judging on or off for a track")
@policy("authenticated")
def set_mode(request, e: str, track: str, payload: ModeIn):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    t = pairwise.set_mode(ev, request.actor, pairwise.get_track(ev, track), payload.enabled)
    return {"track": t.ref, "enabled": t.pairwise}


@router.get("/me/events/{e}/pairwise", summary="Judge: my comparative tracks, progress and next pair")
@policy("authenticated")
def my_pairs(request, e: str):
    ev = get_event(e)
    role = request.actor.require_judge(ev)
    return {"tracks": [{"track": s["track"].ref, "name": s["track"].name, "pool": s["pool"], "done": s["done"],
                        "target": s["target"], "pairs": s["pairs"], "ready": s["ready"],
                        "next": list(s["pair"]) if s["pair"] else None}
                       for s in pairwise.judge_tracks(ev, role)]}


@router.post("/me/events/{e}/pairwise", response={201: dict},
             summary="Judge: record a comparison between two projects I reviewed (a, b or tie)")
@policy("authenticated")
def compare(request, e: str, payload: PairIn):
    ev = get_event(e)
    role = request.actor.require_judge(ev)
    pa = Project.objects.filter(event=ev, ref=payload.a).select_related("track").first()
    pb = Project.objects.filter(event=ev, ref=payload.b).select_related("track").first()
    if not pa or not pb:
        raise NotFound("Unknown project.")
    c = record_comparison(request.actor, ev, role, pa, pb, payload.outcome, reason=payload.reason)
    return 201, {"id": str(c.pk)}
