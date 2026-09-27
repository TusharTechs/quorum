from typing import Optional

from ninja import Router, Schema

from quorum.events import services
from quorum.events.models import Team
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound

from .common import UUID_RE, get_event

router = Router(tags=["teams"])


class TeamIn(Schema):
    name: str


class CodeIn(Schema):
    code: str


def team_out(t: Team, include_members=True) -> dict:
    d = {"id": str(t.pk), "ref": t.ref, "name": t.name, "event": t.event.ref}
    if include_members:
        d["members"] = [{"name": m.user.display, "email": m.user.email, "role": m.role}
                        for m in t.members.select_related("user")]
    return d


def _team(request, tid) -> Team:
    t = Team.objects.select_related("event").filter(pk=tid).first() if UUID_RE.match(tid or "") else None
    if not t:
        raise NotFound("No such team.")
    if not (t.members.filter(user=request.user).exists() or request.actor.is_organizer(t.event)):
        raise Forbidden("Only the team and organizers can see this team.")
    return t


@router.post("/events/{e}/teams", response={201: dict}, summary="Create a team; returns a one-time invite code")
@policy("authenticated")
def create_team(request, e: str, payload: TeamIn):
    team, code = services.create_team(request.actor, get_event(e), payload.name)
    return 201, {**team_out(team), "invite_code": code, "invite_url": f"/invite/{code}"}


@router.get("/events/{e}/my-team", summary="The caller's team in an event")
@policy("authenticated")
def my_team(request, e: str):
    t = services.team_of(request.user, get_event(e))
    if not t:
        raise NotFound("You are not in a team for this event.")
    return team_out(t)


@router.get("/teams/{tid}", summary="Read a team (members and organizers)")
@policy("authenticated")
def read_team(request, tid: str):
    return team_out(_team(request, tid))


@router.post("/teams/{tid}/invite", summary="Rotate the invite code (the old link stops working)")
@policy("authenticated")
def rotate(request, tid: str):
    t = _team(request, tid)
    code = services.rotate_invite(request.actor, t)
    return {"invite_code": code, "invite_url": f"/invite/{code}"}


@router.post("/invites/accept", summary="Join a team with an invite code")
@policy("authenticated")
def accept(request, payload: CodeIn):
    return team_out(services.join_team(request.actor, payload.code))


@router.post("/teams/{tid}/leave", summary="Leave a team (before the deadline)")
@policy("authenticated")
def leave(request, tid: str):
    services.leave_team(request.actor, _team(request, tid))
    return {"ok": True}
