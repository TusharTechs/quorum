"""CSV exports at every stage of the pipeline. Organizer-only (checked by the caller),
audited, and injection-safe (quorum.core.csvsafe)."""

from __future__ import annotations

from collections import defaultdict

from quorum.audit.models import AuditEvent
from quorum.core.csvsafe import to_csv
from quorum.events.models import Event, EventRole, Project, TeamMember
from quorum.judging.models import Assignment, PairwiseComparison, Review, ReviewScore
from quorum.results.service import latest_run
from quorum.voting.models import Vote


def projects_csv(ev: Event) -> str:
    rows = []
    for p in Project.objects.filter(event=ev).select_related("team", "track", "duplicate_of").order_by("ref"):
        rows.append([p.ref, p.title, p.tagline, p.team.ref, p.team.name, p.track.ref if p.track else "",
                     p.status, p.duplicate_of.ref if p.duplicate_of else "", p.repo_url, p.demo_video_url,
                     p.live_url, ";".join(p.tech_tags or []), p.submitted_at.isoformat() if p.submitted_at else "",
                     p.version, p.content_hash])
    return to_csv(["project", "title", "tagline", "team", "team_name", "track", "status", "duplicate_of",
                   "repo_url", "demo_video_url", "live_url", "tech_tags", "submitted_at", "version",
                   "content_hash"], rows)


def teams_csv(ev: Event) -> str:
    rows = []
    for m in TeamMember.objects.filter(event=ev).select_related("team", "user").order_by("team__ref", "joined_at"):
        rows.append([m.team.ref, m.team.name, m.user.email, m.user.name, m.role, m.joined_at.isoformat()])
    return to_csv(["team", "team_name", "email", "name", "role", "joined_at"], rows)


def judges_csv(ev: Event) -> str:
    rows = []
    roles = EventRole.objects.filter(event=ev, role="judge").select_related("user").prefetch_related(
        "judge_tracks__track").order_by("ref")
    counts = defaultdict(lambda: defaultdict(int))
    for a in Assignment.objects.filter(event=ev).exclude(status="reassigned").values("judge_role_id", "status"):
        counts[a["judge_role_id"]][a["status"]] += 1
    for r in roles:
        c = counts[r.pk]
        total = sum(c.values())
        rows.append([r.ref, r.user.name, r.user.email, ";".join(t.track.ref for t in r.judge_tracks.all()),
                     r.capacity or "", total, c.get("submitted", 0), total - c.get("submitted", 0) - c.get("recused", 0),
                     c.get("recused", 0), r.available,
                     r.last_activity_at.isoformat() if r.last_activity_at else ""])
    return to_csv(["judge", "name", "email", "tracks", "capacity", "assigned", "submitted", "pending", "recused",
                   "available", "last_activity_at"], rows)


def assignments_csv(ev: Event) -> str:
    rows = []
    for a in Assignment.objects.filter(event=ev).select_related("judge_role", "project", "batch", "reassigned_from",
                                                                 "reassigned_from__judge_role").order_by("assigned_at"):
        rows.append([str(a.pk), a.judge_role.ref, a.project.ref, a.status, a.source, a.strategy, a.seed or "",
                     a.batch.kind if a.batch else "", a.assigned_at.isoformat(),
                     a.due_at.isoformat() if a.due_at else "",
                     a.reassigned_from.judge_role.ref if a.reassigned_from else ""])
    return to_csv(["assignment", "judge", "project", "status", "source", "strategy", "seed", "batch_kind",
                   "assigned_at", "due_at", "reassigned_from_judge"], rows)


def reviews_csv(ev: Event) -> str:
    crits = list(ev.criteria.order_by("position"))
    scores = defaultdict(dict)
    for s in ReviewScore.objects.filter(review__event=ev).select_related("criterion"):
        scores[s.review_id][s.criterion.key] = float(s.value)
    rows = []
    for r in Review.objects.filter(event=ev).select_related("judge_role", "project").order_by("project__ref", "judge_role__ref"):
        rows.append([str(r.pk), r.project.ref, r.judge_role.ref, r.status]
                    + [scores[r.pk].get(c.key, "") for c in crits]
                    + [r.feedback_to_team, r.moderation, r.submitted_at.isoformat() if r.submitted_at else "",
                       r.active_seconds])
    return to_csv(["review", "project", "judge", "status"] + [c.key for c in crits]
                  + ["feedback_to_team", "moderation", "submitted_at", "active_seconds"], rows)


def scores_csv(ev: Event) -> str:
    """Long format: one row per (review, criterion). The input any statistician wants."""
    rows = []
    for s in (ReviewScore.objects.filter(review__event=ev, review__status="submitted")
              .select_related("review__judge_role", "review__project", "criterion")
              .order_by("review__project__ref", "review__judge_role__ref", "criterion__position")):
        rows.append([s.review.project.ref, s.review.judge_role.ref, s.criterion.key, float(s.value),
                     s.criterion.weight_bp])
    return to_csv(["project", "judge", "criterion", "value", "weight_bp"], rows)


def results_csv(ev: Event) -> str:
    run = latest_run(ev, "official") or latest_run(ev)
    header = ["rank", "project", "title", "team", "track", "n_reviews", "n_effective", "raw_score",
              "calibrated_score", "se", "rank_interval_90", "p_prize", "p_track_first", "tie_group",
              "tied_with_next", "raw_rank", "raptors_classic_score", "zscore_rank", "run_output_hash"]
    if not run or not run.output.get("entries"):
        return to_csv(header, [])
    info = {p.ref: p for p in Project.objects.filter(event=ev).select_related("team", "track")}
    rows = []
    for e in run.output["entries"]:
        p = info.get(e["project"])
        rows.append([e["rank"], e["project"], p.title if p else "", p.team.name if p else "",
                     p.track.name if p and p.track else "", e["n_reviews"], _r(e.get("n_effective")),
                     _r(e.get("raw")), _r(e.get("calibrated")), _r(e.get("se")),
                     f"{e.get('rank_lo')}-{e.get('rank_hi')}", _r(e.get("p_prize")), _r(e.get("p_track_first")),
                     e.get("tie_group"), e.get("tied_with_next"), e.get("raw_rank"), _r(e.get("classic")),
                     e.get("zscore_rank"), run.output_hash])
    return to_csv(header, rows)


def votes_csv(ev: Event) -> str:
    rows = []
    for v in Vote.objects.filter(event=ev).select_related("project", "voter").order_by("created_at"):
        rows.append([str(v.pk), v.project.ref, v.category, v.voter.kind, v.voter.voter_key[:12], v.weight,
                     v.ballot_position or "", v.status, v.void_reason, v.created_at.isoformat()])
    return to_csv(["vote", "project", "category", "voter_kind", "voter_key_prefix", "weight", "ballot_position",
                   "status", "void_reason", "created_at"], rows)


def feedback_csv(ev: Event) -> str:
    rows = []
    for r in Review.objects.filter(event=ev, status="submitted").select_related("project", "judge_role").order_by("project__ref"):
        rows.append([r.project.ref, r.judge_role.ref, r.moderation, r.feedback_for_team, r.quotable])
    return to_csv(["project", "judge", "moderation", "feedback_as_released", "quotable"], rows)


def comparisons_csv(ev: Event) -> str:
    """Every pairwise choice: tie-break rounds and comparative judging per track."""
    rows = []
    for c in (PairwiseComparison.objects.filter(event=ev)
              .select_related("judge_role", "project_a__track", "project_b").order_by("created_at", "id")):
        winner = {"a": c.project_a.ref, "b": c.project_b.ref, "tie": "tie"}[c.outcome]
        rows.append([str(c.pk), "tiebreak" if c.tiebreak_id else "comparative", str(c.tiebreak_id or ""),
                     c.project_a.track.ref if c.project_a.track else "", c.judge_role.ref, c.project_a.ref,
                     c.project_b.ref, winner, c.reason, c.active_seconds, c.created_at.isoformat()])
    return to_csv(["comparison", "context", "tiebreak", "track", "judge", "project_a", "project_b", "winner",
                   "reason", "active_seconds", "created_at"], rows)


def audit_csv(ev: Event) -> str:
    rows = [[a.seq, a.ts.isoformat(), a.actor_label, a.actor_role, a.action, a.target_type, a.target_id,
             a.summary, a.prev_hash, a.hash]
            for a in AuditEvent.objects.filter(event_id=ev.pk).order_by("seq")]
    return to_csv(["seq", "ts", "actor", "actor_role", "action", "target_type", "target_id", "summary",
                   "prev_hash", "hash"], rows)


def _r(x, nd=4):
    return round(x, nd) if isinstance(x, (int, float)) else ("" if x is None else x)


CSV_EXPORTS = {
    "projects": projects_csv, "teams": teams_csv, "judges": judges_csv, "assignments": assignments_csv,
    "reviews": reviews_csv, "scores": scores_csv, "results": results_csv, "votes": votes_csv,
    "feedback": feedback_csv, "comparisons": comparisons_csv, "audit": audit_csv,
}
