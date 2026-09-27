"""Build the engine's canonical input from the database. The exact dict is stored with
every ranking run, so a run can be recomputed byte-for-byte from an export."""

from __future__ import annotations

from collections import defaultdict

from quorum.events.models import Project
from quorum.judging.models import Review, ReviewScore


def judge_key(role) -> str:
    return role.ref or str(role.pk)


def eligible_projects(event):
    return (Project.objects.filter(event=event, status=Project.Status.SUBMITTED, duplicate_of__isnull=True)
            .select_related("track", "team").order_by("ref"))


def build_input(event, method=None, overrides: dict | None = None) -> dict:
    criteria = list(event.criteria.order_by("position", "key"))
    projects = list(eligible_projects(event))
    pids = {p.pk for p in projects}
    reviews = (Review.objects.filter(event=event, status=Review.Status.SUBMITTED, project_id__in=pids)
               .select_related("judge_role", "project"))
    scores = defaultdict(dict)
    for s in ReviewScore.objects.filter(review__in=reviews).select_related("criterion"):
        scores[s.review_id][s.criterion.key] = float(s.value)
    method_fields = {"calibration": (method.spec.get("calibration") if method else "offset-reml/v1"),
                     "prize_n": event.prize_positions}
    if method:
        method_fields["flat_rule"] = method.spec.get("flat_rule", {}).get("enabled", True)
        method_fields["flat_min_reviews"] = method.spec.get("flat_rule", {}).get("min_reviews", 3)
    method_fields.update(overrides or {})
    return {
        "event": event.ref,
        "method_hash": method.spec_hash if method else None,
        "method": method_fields,
        "criteria": [{"key": c.key, "weight": c.weight_bp} for c in criteria],
        "projects": [{"id": p.ref, "track": p.track.ref if p.track else None} for p in projects],
        "reviews": sorted(
            ({"judge": judge_key(r.judge_role), "project": r.project.ref, "criteria": scores.get(r.pk, {})}
             for r in reviews if scores.get(r.pk)),
            key=lambda d: (d["project"], d["judge"]),
        ),
    }
