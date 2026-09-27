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


HERO_ROWS = [  # an illustration, not data: real unpublished rankings never appear publicly
    ("Lighthouse", 1, 3, 1.3, True), ("Tidepool", 1, 3, 1.9, True), ("Relay", 1, 4, 2.6, True),
    ("Cobalt", 3, 6, 4.4, False), ("Quarry", 4, 8, 5.8, False), ("Juniper", 6, 10, 8.1, False),
]
DEMO_ROLE_CARDS = [
    {"key": "organizer", "icon": "layout-dashboard", "title": "Organizer",
     "what": "Run Sample Hack 2026: stalled judges, a statistical tie, feedback coverage, results."},
    {"key": "judge", "icon": "gavel", "title": "Judge",
     "what": "Work through a batch in the keyboard-first console; compare pairs; get a signed protocol."},
    {"key": "participant", "icon": "user", "title": "Participant",
     "what": "See your team, your submission and, once results are out, your scorecard."},
]


@policy("public")
def home(request):
    events = Event.objects.filter(is_listed=True).annotate(
        n_projects=Count("projects", filter=Q(projects__status="submitted", projects__duplicate_of__isnull=True)))

    def x(rank):
        return round(108 + (rank - 1) * 32.4, 1)
    rows = [{"name": n, "lo": x(lo), "hi": x(hi), "at": x(at), "tie": tie, "y": 50 + i * 32}
            for i, (n, lo, hi, at, tie) in enumerate(HERO_ROWS)]
    return render(request, "public/home.html", {"events": events, "nav": "home", "hero_rows": rows,
                                                "demo_roles": DEMO_ROLE_CARDS})


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
    from quorum.intelligence import embed, semantic

    mode = "exact" if request.GET.get("mode") == "exact" else "smart"
    how = {}
    if q and mode == "smart" and embed.available():
        hits = semantic.search(search_projects(public_projects(ev), "", track, tag, sort), q)
        projects = [p for p, _s, _h in hits]
        how = {p.pk: h for p, _s, h in hits}
    else:
        projects = list(search_projects(public_projects(ev), q, track, tag, sort)[:200])
    for p in projects:
        p.match_how = how.get(p.pk, "")
    tracks = Track.objects.filter(event=ev) if ev else Track.objects.filter(event__is_listed=True).select_related("event")
    counts = dict(public_projects(ev).values_list("track__ref").annotate(n=Count("id")).values_list("track__ref", "n"))
    chips = [{"t": t, "n": counts.get(t.ref, 0)} for t in tracks if counts.get(t.ref)] if ev else []
    return render(request, "public/gallery.html", {
        "projects": projects, "ev": ev, "q": q, "track": track, "tag": tag, "sort": sort, "tracks": tracks,
        "chips": chips, "total": sum(counts.values()), "mode": mode, "smart_ok": embed.available(),
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
    if p.status == Project.Status.SUBMITTED and p.duplicate_of_id is None:
        from quorum.intelligence import semantic

        ctx["similar"] = semantic.similar(p, list(public_projects(p.event)), n=3)
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
def favicon(request):
    from django.shortcuts import redirect
    from django.templatetags.static import static

    return redirect(static("brand/favicon.ico"), permanent=True)


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
