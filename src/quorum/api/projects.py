
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db.models import Q
from ninja import Router

from quorum.events import services
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden
from quorum.policy.repos import can_view_project, public_projects, team_projects

from .common import find_project, get_event
from .schemas import ProjectIn, ProjectPublic, ProjectTeamView
from .serialize import project_public, project_team

router = Router(tags=["projects"])


def search_projects(qs, q: str | None, track: str | None, tag: str | None, sort: str | None):
    if track:
        qs = qs.filter(Q(track__ref=track) | Q(track__name__iexact=track))
    if tag:
        qs = qs.filter(tech_tags__contains=[tag])
    if q:
        vector = SearchVector("title", weight="A") + SearchVector("tagline", weight="B") + \
            SearchVector("description_md", weight="C") + SearchVector("team__name", weight="B")
        query = SearchQuery(q, search_type="websearch")
        qs = qs.annotate(rank=SearchRank(vector, query)).filter(
            Q(rank__gt=0.01) | Q(title__icontains=q) | Q(tagline__icontains=q) | Q(team__name__icontains=q)
        ).order_by("-rank", "title")
        return qs
    order = {"title": ("title",), "newest": ("-submitted_at", "title"), "track": ("track__position", "title")}
    return qs.order_by(*order.get(sort or "", ("submitted_at", "title")))


@router.get("/events/{e}/projects", response=list[ProjectPublic], summary="Public gallery of an event")
@policy("public")
def list_projects(request, e: str, q: str = None, track: str = None, tag: str = None, sort: str = None):
    """Submitted, canonical projects only. Drafts and duplicates never appear."""
    ev = get_event(e)
    return [project_public(p) for p in search_projects(public_projects(ev), q, track, tag, sort)]


@router.post("/events/{e}/projects", response={201: ProjectTeamView}, summary="Create a submission (draft or submit)")
@policy("authenticated")
def create_project(request, e: str, payload: ProjectIn):
    """Refused with 403 `deadline_passed` once the event has closed, before the body is
    even validated, so a late submission is always refused for the right reason."""
    ev = get_event(e)
    data = payload.dict(exclude_none=True)
    submit = data.pop("submit", False)
    p = services.create_project(request.actor, ev, data, submit=submit)
    return 201, project_team(p)


@router.get("/projects/{pid}", response=ProjectTeamView | ProjectPublic, summary="Read a project")
@policy("public")
def read_project(request, pid: str):
    p = find_project(pid)
    if not can_view_project(request.actor, p):
        raise Forbidden("This project is not public.")
    if team_projects(request.actor).filter(pk=p.pk).exists() or request.actor.is_organizer(p.event):
        return project_team(p)
    return project_public(p)


@router.patch("/projects/{pid}", response=ProjectTeamView, summary="Edit a submission (until the deadline)")
@policy("authenticated")
def update_project(request, pid: str, payload: ProjectIn):
    p = find_project(pid)
    data = payload.dict(exclude_none=True)
    data.pop("submit", None)
    return project_team(services.update_project(request.actor, p, data))


@router.post("/projects/{pid}/submit", response=ProjectTeamView, summary="Submit the draft")
@policy("authenticated")
def submit_project(request, pid: str):
    return project_team(services.submit_project(request.actor, find_project(pid)))


@router.post("/projects/{pid}/withdraw", response=ProjectTeamView, summary="Withdraw a submission")
@policy("authenticated")
def withdraw_project(request, pid: str):
    return project_team(services.withdraw_project(request.actor, find_project(pid)))


@router.get("/projects/{pid}/revisions", summary="Revision history (team and organizers)")
@policy("authenticated")
def revisions(request, pid: str):
    p = find_project(pid)
    if not (team_projects(request.actor).filter(pk=p.pk).exists() or request.actor.is_organizer(p.event)):
        raise Forbidden("Only the team and organizers can read the revision history.")
    return [{"version": r.version, "content_hash": r.content_hash, "created_at": r.created_at,
             "author": str(r.author) if r.author else None} for r in p.revisions.all()]
