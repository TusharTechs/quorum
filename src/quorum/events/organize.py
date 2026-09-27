"""Organizer operations on events: create (blank / Raptors template / clone), edit with
audited date changes, rubric editing before the method lock, tracks, prizes, questions,
judge invitations."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from quorum.accounts.services import issue_magic_link, user_for_email
from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.core.text import clean_line
from quorum.judging import method as methods
from quorum.judging.models import Criterion
from quorum.policy.errors import Conflict, Forbidden, Invalid

from .models import CustomQuestion, Event, EventRole, JudgeTrack, Prize, Track

TEMPLATES = {
    "raptors": {
        "label": "Hackathon Raptors style (Code Olympics weights)",
        "criteria": [("functionality", "Functionality & reliability", 4000), ("constraints", "Constraint mastery", 3000),
                     ("code_quality", "Code quality", 2000), ("innovation", "Innovation", 1000)],
        "reviews_per_project": 3, "batch_size": 12, "min_feedback_chars": 80,
    },
    "classic": {
        "label": "Classic hackathon (equal weights)",
        "criteria": [("functionality", "Functionality", 2500), ("design", "Design & UX", 2500),
                     ("impact", "Impact", 2500), ("innovation", "Innovation", 2500)],
        "reviews_per_project": 3, "batch_size": 10, "min_feedback_chars": 60,
    },
}
ANCHORS = {"1": "Missing or broken", "2": "Weak", "3": "Solid", "4": "Strong", "5": "Exceptional"}


def _dt(v, field):
    d = parse_datetime(v) if isinstance(v, str) else v
    if d is None:
        raise Invalid(f"{field}: enter a date and time.", extra={"fields": {field: "invalid"}})
    if d.tzinfo is None:
        from django.utils import timezone

        d = timezone.make_aware(d, timezone.utc)
    return d


def _unique_slug(name):
    base = slugify(name)[:60] or "event"
    slug, n = base, 2
    while Event.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


@transaction.atomic
def create_event(actor, data: dict, template: str = "raptors") -> Event:
    actor.require_auth()
    if not (actor.is_admin or actor.user.event_roles.filter(role="organizer").exists() or actor.user.is_staff):
        raise Forbidden("Only organizers and administrators can create events.")
    name = clean_line(data.get("name", ""), 140)
    if len(name) < 3:
        raise Invalid("Give the event a name.", extra={"fields": {"name": "required"}})
    t0 = now().replace(minute=0, second=0, microsecond=0)
    opens = _dt(data.get("submissions_open_at") or t0 + timedelta(days=7), "submissions_open_at")
    closes = _dt(data.get("submissions_close_at") or opens + timedelta(hours=72), "submissions_close_at")
    if closes <= opens:
        raise Invalid("Submissions must close after they open.", extra={"fields": {"submissions_close_at": "before open"}})
    tpl = TEMPLATES.get(template, TEMPLATES["raptors"])
    slug = _unique_slug(data.get("slug") or name)
    ev = Event.objects.create(
        ref=slug[:40], slug=slug, name=name, tagline=clean_line(data.get("tagline", ""), 200),
        description_md=data.get("description_md", "")[:20000],
        registration_opens_at=t0, submissions_open_at=opens, submissions_close_at=closes,
        judging_opens_at=closes + timedelta(days=1), judging_closes_at=closes + timedelta(days=11),
        voting_mode=data.get("voting_mode") or "off", phase="draft",
        reviews_per_project=tpl["reviews_per_project"], batch_size=tpl["batch_size"],
        min_feedback_chars=tpl["min_feedback_chars"], created_by=actor.user)
    EventRole.objects.create(event=ev, user=actor.user, role="organizer")
    for i, (key, cname, w) in enumerate(tpl["criteria"]):
        Criterion.objects.create(event=ev, key=key, name=cname, weight_bp=w, position=i, anchors=ANCHORS)
    audit.record("EVENT_CREATED", f"Event {ev.name} created from the '{template}' template", event=ev, actor=actor,
                 actor_role="organizer", target=ev)
    return ev


@transaction.atomic
def clone_event(actor, source: Event, name: str, shift_days: int) -> Event:
    actor.require_organizer(source)
    shift = timedelta(days=int(shift_days))
    name = clean_line(name, 140) or f"{source.name} (copy)"
    slug = _unique_slug(name)
    ev = Event.objects.create(
        ref=slug[:40], slug=slug, name=name, tagline=source.tagline, description_md=source.description_md,
        rules_md=source.rules_md, registration_opens_at=source.registration_opens_at + shift,
        submissions_open_at=source.submissions_open_at + shift, submissions_close_at=source.submissions_close_at + shift,
        grace_seconds=source.grace_seconds, judging_opens_at=source.judging_opens_at + shift,
        judging_closes_at=source.judging_closes_at + shift,
        voting_opens_at=source.voting_opens_at + shift if source.voting_opens_at else None,
        voting_closes_at=source.voting_closes_at + shift if source.voting_closes_at else None,
        team_size_max=source.team_size_max, reviews_per_project=source.reviews_per_project,
        batch_size=source.batch_size, min_feedback_chars=source.min_feedback_chars,
        focus_budget_pct=source.focus_budget_pct, prize_positions=source.prize_positions,
        voting_mode=source.voting_mode, votes_per_voter=source.votes_per_voter, phase="draft", created_by=actor.user)
    EventRole.objects.create(event=ev, user=actor.user, role="organizer")
    for t in source.tracks.all():
        Track.objects.create(event=ev, ref=t.ref, name=t.name, description=t.description, position=t.position)
    tracks = {t.ref: t for t in ev.tracks.all()}
    for p in source.prizes.select_related("track"):
        Prize.objects.create(event=ev, track=tracks.get(p.track.ref) if p.track else None, name=p.name,
                             description=p.description, places=p.places, kind=p.kind, value_text=p.value_text,
                             position=p.position)
    for q in source.questions.all():
        CustomQuestion.objects.create(event=ev, ref=q.ref, label=q.label, help=q.help, kind=q.kind,
                                      required=q.required, options=q.options, position=q.position)
    for c in source.criteria.all():
        Criterion.objects.create(event=ev, key=c.key, name=c.name, description=c.description, weight_bp=c.weight_bp,
                                 scale_min=c.scale_min, scale_max=c.scale_max, anchors=c.anchors, position=c.position)
    audit.record("EVENT_CREATED", f"Event {ev.name} cloned from {source.name} (+{shift_days} days)", event=ev,
                 actor=actor, actor_role="organizer", target=ev, data={"source": source.ref})
    return ev


DATE_FIELDS = ("registration_opens_at", "submissions_open_at", "submissions_close_at", "judging_opens_at",
               "judging_closes_at", "voting_opens_at", "voting_closes_at")
INT_FIELDS = ("grace_seconds", "team_size_max", "reviews_per_project", "batch_size", "min_feedback_chars",
              "focus_budget_pct", "prize_positions", "votes_per_voter")


@transaction.atomic
def update_event(actor, ev: Event, data: dict) -> Event:
    actor.require_organizer(ev)
    changes = {}
    for f in ("name", "tagline"):
        if f in data:
            v = clean_line(data[f], 200)
            if v != getattr(ev, f):
                changes[f] = (getattr(ev, f), v)
                setattr(ev, f, v)
    for f in ("description_md", "rules_md"):
        if f in data and data[f] != getattr(ev, f):
            changes[f] = ("…", "…")
            setattr(ev, f, data[f][:20000])
    for f in DATE_FIELDS:
        if f in data and data[f] not in (None, ""):
            v = _dt(data[f], f)
            if v != getattr(ev, f):
                changes[f] = (getattr(ev, f).isoformat() if getattr(ev, f) else None, v.isoformat())
                setattr(ev, f, v)
    for f in INT_FIELDS:
        if f in data and str(data[f]).strip() != "":
            v = int(data[f])
            if v != getattr(ev, f):
                changes[f] = (getattr(ev, f), v)
                setattr(ev, f, v)
    for f in ("voting_mode", "rank_display"):
        if f in data and data[f] and data[f] != getattr(ev, f):
            changes[f] = (getattr(ev, f), data[f])
            setattr(ev, f, data[f])
    for f in ("quadratic_voting", "auto_approve_feedback", "is_listed"):
        if f in data:
            v = str(data[f]).lower() in ("1", "true", "on", "yes")
            if v != getattr(ev, f):
                changes[f] = (getattr(ev, f), v)
                setattr(ev, f, v)
    if ev.submissions_close_at <= ev.submissions_open_at or ev.judging_closes_at <= ev.judging_opens_at:
        raise Invalid("Each window must close after it opens.")
    ev.full_clean(exclude=["created_by"])
    ev.save()
    if changes:
        extension = "submissions_close_at" in changes or "grace_seconds" in changes
        audit.record("DEADLINE_CHANGED" if extension else "EVENT_UPDATED",
                     ("Deadline changed: " if extension else "Event settings changed: ") + ", ".join(changes),
                     event=ev, actor=actor, actor_role="organizer", target=ev,
                     data={k: [str(a), str(b)] for k, (a, b) in changes.items()})
    return ev


def _guard_rubric_editable(ev):
    if methods.is_locked(ev):
        raise Conflict("The judging method is locked (published to participants). Changing the rubric now "
                       "needs an administrator override with a written reason.", code="method_locked")


@transaction.atomic
def save_criteria(actor, ev: Event, rows: list[dict], override_reason: str | None = None):
    """rows: [{"key","name","description","weight_pct","scale_max"}]; weights must sum to 100%.
    After the method is locked, only an administrator may change it, with a written reason;
    the change becomes a new locked method version and is shown on the results page forever."""
    actor.require_organizer(ev)
    if override_reason:
        actor.require_admin()
        if len(override_reason.strip()) < 10:
            raise Invalid("An override needs a written reason (10+ characters).")
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute("SET LOCAL quorum.bypass = 'on'")
    else:
        _guard_rubric_editable(ev)
    rows = [r for r in rows if (r.get("name") or "").strip()]
    if not rows:
        raise Invalid("Add at least one criterion.")
    total = 0
    clean = []
    for i, r in enumerate(rows):
        bp = int(round(float(r.get("weight_pct") or 0) * 100))
        if bp <= 0:
            raise Invalid(f"{r['name']}: weight must be positive.")
        total += bp
        clean.append({"key": slugify(r.get("key") or r["name"])[:40] or f"c{i}", "name": clean_line(r["name"], 80),
                      "description": (r.get("description") or "")[:500], "weight_bp": bp,
                      "scale_max": int(r.get("scale_max") or 5), "position": i,
                      "anchors": r.get("anchors") or ANCHORS})
    if total != 10000:
        raise Invalid(f"Weights must add up to 100% (now {total / 100:g}%).")
    keys = [c["key"] for c in clean]
    if len(set(keys)) != len(keys):
        raise Invalid("Two criteria have the same key.")
    Criterion.objects.filter(event=ev).exclude(key__in=keys).delete()
    for c in clean:
        Criterion.objects.update_or_create(event=ev, key=c["key"], defaults=c)
    audit.record("RUBRIC_UPDATED", "Rubric saved: " + ", ".join(f"{c['name']} {c['weight_bp'] / 100:g}%" for c in clean),
                 event=ev, actor=actor, actor_role="admin" if override_reason else "organizer")
    if override_reason:
        methods.override(ev, actor, override_reason)


@transaction.atomic
def add_track(actor, ev, name):
    actor.require_organizer(ev)
    name = clean_line(name, 100)
    if not name:
        raise Invalid("Track name required.")
    n = ev.tracks.count() + 1
    t = Track.objects.create(event=ev, ref=f"trk_{n:02d}" if not ev.tracks.filter(ref=f"trk_{n:02d}").exists()
                             else slugify(name)[:40], name=name, position=n)
    audit.record("TRACK_ADDED", f"Track {name} added", event=ev, actor=actor, actor_role="organizer", target=t)
    return t


@transaction.atomic
def add_prize(actor, ev, name, places=1, kind="judged", value_text="", description=""):
    actor.require_organizer(ev)
    p = Prize.objects.create(event=ev, name=clean_line(name, 120), places=max(1, int(places or 1)), kind=kind,
                             value_text=clean_line(value_text, 80), description=description[:500],
                             position=ev.prizes.count())
    audit.record("PRIZE_ADDED", f"Prize {p.name} added", event=ev, actor=actor, actor_role="organizer")
    return p


@transaction.atomic
def add_question(actor, ev, label, kind="text", required=False, options=""):
    actor.require_organizer(ev)
    label = clean_line(label, 200)
    if not label:
        raise Invalid("Question label required.")
    opts = [o.strip() for o in (options or "").split(",") if o.strip()] if kind == "choice" else []
    q = CustomQuestion.objects.create(event=ev, ref=slugify(label)[:40] or f"q{ev.questions.count() + 1}", label=label,
                                      kind=kind, required=bool(required), options=opts, position=ev.questions.count())
    audit.record("QUESTION_ADDED", f"Custom question added: {label}", event=ev, actor=actor, actor_role="organizer")
    return q


@transaction.atomic
def open_registration(actor, ev: Event):
    """Publishing the event locks (pre-registers) the judging method."""
    actor.require_organizer(ev)
    if not methods.is_locked(ev):
        methods.lock(ev, actor)
    if ev.phase == "draft":
        ev.phase = "registration"
        ev.save(update_fields=["phase"])
    audit.record("PHASE_CHANGED", "Event published; registration open; method locked", event=ev, actor=actor,
                 actor_role="organizer", target=ev)
    audit.checkpoint(ev, "registration opened")


@transaction.atomic
def set_phase(actor, ev: Event, phase: str):
    actor.require_organizer(ev)
    allowed = {"judging", "focus", "deliberation", "archived"}
    if phase not in allowed:
        raise Invalid("That phase change is not allowed here.")
    old = ev.phase
    ev.phase = phase
    ev.save(update_fields=["phase"])
    audit.record("PHASE_CHANGED", f"Phase {old} → {phase}", event=ev, actor=actor, actor_role="organizer", target=ev)
    audit.checkpoint(ev, f"phase {phase}")


@transaction.atomic
def invite_judge(actor, ev: Event, email: str, name: str = "", track_refs=(), capacity=None) -> EventRole:
    actor.require_organizer(ev)
    email = (email or "").strip().lower()
    if "@" not in email:
        raise Invalid("Enter the judge's e-mail.")
    u = user_for_email(email, name)
    if u.team_memberships.filter(event=ev).exists():
        raise Conflict("This person is in a team in this event and cannot judge it.")
    n = EventRole.objects.filter(event=ev, role="judge").count() + 1
    ref = f"jdg_{n:02d}"
    while EventRole.objects.filter(event=ev, ref=ref).exists():
        n += 1
        ref = f"jdg_{n:02d}"
    role, created = EventRole.objects.get_or_create(event=ev, user=u, role="judge",
                                                    defaults={"ref": ref, "capacity": capacity or None})
    for t in Track.objects.filter(event=ev, ref__in=list(track_refs)):
        JudgeTrack.objects.get_or_create(event_role=role, track=t)
    issue_magic_link(email, "judge_invite", next_url=f"/j/{ev.slug}", ttl_minutes=60 * 24 * 7,
                     subject=f"You're invited to judge {ev.name}",
                     intro=(f"Hello {name or email},\n\nYou have been invited to judge {ev.name}. Judging runs "
                            f"{ev.judging_opens_at:%d %b} to {ev.judging_closes_at:%d %b %Y} (UTC), fully online and "
                            f"asynchronous; expect about 1-2 hours. Your scores are visible only to you and the "
                            f"organizers.\n\nThis link signs you in (valid 7 days):"))
    audit.record("JUDGE_INVITED", f"{email} invited as judge", event=ev, actor=actor, actor_role="organizer",
                 target=role, data={"tracks": list(track_refs)})
    return role
