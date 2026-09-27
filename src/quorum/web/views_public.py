from __future__ import annotations

import mimetypes
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import render

from quorum.api.common import get_event
from quorum.api.projects import search_projects
from quorum.core.clock import now
from quorum.core.crypto import public_jwks
from quorum.events.models import Event, Project, Track
from quorum.judging import method as methods
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound
from quorum.policy.repos import can_view_project, public_projects, team_projects


def _phase_steps(ev: Event):
    order = ["registration", "submissions", "closed", "judging", "focus", "deliberation", "locked", "published"]
    labels = {"registration": "Registration", "submissions": "Submissions", "closed": "Closed", "judging": "Judging",
              "focus": "Focus round", "deliberation": "Deliberation", "locked": "Locked", "published": "Published"}
    cur = effective_phase(ev)
    idx = order.index(cur) if cur in order else 0
    return [{"label": labels[p], "state": "done" if i < idx else ("now" if i == idx else "")} for i, p in enumerate(order)]


def effective_phase(ev: Event) -> str:
    """Phase shown to people: manual gates (judging, locked, published...) plus the clock."""
    t = now()
    if ev.phase in ("judging", "focus", "deliberation", "locked", "published", "archived"):
        return ev.phase
    if t < ev.submissions_open_at:
        return "registration"
    if t < ev.effective_close:
        return "submissions"
    return "closed"


@policy("public")
def home(request):
    events = Event.objects.filter(is_listed=True).annotate(
        n_projects=Count("projects", filter=Q(projects__status="submitted", projects__duplicate_of__isnull=True)))
    return render(request, "public/home.html", {"events": events, "nav": "home"})


@policy("public")
def events_list(request):
    events = Event.objects.filter(is_listed=True).annotate(
        n_projects=Count("projects", filter=Q(projects__status="submitted", projects__duplicate_of__isnull=True)))
    return render(request, "public/events.html", {"events": events, "nav": "events", "now": now()})


@policy("public")
def event_page(request, slug):
    ev = get_event(slug)
    m = methods.current(ev)
    actor = request.actor
    my_team = None
    if actor.authenticated:
        from quorum.events.services import team_of

        my_team = team_of(actor.user, ev)
    ctx = {
        "ev": ev, "method": m, "phase": effective_phase(ev), "steps": _phase_steps(ev),
        "criteria": ev.criteria.order_by("position"), "tracks": ev.tracks.all(), "prizes": ev.prizes.all(),
        "n_projects": public_projects(ev).count(), "is_org": actor.is_organizer(ev),
        "is_judge": actor.has(ev, "judge"), "my_team": my_team, "now": now(), "nav": "events",
        "submissions_open": ev.submissions_open(now()),
    }
    return render(request, "public/event.html", ctx)


def _gallery(request, ev=None):
    q = request.GET.get("q", "").strip()
    track = request.GET.get("track", "")
    tag = request.GET.get("tag", "")
    sort = request.GET.get("sort", "")
    qs = search_projects(public_projects(ev), q, track, tag, sort)
    projects = list(qs[:200])
    tracks = Track.objects.filter(event=ev) if ev else Track.objects.filter(event__is_listed=True).select_related("event")
    return render(request, "public/gallery.html", {
        "projects": projects, "ev": ev, "q": q, "track": track, "tag": tag, "sort": sort, "tracks": tracks,
        "events": Event.objects.filter(is_listed=True) if not ev else None, "nav": "gallery",
    })


@policy("public")
def gallery(request):
    """The public gallery (acceptance check T1). Server-rendered: no JavaScript needed."""
    ev = None
    if request.GET.get("event"):
        ev = get_event(request.GET["event"])
    return _gallery(request, ev)


@policy("public")
def event_gallery(request, slug):
    return _gallery(request, get_event(slug))


@policy("public")
def project_page(request, pid):
    p = Project.objects.select_related("team", "track", "event").filter(pk=pid).first()
    if p is None:
        raise NotFound("No such project.")
    actor = request.actor
    if not can_view_project(actor, p):
        raise Forbidden("This project is not public.")
    from quorum.voting import services as voting

    is_member = team_projects(actor).filter(pk=p.pk).exists()
    ctx = {
        "p": p, "ev": p.event, "images": p.images.all(), "is_member": is_member,
        "is_org": actor.is_organizer(p.event),
        "answers": p.answers.select_related("question").order_by("question__position"),
        "comments": p.comments.filter(hidden_at__isnull=True).select_related("author"),
        "hidden_comments": p.comments.filter(hidden_at__isnull=False).count() if actor.is_organizer(p.event) else 0,
        "voting": voting.ballot_state(request, p.event), "nav": "gallery",
        "duplicate_of": p.duplicate_of,
    }
    return render(request, "public/project.html", ctx)


@policy("public")
def media(request, path):
    """Uploaded images. Drafts' images are only served to the team and organizers."""
    rel = Path(path)
    if ".." in rel.parts:
        raise Http404()
    full = (settings.MEDIA_ROOT / rel).resolve()
    if not str(full).startswith(str(settings.MEDIA_ROOT.resolve())) or not full.is_file():
        raise Http404()
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "projects":
        p = Project.objects.select_related("event").filter(pk=parts[1]).first()
        if p is None or not can_view_project(request.actor, p):
            raise Http404()
    ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
    resp = FileResponse(open(full, "rb"), content_type=ctype)
    resp["Content-Security-Policy"] = "default-src 'none'; sandbox"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, max-age=3600"
    return resp


@policy("public")
def methodology(request):
    return render(request, "public/methodology.html", {"nav": ""})


@policy("public")
def event_methodology(request, slug):
    ev = get_event(slug)
    m = methods.current(ev)
    return render(request, "public/event_methodology.html", {
        "ev": ev, "method": m, "criteria": ev.criteria.order_by("position"),
        "spec_json": methods.spec_json(m) if m else "", "versions": ev.methods.all(), "nav": "events",
        "comparative": ev.tracks.filter(pairwise=True),
    })


@policy("public")
def keys(request):
    resp = JsonResponse(public_jwks())
    resp["Access-Control-Allow-Origin"] = "*"
    return resp


@policy("public")
def revocations(request):
    from quorum.audit.certificates import revocation_list

    resp = JsonResponse(revocation_list())
    resp["Access-Control-Allow-Origin"] = "*"
    resp["Cache-Control"] = "no-store"
    return resp


@policy("public")
def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


@policy("public")
def readyz(request):
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
    return JsonResponse({"ok": True, "db": True, "time": now().isoformat()})


@policy("public")
def embed_gallery(request, slug):
    """Embeddable gallery: frameable (CSP frame-ancestors *), rendered as an anonymous
    visitor whoever is looking, so it can never show a draft or private data."""
    ev = get_event(slug)
    projects = list(public_projects(ev).order_by("title")[:200])
    resp = render(request, "public/embed.html", {"ev": ev, "projects": projects})
    return resp
