"""Whole-event export/import ("an organizer can leave as easily as they arrived").

`quorum.bundle/v1` is a single JSON document: configuration, the locked method, people,
teams, submissions, assignments, reviews, pairwise comparisons, every ranking run's exact
engine input and output hash, and the audit head. `python -m engine recompute bundle.json`
re-runs every stored ranking with nothing but the Python standard library and prints
MATCH or MISMATCH. Importing a bundle creates a new event and verifies the same way.
"""

from __future__ import annotations

from django.db import connection, transaction
from django.utils.dateparse import parse_datetime

from engine import ENGINE_VERSION
from engine.pipeline import compute
from quorum.accounts.services import user_for_email
from quorum.audit import service as audit
from quorum.audit.models import AuditCheckpoint
from quorum.core.clock import now
from quorum.events.models import (CustomQuestion, Event, EventRole, JudgeTrack, Prize, Project, ProjectAnswer, Team,
                                  TeamMember, Track)
from quorum.judging.models import Assignment, Criterion, JudgingMethod, PairwiseComparison, Review, ReviewScore
from quorum.policy.errors import Invalid

FORMAT = "quorum.bundle/v1"
EVENT_FIELDS = ["ref", "slug", "name", "tagline", "description_md", "rules_md", "registration_opens_at",
                "submissions_open_at", "submissions_close_at", "grace_seconds", "judging_opens_at", "judging_closes_at",
                "voting_opens_at", "voting_closes_at", "phase", "team_size_max", "reviews_per_project", "batch_size",
                "min_feedback_chars", "focus_budget_pct", "prize_positions", "voting_mode", "votes_per_voter",
                "quadratic_voting", "rank_display"]


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def export_bundle(ev: Event, pseudonymize: bool = False) -> dict:
    def who(u):
        if pseudonymize:
            import hashlib

            return "person-" + hashlib.sha256(u.email.encode()).hexdigest()[:12] + "@pseudonymized.invalid"
        return u.email

    seq, head = audit.head(ev)
    return {
        "format": FORMAT, "exported_at": now().isoformat(), "engine": ENGINE_VERSION, "pseudonymized": pseudonymize,
        "event": {f: _iso(getattr(ev, f)) for f in EVENT_FIELDS},
        "methods": [{"version": m.version, "spec": m.spec, "spec_hash": m.spec_hash, "locked_at": _iso(m.locked_at),
                     "override_reason": m.override_reason} for m in ev.methods.order_by("version")],
        "tracks": [{"ref": t.ref, "name": t.name, "description": t.description, "position": t.position,
                    "pairwise": t.pairwise} for t in ev.tracks.all()],
        "criteria": [{"key": c.key, "name": c.name, "description": c.description, "weight_bp": c.weight_bp,
                      "scale_min": c.scale_min, "scale_max": c.scale_max, "anchors": c.anchors, "position": c.position}
                     for c in ev.criteria.all()],
        "prizes": [{"name": p.name, "description": p.description, "places": p.places, "kind": p.kind,
                    "value_text": p.value_text, "track": p.track.ref if p.track else None} for p in ev.prizes.select_related("track")],
        "questions": [{"ref": q.ref, "label": q.label, "help": q.help, "kind": q.kind, "required": q.required,
                       "options": q.options} for q in ev.questions.all()],
        "teams": [{"ref": t.ref, "name": t.name,
                   "members": [{"email": who(m.user), "name": "" if pseudonymize else m.user.name, "role": m.role}
                               for m in t.members.select_related("user")]} for t in ev.teams.all()],
        "projects": [{"ref": p.ref, "team": p.team.ref, "track": p.track.ref if p.track else None, "title": p.title,
                      "tagline": p.tagline, "description_md": p.description_md, "demo_video_url": p.demo_video_url,
                      "repo_url": p.repo_url, "live_url": p.live_url, "declared_commit_sha": p.declared_commit_sha,
                      "tech_tags": p.tech_tags, "status": p.status, "duplicate_of": p.duplicate_of.ref if p.duplicate_of else None,
                      "submitted_at": _iso(p.submitted_at), "content_hash": p.content_hash, "version": p.version,
                      "answers": {a.question.ref: a.value for a in p.answers.select_related("question")}}
                     for p in ev.projects.select_related("team", "track", "duplicate_of").order_by("ref")],
        "judges": [{"ref": r.ref, "email": who(r.user), "name": "" if pseudonymize else r.user.name, "capacity": r.capacity,
                    "tracks": [jt.track.ref for jt in r.judge_tracks.select_related("track")]}
                   for r in ev.roles.filter(role="judge").select_related("user").order_by("ref")],
        "organizers": [who(r.user) for r in ev.roles.filter(role="organizer").select_related("user")],
        "assignments": [{"judge": a.judge_role.ref, "project": a.project.ref, "status": a.status, "source": a.source,
                         "strategy": a.strategy} for a in Assignment.objects.filter(event=ev).select_related("judge_role", "project")],
        "reviews": [{"judge": r.judge_role.ref, "project": r.project.ref, "status": r.status,
                     "scores": {s.criterion.key: float(s.value) for s in r.scores.select_related("criterion")},
                     "feedback_to_team": r.feedback_to_team, "moderation": r.moderation,
                     "submitted_at": _iso(r.submitted_at), "source_scores": r.source_scores}
                    for r in Review.objects.filter(event=ev).select_related("judge_role", "project")],
        "pairwise": [{"judge": c.judge_role.ref, "a": c.project_a.ref, "b": c.project_b.ref, "outcome": c.outcome,
                      "tiebreak": str(c.tiebreak_id) if c.tiebreak_id else None, "reason": c.reason}
                     for c in PairwiseComparison.objects.filter(event=ev).select_related("judge_role", "project_a", "project_b")],
        "ranking_runs": [{"id": str(r.pk), "kind": r.kind, "created_at": _iso(r.created_at), "engine_version": r.engine_version,
                          "method_hash": r.method_hash, "input_hash": r.input_hash, "output_hash": r.output_hash,
                          "heavy": "loo" in r.output, "input": r.input} for r in ev.runs.order_by("created_at")],
        "publication": ({"run": str(ev.publication.run_id), "locked_at": _iso(ev.publication.locked_at),
                         "published_at": _iso(ev.publication.published_at), "final_order": ev.publication.final_order,
                         "audit_head": ev.publication.audit_head, "audit_seq": ev.publication.audit_seq}
                        if hasattr(ev, "publication") else None),
        "audit": {"seq": seq, "head": head,
                  "checkpoints": [{"seq": c.seq, "head": c.head, "reason": c.reason, "payload": c.payload,
                                   "signature": c.signature, "key_id": c.key_id}
                                  for c in AuditCheckpoint.objects.filter(event_id=ev.pk).order_by("seq")]},
    }


@transaction.atomic
def import_bundle(actor, data: dict, new_slug: str | None = None) -> tuple[Event, dict]:
    if data.get("format") != FORMAT:
        raise Invalid(f"Not a {FORMAT} document.")
    with connection.cursor() as cur:
        cur.execute("SET LOCAL quorum.bypass = 'on'")  # historical submissions and scores
    e = dict(data["event"])
    slug = new_slug or f"{e['slug']}-imported"
    n = 2
    while Event.objects.filter(slug=slug).exists():
        slug, n = f"{e['slug']}-imported-{n}", n + 1
    fields = {k: (parse_datetime(v) if k.endswith("_at") and isinstance(v, str) else v) for k, v in e.items()
              if k in EVENT_FIELDS and k not in ("ref", "slug")}
    fields["phase"] = "judging" if fields.get("phase") in ("locked", "published") else fields.get("phase", "draft")
    ev = Event.objects.create(ref=slug[:40], slug=slug, created_by=getattr(actor, "user", None), **fields)
    if getattr(actor, "user", None):
        EventRole.objects.create(event=ev, user=actor.user, role="organizer")
    tracks = {t["ref"]: Track.objects.create(event=ev, **t) for t in data.get("tracks", [])}
    crits = {c["key"]: Criterion.objects.create(event=ev, **c) for c in data.get("criteria", [])}
    for p in data.get("prizes", []):
        Prize.objects.create(event=ev, track=tracks.get(p.pop("track", None)), **p)
    qs = {q["ref"]: CustomQuestion.objects.create(event=ev, **q) for q in data.get("questions", [])}
    for m in data.get("methods", []):
        JudgingMethod.objects.create(event=ev, version=m["version"], spec=m["spec"], spec_hash=m["spec_hash"],
                                     locked_at=parse_datetime(m["locked_at"]) if m.get("locked_at") else None,
                                     override_reason=m.get("override_reason", ""))
    judges = {}
    for j in data.get("judges", []):
        u = user_for_email(j["email"], j.get("name", ""))
        r = EventRole.objects.create(event=ev, user=u, role="judge", ref=j["ref"], capacity=j.get("capacity"))
        for t in j.get("tracks", []):
            if t in tracks:
                JudgeTrack.objects.create(event_role=r, track=tracks[t])
        judges[j["ref"]] = r
    teams = {}
    for t in data.get("teams", []):
        team = Team.objects.create(event=ev, ref=t["ref"], name=t["name"])
        for m in t.get("members", []):
            u = user_for_email(m["email"], m.get("name", ""))
            TeamMember.objects.create(team=team, event=ev, user=u, role=m.get("role", "member"))
            EventRole.objects.get_or_create(event=ev, user=u, role="participant")
        teams[t["ref"]] = team
    projects = {}
    for p in data.get("projects", []):
        pr = Project.objects.create(
            event=ev, ref=p["ref"], team=teams[p["team"]], track=tracks.get(p.get("track")), title=p["title"],
            tagline=p.get("tagline", ""), description_md=p.get("description_md", ""),
            demo_video_url=p.get("demo_video_url", ""), repo_url=p.get("repo_url", ""), live_url=p.get("live_url", ""),
            declared_commit_sha=p.get("declared_commit_sha", ""), tech_tags=p.get("tech_tags") or [],
            status=p.get("status", "submitted"),
            submitted_at=parse_datetime(p["submitted_at"]) if p.get("submitted_at") else None,
            content_hash=p.get("content_hash", ""), version=p.get("version", 1))
        from quorum.core.text import render_markdown

        pr.description_html = render_markdown(pr.description_md)
        pr.save(update_fields=["description_html"])
        for ref, val in (p.get("answers") or {}).items():
            if ref in qs:
                ProjectAnswer.objects.create(project=pr, question=qs[ref], value=val)
        projects[p["ref"]] = pr
    for p in data.get("projects", []):
        if p.get("duplicate_of"):
            Project.objects.filter(pk=projects[p["ref"]].pk).update(duplicate_of=projects[p["duplicate_of"]])
    amap = {}
    for a in data.get("assignments", []):
        if a["judge"] in judges and a["project"] in projects:
            amap[(a["judge"], a["project"])] = Assignment.objects.create(
                event=ev, judge_role=judges[a["judge"]], project=projects[a["project"]], status=a["status"],
                source="import", strategy=a.get("strategy", ""))
    for r in data.get("reviews", []):
        a = amap.get((r["judge"], r["project"]))
        if not a:
            continue
        rv = Review.objects.create(assignment=a, event=ev, judge_role=a.judge_role, project=a.project, status=r["status"],
                                   feedback_to_team=r.get("feedback_to_team", ""), moderation=r.get("moderation", "pending"),
                                   submitted_at=parse_datetime(r["submitted_at"]) if r.get("submitted_at") else None,
                                   source_scores=r.get("source_scores") or [])
        for k, v in (r.get("scores") or {}).items():
            if k in crits:
                ReviewScore.objects.create(review=rv, criterion=crits[k], value=v)
    for c in data.get("pairwise", []):
        # comparative choices come across; tie-break rounds belong to the source event's panels
        if c.get("tiebreak") or c["judge"] not in judges or c["a"] not in projects or c["b"] not in projects:
            continue
        PairwiseComparison.objects.create(event=ev, judge_role=judges[c["judge"]], project_a=projects[c["a"]],
                                          project_b=projects[c["b"]], outcome=c["outcome"], reason=c.get("reason", ""))
    audit.record("BUNDLE_IMPORTED", f"Event imported from a {FORMAT} bundle ({len(projects)} projects, "
                                    f"{len(data.get('reviews', []))} reviews)", event=ev, actor=actor,
                 actor_role="organizer", data={"source_head": (data.get("audit") or {}).get("head")})
    report = verify_bundle(data)
    return ev, report


def verify_bundle(data: dict) -> dict:
    """Recompute every ranking run stored in the bundle from its exact input."""
    results = []
    for run in data.get("ranking_runs", []):
        res = compute(run["input"], heavy=run.get("heavy", True))
        results.append({"id": run["id"], "kind": run["kind"], "match": res["output_hash"] == run["output_hash"],
                        "expected": run["output_hash"], "got": res["output_hash"]})
    return {"runs": results, "all_match": all(r["match"] for r in results) if results else None}
