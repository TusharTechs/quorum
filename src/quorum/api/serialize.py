"""Per-audience serializers: each lists its fields explicitly. There is no
'serialize everything' path, so a new column can never leak by default."""

from __future__ import annotations

from quorum.events.models import Project


def thumb_url(p: Project):
    return f"/media/{p.thumbnail}" if p.thumbnail else None


def project_public(p: Project) -> dict:
    return {
        "id": str(p.pk), "ref": p.ref, "event": p.event.ref, "title": p.title, "tagline": p.tagline,
        "track": p.track.name if p.track_id else None, "team": p.team.name,
        "description_html": p.description_html, "repo_url": p.repo_url, "demo_video_url": p.demo_video_url,
        "live_url": p.live_url, "tech_tags": p.tech_tags or [], "thumbnail_url": thumb_url(p),
        "submitted_at": p.submitted_at,
    }


def project_team(p: Project) -> dict:
    d = project_public(p)
    d.update({
        "status": p.status, "description_md": p.description_md, "declared_commit_sha": p.declared_commit_sha,
        "version": p.version, "content_hash": p.content_hash,
        "answers": {a.question.ref: a.value for a in p.answers.select_related("question")},
    })
    return d


def review_out(r, weights: dict | None = None) -> dict:
    crit = {s.criterion.key: float(s.value) for s in r.scores.all()}
    total = None
    if weights and crit:
        present = [k for k in crit if k in weights]
        w = sum(weights[k] for k in present)
        total = sum(weights[k] * crit[k] for k in present) / w if w else None
    return {
        "review_id": str(r.pk), "event": r.event.ref, "project": r.project.ref, "project_title": r.project.title,
        "judge": r.judge_role.ref or str(r.judge_role_id), "status": r.status, "criteria": crit,
        "weighted_total": round(total, 4) if total is not None else None,
        "feedback_to_team": r.feedback_to_team, "submitted_at": r.submitted_at,
    }
