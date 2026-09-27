from typing import Any

from ninja import Router, Schema

from quorum.judging import ops
from quorum.policy.decorators import policy

from .common import get_event

router = Router(tags=["judging ops"])


class PlanIn(Schema):
    kind: str = "baseline"
    k: int | None = None
    seed: int = 0


class RebalanceIn(Schema):
    judges: list[str] = []


class CommitIn(Schema):
    plan: dict[str, Any]


class NudgeIn(Schema):
    judges: list[str] = []
    message: str = ""


def _org(request, e):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return ev


@router.get("/events/{e}/ops", summary="Judging health: progress, coverage, burn-down, forecast, feedback coverage")
@policy("authenticated")
def status(request, e: str):
    ev = _org(request, e)
    prog = ops.judge_progress(ev)
    cov = ops.coverage(ev)
    fc = ops.forecast(ev, prog)
    fcov = ops.feedback_coverage(ev)
    bd = ops.burndown(ev)
    return {
        "judges": [{"ref": p["ref"], "status": p["status"], "assigned": p["assigned"], "submitted": p["submitted"],
                    "pending": p["pending"], "rate_per_day": round(p["rate_per_day"], 3),
                    "last_activity": p["last_activity"]} for p in prog],
        "coverage": {"target": cov["target"], "ok": cov["ok"], "pending": cov["pending"], "below": cov["below"],
                     "short": [r["project"].ref for r in cov["rows"] if r["state"] == "below"]},
        "at_risk": [{"project": r["project"].ref, "short_by": r["short_by"]} for r in fc["at_risk"]],
        "feedback": {"with_feedback": fcov["with_feedback"], "total": fcov["total"], "none": [p.ref for p in fcov["none"]]},
        "burndown": {"total": bd["total"], "remaining": bd["remaining"], "points": bd["points"]},
    }


@router.post("/events/{e}/assignments/plan", summary="Dry-run an assignment plan (nothing is written)")
@policy("authenticated")
def plan(request, e: str, payload: PlanIn):
    return ops.plan_assignments(_org(request, e), k=payload.k, seed=payload.seed, kind=payload.kind)


@router.post("/events/{e}/rebalance/plan", summary="Dry-run moving stalled judges' pending work")
@policy("authenticated")
def plan_rebalance(request, e: str, payload: RebalanceIn):
    ev = _org(request, e)
    from quorum.events.models import EventRole

    ids = [str(r.pk) for r in EventRole.objects.filter(event=ev, role="judge", ref__in=payload.judges)] or \
        list(ops.inactive_role_ids(ev))
    return ops.plan_rebalance(ev, ids)


@router.post("/events/{e}/assignments/commit", summary="Commit a plan (every pair is re-validated)")
@policy("authenticated")
def commit(request, e: str, payload: CommitIn):
    ev = _org(request, e)
    p = payload.plan
    n = ops.commit_plan(ev, request.actor, p, source="rebalance" if p.get("drop_pending_of") else "algorithm",
                        batch_kind=p.get("kind", "baseline"))
    return {"created": n, "skipped": p.get("skipped", [])}


@router.post("/events/{e}/batches/send", summary="E-mail every judge their unsent batch")
@policy("authenticated")
def send_batches(request, e: str):
    return {"sent": ops.send_batches(_org(request, e), request.actor)}


@router.post("/events/{e}/nudge", summary="Send a reminder now to judges with pending work")
@policy("authenticated")
def nudge(request, e: str, payload: NudgeIn):
    ev = _org(request, e)
    from quorum.events.models import EventRole

    ids = [r.pk for r in EventRole.objects.filter(event=ev, role="judge", ref__in=payload.judges)] or None
    return {"sent": ops.nudge_now(ev, request.actor, ids, payload.message)}
