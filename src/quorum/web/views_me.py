"""Participant pages: dashboard, team hub, invites, submission editor, scorecard."""

from __future__ import annotations

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render

from quorum.api.common import get_event
from quorum.audit.models import Certificate
from quorum.core.clock import now
from quorum.events import services as ev_services
from quorum.events.models import Comment, Event, Project, ProjectImage, TeamMember
from quorum.judging.feedback import scorecard
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound, PolicyError
from quorum.policy.repos import team_projects
from quorum.results.service import latest_run
from quorum.voting import services as voting


def _flash_error(request, e: PolicyError):
    fields = e.extra.get("fields") if isinstance(e.extra, dict) else None
    messages.error(request, e.detail)
    return fields or {}


@policy("authenticated")
def dashboard(request):
    memberships = TeamMember.objects.filter(user=request.user).select_related("team", "event")
    rows = []
    for m in memberships:
        p = Project.objects.filter(team=m.team, duplicate_of__isnull=True).exclude(status="withdrawn").first()
        rows.append({"m": m, "event": m.event, "project": p,
                     "open": m.event.submissions_open(now()), "released": bool(m.event.feedback_released_at)})
    roles = request.user.event_roles.select_related("event").exclude(role="participant")
    open_events = Event.objects.filter(is_listed=True, submissions_close_at__gt=now()).exclude(
        pk__in=[r["event"].pk for r in rows])
    certs = Certificate.objects.filter(user=request.user).select_related("event")
    return render(request, "me/dashboard.html", {"rows": rows, "roles": roles, "open_events": open_events,
                                                 "certs": certs, "nav": "me"})


@policy("authenticated")
def team_hub(request, slug):
    ev = get_event(slug)
    actor = request.actor
    team = ev_services.team_of(actor.user, ev)
    invite_url = request.session.pop(f"invite_{ev.pk}", None)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                team, code = ev_services.create_team(actor, ev, request.POST.get("name", ""))
                request.session[f"invite_{ev.pk}"] = f"{request.build_absolute_uri('/invite/')}{code}"
                messages.success(request, f"Team {team.name} created. Share the invite link with your teammates.")
            elif action == "rotate" and team:
                code = ev_services.rotate_invite(actor, team)
                request.session[f"invite_{ev.pk}"] = f"{request.build_absolute_uri('/invite/')}{code}"
                messages.success(request, "New invite link created; the old one no longer works.")
            elif action == "leave" and team:
                ev_services.leave_team(actor, team)
                messages.success(request, "You left the team.")
        except PolicyError as e:
            _flash_error(request, e)
        return redirect(f"/me/e/{ev.slug}")
    project = None
    if team:
        project = Project.objects.filter(team=team, duplicate_of__isnull=True).exclude(status="withdrawn").first()
    return render(request, "me/team.html", {
        "ev": ev, "team": team, "members": team.members.select_related("user") if team else [],
        "project": project, "invite_url": invite_url, "open": ev.submissions_open(now()), "now": now(), "nav": "me",
    })


@policy("public")
def invite(request, code):
    team = ev_services.find_team_by_code(code)
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect(f"/login?next=/invite/{code}")
        try:
            ev_services.join_team(request.actor, code)
            messages.success(request, f"You joined {team.name}.")
            return redirect(f"/me/e/{team.event.slug}")
        except PolicyError as e:
            _flash_error(request, e)
    return render(request, "me/invite.html", {"team": team, "ev": team.event, "code": code,
                                              "members": team.members.select_related("user"), "nav": "me"})


def _form_data(request):
    d = {k: request.POST.get(k, "") for k in ("title", "tagline", "description_md", "demo_video_url", "repo_url",
                                              "live_url", "declared_commit_sha", "tech_tags", "track")}
    answers = {k[2:]: v for k, v in request.POST.items() if k.startswith("q_")}
    if answers or request.POST.get("has_questions"):
        d["answers"] = answers
    return d


@policy("authenticated")
def new_submission(request, slug):
    ev = get_event(slug)
    if request.method == "POST":
        try:
            p = ev_services.create_project(request.actor, ev, _form_data(request))
            messages.success(request, "Draft saved. Keep editing until the deadline; submit when it is ready.")
            return redirect(f"/me/projects/{p.pk}/edit")
        except PolicyError as e:
            errors = _flash_error(request, e)
            return render(request, "me/edit.html", {"ev": ev, "p": None, "team": ev_services.team_of(request.user, ev), "data": request.POST, "errors": errors,
                                                    "tracks": ev.tracks.all(), "questions": ev.questions.all(),
                                                    "open": ev.submissions_open(now()), "nav": "me"}, status=e.status)
    ev_services.guard_submissions_open(ev)
    return render(request, "me/edit.html", {"ev": ev, "p": None, "team": ev_services.team_of(request.user, ev), "data": {}, "errors": {}, "tracks": ev.tracks.all(),
                                            "questions": ev.questions.all(), "open": True, "nav": "me"})


def _my_project(request, pid) -> Project:
    p = team_projects(request.actor).filter(pk=pid).select_related("event", "team", "track").first()
    if not p:
        raise Forbidden("Only members of this team can edit the submission.")
    return p


@policy("authenticated")
def edit_submission(request, pid):
    p = _my_project(request, pid)
    ev = p.event
    errors = {}
    if request.method == "POST":
        action = request.POST.get("action", "save")
        try:
            if action in ("save", "submit", "autosave"):
                ev_services.update_project(request.actor, p, _form_data(request))
            if action == "submit":
                ev_services.submit_project(request.actor, p)
                messages.success(request, "Submitted. You can keep editing until the deadline.")
            elif action == "withdraw":
                ev_services.withdraw_project(request.actor, p)
                messages.success(request, "Withdrawn.")
            elif action == "upload" and request.FILES.get("image"):
                ev_services.add_image(request.actor, p, request.FILES["image"])
                messages.success(request, "Image added.")
            elif action == "remove_image":
                img = ProjectImage.objects.filter(project=p, pk=request.POST.get("image")).first()
                if img:
                    ev_services.remove_image(request.actor, img)
            elif action == "save":
                messages.success(request, f"Saved (version {p.version}).")
            if action == "autosave":
                p.refresh_from_db()
                return JsonResponse({"ok": True, "version": p.version, "saved_at": now().isoformat()})
            return redirect(f"/me/projects/{p.pk}/edit")
        except PolicyError as e:
            if action == "autosave":
                return JsonResponse({"ok": False, "error": e.code, "detail": e.detail}, status=e.status)
            errors = _flash_error(request, e)
    p.refresh_from_db()
    data = {"title": p.title, "tagline": p.tagline, "description_md": p.description_md, "demo_video_url": p.demo_video_url,
            "repo_url": p.repo_url, "live_url": p.live_url, "declared_commit_sha": p.declared_commit_sha,
            "tech_tags": ", ".join(p.tech_tags or []), "track": p.track.ref if p.track else ""}
    answers = {a.question.ref: a.value for a in p.answers.select_related("question")}
    return render(request, "me/edit.html", {
        "ev": ev, "p": p, "data": data, "answers": answers, "errors": errors, "tracks": ev.tracks.all(),
        "questions": ev.questions.all(), "images": p.images.all(), "open": ev.submissions_open(now()),
        "missing": ev_services.missing_for_submission(p), "revisions": p.revisions.all()[:12], "nav": "me",
    })


@policy("authenticated")
def my_scorecard(request, slug):
    ev = get_event(slug)
    team = ev_services.team_of(request.user, ev)
    if not team:
        raise Forbidden("Scorecards are for teams in this event.")
    if not ev.feedback_released_at and not request.actor.is_organizer(ev):
        return render(request, "me/scorecard_pending.html", {"ev": ev, "team": team, "nav": "me"})
    p = Project.objects.filter(event=ev, team=team, duplicate_of__isnull=True).exclude(status="withdrawn").first()
    if not p:
        raise NotFound("Your team has no submission in this event.")
    pub = getattr(ev, "publication", None)
    run = pub.run if pub else latest_run(ev)
    sc = scorecard(ev, p, run)
    place = (pub.final_order.index(p.ref) + 1) if pub and p.ref in (pub.final_order or []) else None
    from quorum.intelligence.themes import themes

    return render(request, "me/scorecard.html", {"ev": ev, "team": team, "p": p, "sc": sc, "pub": pub, "place": place,
                                                 "themes": themes(sc["feedback"], sc["criteria"]), "nav": "me"})


@policy("authenticated")
def post_comment(request, pid):
    p = Project.objects.select_related("event").filter(pk=pid).first()
    if not p or p.status != "submitted":
        raise NotFound("No such project.")
    try:
        voting.add_comment(request, p, request.POST.get("body", ""))
    except PolicyError as e:
        _flash_error(request, e)
    return redirect(f"/p/{p.pk}#comments")


@policy("authenticated")
def hide_comment(request, cid):
    c = Comment.objects.select_related("project__event").filter(pk=cid).first()
    if not c:
        raise NotFound("No such comment.")
    voting.hide_comment(request.actor, c, request.POST.get("reason", ""))
    messages.success(request, "Comment hidden (recorded in the audit log).")
    return redirect(f"/p/{c.project_id}")
