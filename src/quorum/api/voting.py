from typing import Optional

from ninja import Router, Schema

from quorum.core.clock import now
from quorum.events.models import Comment, Project
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound
from quorum.policy.repos import public_projects
from quorum.voting import services as voting
from quorum.voting.models import IntegrityFlag

from .common import find_project, get_event

router = Router(tags=["voting & comments"])


class EmailIn(Schema):
    email: str


class CodeIn(Schema):
    code: str


class VoteIn(Schema):
    project: str
    ballot: str
    weight: int = 1


class CodesIn(Schema):
    n: int = 20
    batch: str = ""


class ResolveIn(Schema):
    flags: list[str]
    action: str
    note: str


class CommentIn(Schema):
    body: str


class HideIn(Schema):
    reason: str = ""


@router.get("/events/{e}/ballot", summary="My ballot: shuffled order and a signed ballot token")
@policy("public")
def ballot(request, e: str):
    ev = get_event(e)
    voter = voting.ensure_user_voter(request, ev) if ev.voting_mode == "authenticated" else voting.current_voter(request, ev)
    if voter is None:
        raise Forbidden("Verify your identity first (e-mail link, code, or sign-in).", code="voter_unverified")
    order = voting.ballot_order(ev, voter, list(public_projects(ev)))
    voted = set(voter.votes.filter(status="valid").values_list("project_id", flat=True))
    return {"token": voting.ballot_token(ev, voter), "open": ev.voting_open(now()),
            "votes_left": ev.votes_per_voter - len(voted),
            "projects": [{"id": str(p.pk), "ref": p.ref, "title": p.title, "voted": p.pk in voted} for p in order]}


@router.post("/events/{e}/voters/email", summary="Start e-mail verification (a one-time link is sent)")
@policy("public")
def verify_email(request, e: str, payload: EmailIn):
    voting.start_email_verification(request, get_event(e), payload.email)
    return {"sent": True}


@router.post("/events/{e}/votes", response={201: dict}, summary="Cast a vote (ballot token required)")
@policy("public")
def cast(request, e: str, payload: VoteIn):
    ev = get_event(e)
    voter = voting.ensure_user_voter(request, ev) if ev.voting_mode == "authenticated" else voting.current_voter(request, ev)
    if voter is None:
        raise Forbidden("Verify your identity first.", code="voter_unverified")
    p = Project.objects.filter(event=ev).filter(ref=payload.project).first() or \
        Project.objects.filter(event=ev, pk=payload.project if len(payload.project) > 30 else None).first()
    if not p:
        raise NotFound("Unknown project.")
    v = voting.cast_vote(request, ev, voter, p, payload.ballot, payload.weight)
    return 201, {"id": str(v.pk), "ballot_position": v.ballot_position}


@router.get("/events/{e}/votes/tally", summary="Tallies (organizers during the window; public after close+publish)")
@policy("public")
def tally(request, e: str):
    ev = get_event(e)
    if not voting.tallies_visible_to(request.actor, ev):
        raise Forbidden("Tallies are hidden until voting closes and results are published.", code="tally_hidden")
    return [{"project": t["project"].ref, "title": t["project"].title, "valid": t["valid"], "raw": t["raw"],
             "voided": t["voided"]} for t in voting.tallies(ev)]


@router.post("/events/{e}/vote-codes", summary="Issue one-time voting codes (shown once)")
@policy("authenticated")
def codes(request, e: str, payload: CodesIn):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return {"codes": voting.issue_codes(request.actor, ev, payload.n, payload.batch)}


@router.get("/events/{e}/integrity-flags", summary="Voting integrity flags with evidence (organizers)")
@policy("authenticated")
def flags(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    voting.scan_flags(ev)
    return [{"id": str(f.pk), "rule": f.rule, "severity": f.severity, "summary": f.summary, "evidence": f.evidence,
             "votes": len(f.vote_ids), "status": f.status} for f in IntegrityFlag.objects.filter(event=ev)]


@router.post("/events/{e}/integrity-flags/resolve", summary="Keep, void or dismiss flagged votes (with a reason)")
@policy("authenticated")
def resolve(request, e: str, payload: ResolveIn):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return {"resolved": voting.resolve_flags(request.actor, ev, payload.flags, payload.action, payload.note)}


@router.get("/projects/{pid}/comments", summary="Visible comments on a project")
@policy("public")
def comments(request, pid: str):
    p = find_project(pid)
    return [{"id": str(c.pk), "author": c.author.display, "body_html": c.body_html, "created_at": c.created_at}
            for c in p.comments.filter(hidden_at__isnull=True).select_related("author")]


@router.post("/projects/{pid}/comments", response={201: dict}, summary="Comment on a project")
@policy("authenticated")
def add_comment(request, pid: str, payload: CommentIn):
    c = voting.add_comment(request, find_project(pid), payload.body)
    return 201, {"id": str(c.pk)}


@router.post("/comments/{cid}/hide", summary="Hide a comment (organizers; audited)")
@policy("authenticated")
def hide(request, cid: str, payload: HideIn):
    c = Comment.objects.select_related("project__event").filter(pk=cid).first()
    if not c:
        raise NotFound("No such comment.")
    voting.hide_comment(request.actor, c, payload.reason)
    return {"ok": True}
