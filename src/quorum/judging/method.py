"""Method pre-registration.

Hackathon Raptors' own policy is that criteria and weights are published before
registration opens and never change. Quorum makes that a mechanism: the full method is
serialised to canonical JSON, hashed, and locked; the hash is shown on the public event
page. A later change creates a new version, requires an admin with a written reason, and
leaves a permanent banner on the results page."""

from __future__ import annotations

from django.db import connection, transaction

from engine import ENGINE_VERSION
from engine.bundle import canonical_json, sha256_hex
from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.policy.errors import Conflict

from .models import JudgingMethod


def build_spec(event) -> dict:
    return {
        "schema": "quorum.method/v1",
        "engine": ENGINE_VERSION,
        "criteria": [
            {"key": c.key, "name": c.name, "weight_bp": c.weight_bp, "scale": [c.scale_min, c.scale_max]}
            for c in event.criteria.order_by("position", "key")
        ],
        "calibration": (event.options or {}).get("calibration", "offset-reml/v1"),
        "flat_rule": {"enabled": True, "min_reviews": 3},
        "reviews_per_project": event.reviews_per_project,
        "batch_size": event.batch_size,
        "prize_positions": event.prize_positions,
        "focus": {"budget_pct": event.focus_budget_pct, "rounds": 2, "objective": "P(1-P)*v^2/(v+sigma^2)"},
        "tiebreak": {
            "trigger": "adjacent statistical tie (|diff| < 1.96 SE) touching a prize boundary",
            "max_projects": 6, "judges": 3, "resolve_threshold": 0.80,
            "fallback": "recorded organizer decision with reason, or shared prize",
        },
        "feedback": {"min_chars": event.min_feedback_chars, "moderation": not event.auto_approve_feedback},
        "voting": {"mode": event.voting_mode, "votes_per_voter": event.votes_per_voter,
                   "quadratic": event.quadratic_voting, "separate_from_judged_prizes": True},
    }


def current(event) -> JudgingMethod | None:
    return event.methods.order_by("-version").first()


def is_locked(event) -> bool:
    m = current(event)
    return bool(m and m.locked_at)


@transaction.atomic
def lock(event, actor=None, at=None) -> JudgingMethod:
    m = current(event)
    if m and m.locked_at:
        raise Conflict("The judging method is already locked.", code="method_locked")
    if not event.criteria.exists():
        raise Conflict("Add at least one rubric criterion before locking the method.")
    spec = build_spec(event)
    h = sha256_hex(spec)
    m = JudgingMethod.objects.create(event=event, version=(m.version + 1 if m else 1), spec=spec, spec_hash=h,
                                     locked_at=at or now(), locked_by=getattr(actor, "user", actor))
    audit.record("METHOD_LOCKED", f"Judging method v{m.version} locked (sha256 {h[:12]}…)", event=event,
                 actor=actor, actor_role="organizer", target=m, data={"spec_hash": h, "version": m.version}, at=at)
    return m


@transaction.atomic
def override(event, actor, reason: str) -> JudgingMethod:
    """Admin-only escape hatch: records a new locked version built from the current rubric."""
    if not reason or len(reason.strip()) < 10:
        raise Conflict("An override needs a written reason (10+ characters).")
    prev = current(event)
    with connection.cursor() as cur:
        cur.execute("SET LOCAL quorum.bypass = 'on'")
    spec = build_spec(event)
    h = sha256_hex(spec)
    m = JudgingMethod.objects.create(event=event, version=(prev.version + 1 if prev else 1), spec=spec,
                                     spec_hash=h, locked_at=now(), locked_by=getattr(actor, "user", actor),
                                     override_reason=reason.strip())
    audit.record("METHOD_OVERRIDDEN", f"Method overridden to v{m.version}: {reason.strip()[:200]}", event=event,
                 actor=actor, actor_role="admin", target=m,
                 data={"previous_hash": prev.spec_hash if prev else None, "spec_hash": h, "reason": reason.strip()})
    return m


def spec_json(m: JudgingMethod) -> str:
    return canonical_json(m.spec)
