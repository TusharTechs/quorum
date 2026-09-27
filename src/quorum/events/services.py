"""T1 business rules: teams, invites, submissions, deadline.

Every public function takes the acting user (policy.Actor) and enforces, in this order:
authentication -> role -> state (deadline/window) -> object checks -> input validation.
The order matters: a late submission is refused because the deadline passed, never because
of a missing field, so the refusal is always for the right reason (see tests/api/test_deadline).
"""

from __future__ import annotations

import hashlib
import io
import re
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.text import slugify

from engine.bundle import canonical_json, sha256_hex
from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.core.text import clean_line, render_markdown
from quorum.policy.errors import Conflict, DeadlinePassed, Forbidden, Invalid, NotFound, WindowClosed

from .models import CustomQuestion, Event, EventRole, Project, ProjectAnswer, ProjectImage, ProjectRevision, Team, TeamMember, Track

CONTENT_FIELDS = ("title", "tagline", "description_md", "demo_video_url", "repo_url", "live_url",
                  "declared_commit_sha", "tech_tags", "track")


# --------------------------------------------------------------------------- deadline

def guard_submissions_open(event: Event, at=None):
    at = at or now()
    if at >= event.effective_close:
        raise DeadlinePassed(
            f"Submissions for {event.name} closed at {event.submissions_close_at.isoformat()}.",
            extra={"closed_at": event.submissions_close_at.isoformat().replace("+00:00", "Z")},
        )
    if at < event.submissions_open_at:
        raise WindowClosed("Submissions are not open yet.", code="submissions_not_open",
                           extra={"opens_at": event.submissions_open_at.isoformat()})


def guard_team_changes_open(event: Event, at=None):
    """Teams may form and change until submissions close, never after."""
    at = at or now()
    if at >= event.effective_close:
        raise DeadlinePassed("Teams are frozen: submissions have closed.",
                             extra={"closed_at": event.submissions_close_at.isoformat()})


# --------------------------------------------------------------------------- teams

def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def team_of(user, event) -> Team | None:
    if not user:
        return None
    m = TeamMember.objects.select_related("team").filter(event=event, user=user).first()
    return m.team if m else None


def _next_ref(model, event, prefix):
    n = model.objects.filter(event=event).count() + 1
    while model.objects.filter(event=event, ref=f"{prefix}_{n:02d}").exists():
        n += 1
    return f"{prefix}_{n:02d}"


@transaction.atomic
def create_team(actor, event: Event, name: str) -> tuple[Team, str]:
    actor.require_auth()
    guard_team_changes_open(event)
    if actor.has(event, "judge"):
        raise Forbidden("Judges of this event cannot join a team in it.")
    name = clean_line(name, 80)
    if len(name) < 2:
        raise Invalid("Team name must be at least 2 characters.", extra={"fields": {"name": "too short"}})
    if team_of(actor.user, event):
        raise Conflict("You are already in a team for this event.")
    code = secrets.token_urlsafe(12)
    try:
        with transaction.atomic():
            team = Team.objects.create(event=event, ref=_next_ref(Team, event, "tm"), name=name,
                                       created_by=actor.user, invite_code_hash=_code_hash(code),
                                       invite_expires_at=event.effective_close)
            TeamMember.objects.create(team=team, event=event, user=actor.user, role=TeamMember.Role.OWNER)
    except IntegrityError:
        raise Conflict("You already have a team for this event.")
    EventRole.objects.get_or_create(event=event, user=actor.user, role=EventRole.Role.PARTICIPANT)
    audit.record("TEAM_CREATED", f"{actor.user} created team {team.name}", event=event, actor=actor,
                 actor_role="participant", target=team)
    return team, code


@transaction.atomic
def rotate_invite(actor, team: Team) -> str:
    actor.require_auth()
    if not TeamMember.objects.filter(team=team, user=actor.user).exists() and not actor.is_organizer(team.event):
        raise Forbidden("Only team members can create invite links.")
    guard_team_changes_open(team.event)
    code = secrets.token_urlsafe(12)
    team.invite_code_hash = _code_hash(code)
    team.invite_expires_at = team.event.effective_close
    team.save(update_fields=["invite_code_hash", "invite_expires_at"])
    audit.record("TEAM_INVITE_ROTATED", f"Invite link for {team.name} rotated", event=team.event, actor=actor,
                 actor_role="participant", target=team)
    return code


def find_team_by_code(code: str) -> Team:
    team = Team.objects.select_related("event").filter(invite_code_hash=_code_hash(code or "")).first()
    if not team:
        raise NotFound("This invite link is not valid. Ask your team for a fresh one.")
    return team


@transaction.atomic
def join_team(actor, code: str) -> Team:
    actor.require_auth()
    team = find_team_by_code(code)
    event = team.event
    guard_team_changes_open(event)
    if team.invite_expires_at and now() > team.invite_expires_at:
        raise Conflict("This invite link has expired.")
    if actor.has(event, "judge"):
        raise Forbidden("Judges of this event cannot join a team in it.")
    if team_of(actor.user, event):
        raise Conflict("You are already in a team for this event.")
    try:
        with transaction.atomic():
            TeamMember.objects.create(team=team, event=event, user=actor.user)
    except Exception as e:  # trigger: team full / judge; unique: already in a team
        msg = str(e).split("\n")[0]
        raise Conflict("Could not join: " + ("the team is full." if "full" in msg else msg))
    EventRole.objects.get_or_create(event=event, user=actor.user, role=EventRole.Role.PARTICIPANT)
    audit.record("TEAM_JOINED", f"{actor.user} joined {team.name}", event=event, actor=actor,
                 actor_role="participant", target=team)
    return team


@transaction.atomic
def leave_team(actor, team: Team):
    actor.require_auth()
    guard_team_changes_open(team.event)
    m = TeamMember.objects.filter(team=team, user=actor.user).first()
    if not m:
        raise Forbidden("You are not in this team.")
    if m.role == TeamMember.Role.OWNER and team.members.count() > 1:
        raise Conflict("Hand ownership to a teammate before leaving.")
    m.delete()
    audit.record("TEAM_LEFT", f"{actor.user} left {team.name}", event=team.event, actor=actor,
                 actor_role="participant", target=team)


# --------------------------------------------------------------------------- projects

URL_FIELDS = ("demo_video_url", "repo_url", "live_url")


def _clean_url(v: str, field: str, errors: dict):
    v = (v or "").strip()
    if not v:
        return ""
    u = urlparse(v)
    if u.scheme not in ("http", "https") or not u.netloc:
        errors[field] = "must be an http(s) URL"
    return v[:400]


def clean_project_data(event: Event, data: dict, partial: bool = False) -> dict:
    """Validate and normalise submission input (shared by the API and the HTML form)."""
    errors: dict[str, str] = {}
    out: dict = {}
    if "title" in data or not partial:
        title = clean_line(data.get("title", ""), 120)
        if len(title) < 2:
            errors["title"] = "required (2-120 characters)"
        out["title"] = title
    if "tagline" in data:
        out["tagline"] = clean_line(data.get("tagline", ""), 200)
    if "summary" in data and "tagline" not in data:
        out["tagline"] = clean_line(data.get("summary", ""), 200)
    if "description_md" in data or "description" in data:
        out["description_md"] = (data.get("description_md") or data.get("description") or "")[:20000]
    for f in URL_FIELDS:
        if f in data:
            out[f] = _clean_url(data.get(f), f, errors)
    if "declared_commit_sha" in data:
        sha = (data.get("declared_commit_sha") or "").strip()
        if sha and not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
            errors["declared_commit_sha"] = "must be a git commit hash"
        out["declared_commit_sha"] = sha
    if "tech_tags" in data:
        tags = data.get("tech_tags") or []
        if isinstance(tags, str):
            tags = [t for t in re.split(r"[,\n]", tags)]
        out["tech_tags"] = sorted({slugify(t)[:30] for t in tags if slugify(t)})[:12]
    if "track" in data or not partial:
        ref = data.get("track")
        if ref:
            t = Track.objects.filter(event=event).filter(ref=ref).first() or \
                Track.objects.filter(event=event, pk=ref if _is_uuid(ref) else None).first()
            if not t:
                errors["track"] = "unknown track"
            out["track"] = t
        elif event.tracks.exists() and not partial:
            errors["track"] = "choose a track"
    if "answers" in data:
        out["answers"] = _clean_answers(event, data.get("answers") or {}, errors)
    if errors:
        raise Invalid("Please fix the highlighted fields.", extra={"fields": errors})
    return out


def _is_uuid(v) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F-]{32,36}", str(v)))


def _clean_answers(event, answers: dict, errors: dict) -> dict:
    out = {}
    for q in event.questions.all():
        v = answers.get(q.ref)
        if q.kind == CustomQuestion.Kind.BOOL:
            v = bool(v) if v is not None else None
        elif q.kind == CustomQuestion.Kind.CHOICE:
            if v and v not in (q.options or []):
                errors[f"answers.{q.ref}"] = "not one of the options"
        elif q.kind == CustomQuestion.Kind.URL:
            v = _clean_url(v or "", f"answers.{q.ref}", errors)
        elif v is not None:
            v = str(v)[:4000]
        if q.required and (v is None or v == ""):
            errors[f"answers.{q.ref}"] = "required"
        out[q.ref] = v
    return out


def snapshot(project: Project) -> dict:
    return {
        "title": project.title, "tagline": project.tagline, "description_md": project.description_md,
        "demo_video_url": project.demo_video_url, "repo_url": project.repo_url, "live_url": project.live_url,
        "declared_commit_sha": project.declared_commit_sha, "tech_tags": project.tech_tags,
        "track": project.track.ref if project.track_id else None, "thumbnail": project.thumbnail,
        "images": [i.sha256 for i in project.images.all()],
        "answers": {a.question.ref: a.value for a in project.answers.select_related("question")},
    }


def _save_revision(project: Project, actor):
    snap = snapshot(project)
    h = sha256_hex(snap)
    project.version += 1
    project.content_hash = h
    Project.objects.filter(pk=project.pk).update(version=project.version, content_hash=h)
    ProjectRevision.objects.create(project=project, version=project.version, snapshot=snap, content_hash=h,
                                   author=getattr(actor, "user", None))
    return h


def _apply(project: Project, cleaned: dict):
    for f in ("title", "tagline", "description_md", "demo_video_url", "repo_url", "live_url",
              "declared_commit_sha", "tech_tags"):
        if f in cleaned:
            setattr(project, f, cleaned[f])
    if "track" in cleaned:
        project.track = cleaned["track"]
    if "description_md" in cleaned:
        project.description_html = render_markdown(cleaned["description_md"])


def _save_answers(project, answers: dict):
    for q in project.event.questions.all():
        if q.ref in answers:
            ProjectAnswer.objects.update_or_create(project=project, question=q, defaults={"value": answers[q.ref]})


@transaction.atomic
def create_project(actor, event: Event, data: dict, submit: bool = False) -> Project:
    actor.require_auth()
    if not (actor.has(event, "participant") or team_of(actor.user, event)):
        raise Forbidden("Only participants of this event can submit. Create or join a team first.")
    guard_submissions_open(event)  # BEFORE any body validation: late = refused for the right reason
    team = team_of(actor.user, event)
    if not team:
        raise Forbidden("Create or join a team first.")
    if Project.objects.filter(event=event, team=team, duplicate_of__isnull=True).exclude(
            status=Project.Status.WITHDRAWN).exists():
        raise Conflict("Your team already has a submission; edit it instead.", code="already_submitted")
    cleaned = clean_project_data(event, data, partial=False)
    project = Project(event=event, ref=_next_ref(Project, event, "prj"), team=team, status=Project.Status.DRAFT)
    _apply(project, cleaned)
    project.save()
    if "answers" in cleaned:
        _save_answers(project, cleaned["answers"])
    _save_revision(project, actor)
    audit.record("PROJECT_CREATED", f"{team.name} started “{project.title}”", event=event, actor=actor,
                 actor_role="participant", target=project)
    if submit:
        submit_project(actor, project)
    return project


def require_team_member(actor, project: Project):
    actor.require_auth()
    if not TeamMember.objects.filter(team_id=project.team_id, user=actor.user).exists():
        raise Forbidden("Only members of this team can change the submission.")


@transaction.atomic
def update_project(actor, project: Project, data: dict) -> Project:
    require_team_member(actor, project)
    guard_submissions_open(project.event)
    cleaned = clean_project_data(project.event, data, partial=True)
    _apply(project, cleaned)
    project.save()
    if "answers" in cleaned:
        _save_answers(project, cleaned["answers"])
    h = _save_revision(project, actor)
    audit.record("PROJECT_UPDATED", f"“{project.title}” saved (v{project.version})", event=project.event,
                 actor=actor, actor_role="participant", target=project, data={"content_hash": h})
    return project


def missing_for_submission(project: Project) -> list[str]:
    missing = []
    if not project.title:
        missing.append("title")
    if not project.tagline:
        missing.append("tagline")
    if not project.description_md:
        missing.append("description")
    if not project.repo_url:
        missing.append("repository URL")
    if project.event.tracks.exists() and not project.track_id:
        missing.append("track")
    answered = {a.question_id: a.value for a in project.answers.all()}
    for q in project.event.questions.filter(required=True):
        if answered.get(q.pk) in (None, ""):
            missing.append(q.label)
    return missing


@transaction.atomic
def submit_project(actor, project: Project) -> Project:
    require_team_member(actor, project)
    guard_submissions_open(project.event)
    missing = missing_for_submission(project)
    if missing:
        raise Invalid("Complete these fields before submitting: " + ", ".join(missing),
                      extra={"fields": {m: "required" for m in missing}})
    if project.status != Project.Status.SUBMITTED:
        project.status = Project.Status.SUBMITTED
        project.submitted_at = now()
        project.save(update_fields=["status", "submitted_at", "updated_at"])
        audit.record("PROJECT_SUBMITTED", f"“{project.title}” submitted by {project.team.name}",
                     event=project.event, actor=actor, actor_role="participant", target=project,
                     data={"content_hash": project.content_hash, "version": project.version})
    return project


@transaction.atomic
def withdraw_project(actor, project: Project) -> Project:
    require_team_member(actor, project)
    guard_submissions_open(project.event)
    project.status = Project.Status.WITHDRAWN
    project.save(update_fields=["status", "updated_at"])
    audit.record("PROJECT_WITHDRAWN", f"“{project.title}” withdrawn", event=project.event, actor=actor,
                 actor_role="participant", target=project)
    return project


# --------------------------------------------------------------------------- images

ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
MAX_BYTES = 5 * 1024 * 1024


@transaction.atomic
def add_image(actor, project: Project, upload) -> ProjectImage:
    """Images are decoded and re-encoded by Pillow: kills polyglots, strips EXIF/GPS,
    rejects SVG (script) and decompression bombs."""
    from PIL import Image

    require_team_member(actor, project)
    guard_submissions_open(project.event)
    if project.images.count() >= 8:
        raise Conflict("At most 8 images per project.")
    raw = upload.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise Invalid("Images must be 5 MB or smaller.")
    Image.MAX_IMAGE_PIXELS = 25_000_000
    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
        img = Image.open(io.BytesIO(raw))
        if img.format not in ALLOWED_FORMATS:
            raise Invalid("Use PNG, JPEG, WebP or GIF.")
        img = img.convert("RGB")
        img.thumbnail((1600, 1600))
    except Invalid:
        raise
    except Exception:
        raise Invalid("That file is not a readable image.")
    out = io.BytesIO()
    img.save(out, "WEBP", quality=82)
    data = out.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    rel = f"projects/{project.pk}/{digest[:24]}.webp"
    path = settings.MEDIA_ROOT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    pi = ProjectImage.objects.create(project=project, path=rel, position=project.images.count(),
                                     width=img.width, height=img.height, sha256=digest)
    if not project.thumbnail:
        project.thumbnail = rel
        project.save(update_fields=["thumbnail", "updated_at"])
    _save_revision(project, actor)
    audit.record("PROJECT_IMAGE_ADDED", f"Image added to “{project.title}”", event=project.event, actor=actor,
                 actor_role="participant", target=project, data={"sha256": digest})
    return pi


@transaction.atomic
def remove_image(actor, image: ProjectImage):
    project = image.project
    require_team_member(actor, project)
    guard_submissions_open(project.event)
    if project.thumbnail == image.path:
        nxt = project.images.exclude(pk=image.pk).first()
        project.thumbnail = nxt.path if nxt else ""
        project.save(update_fields=["thumbnail", "updated_at"])
    image.delete()
    _save_revision(project, actor)


# --------------------------------------------------------------------------- sealing

@transaction.atomic
def seal_submissions(event: Event):
    """At close: record every submission's content hash in the audit chain, so any later
    change is provable against the chain (the DB trigger also refuses such changes)."""
    if (event.options or {}).get("sealed_at"):
        return False
    hashes = {p.ref: p.content_hash for p in event.projects.filter(status=Project.Status.SUBMITTED).order_by("ref")}
    audit.record("SUBMISSIONS_SEALED", f"Submissions sealed: {len(hashes)} projects, content hashes recorded",
                 event=event, actor_role="system", data={"content_hashes": hashes,
                                                         "digest": sha256_hex(canonical_json(hashes))})
    event.options = {**(event.options or {}), "sealed_at": now().isoformat()}
    event.save(update_fields=["options"])
    return True


def deadline_countdown(event: Event, at=None) -> timedelta:
    return event.effective_close - (at or now())
