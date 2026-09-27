from typing import Any, Optional

from ninja import Router, Schema

from quorum.events.models import Project
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound
from quorum.results import decide
from quorum.results.models import RankingRun, TiebreakRound
from quorum.results.service import compute_run, latest_run

from .common import get_event

router = Router(tags=["results"])


class ComputeIn(Schema):
    heavy: bool = True


class FocusIn(Schema):
    budget: Optional[int] = None
    seed: int = 0


class FocusCommitIn(Schema):
    plan: dict[str, Any]


class TiebreakIn(Schema):
    projects: list[str]
    boundary: int = 1


class ResolveIn(Schema):
    order: Optional[list[str]] = None
    note: str = ""


class ReasonIn(Schema):
    reason: str


def _org(request, e):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return ev


def run_out(r: RankingRun, full=False) -> dict:
    out = r.output
    d = {"id": str(r.pk), "kind": r.kind, "created_at": r.created_at, "input_hash": r.input_hash,
         "output_hash": r.output_hash, "method_hash": r.method_hash, "engine": r.engine_version,
         "signal": out.get("signal"), "k": out.get("k"), "flat_judges": list((out.get("flat_judges") or {}).keys()),
         "entries": out.get("entries")}
    if full:
        d.update({"judges": out.get("judges"), "agreement": out.get("agreement"), "loo": out.get("loo"),
                  "sensitivity": out.get("sensitivity")})
    return d


@router.post("/events/{e}/rankings", summary="Compute (or reuse) a ranking run for the current data")
@policy("authenticated")
def compute(request, e: str, payload: ComputeIn):
    return run_out(compute_run(_org(request, e), request.actor, heavy=payload.heavy))


@router.get("/events/{e}/rankings/latest", summary="Latest ranking run with uncertainty, ties and judge stats")
@policy("authenticated")
def latest(request, e: str):
    r = latest_run(_org(request, e))
    if not r:
        raise NotFound("No ranking has been computed yet.")
    return run_out(r, full=True)


@router.get("/events/{e}/rankings/latest/explain/{ref}", summary="Exact per-judge explanation of one project's score")
@policy("authenticated")
def explain(request, e: str, ref: str):
    r = latest_run(_org(request, e))
    if not r or ref not in (r.output.get("explanations") or {}):
        raise NotFound("No explanation for that project.")
    return r.output["explanations"][ref]


@router.get("/events/{e}/ties", summary="Statistical ties that touch a prize position")
@policy("authenticated")
def ties(request, e: str):
    ev = _org(request, e)
    r = compute_run(ev, request.actor, heavy=False)
    return [{k: v for k, v in t.items() if k != "entries"} for t in decide.boundary_ties(r, ev.prize_positions)]


@router.post("/events/{e}/focus/plan", summary="Dry-run a focus round (adaptive extra reviews)")
@policy("authenticated")
def focus_plan(request, e: str, payload: FocusIn):
    return decide.plan_focus(_org(request, e), payload.budget, payload.seed)


@router.post("/events/{e}/focus/commit", summary="Commit a focus round and e-mail the judges")
@policy("authenticated")
def focus_commit(request, e: str, payload: FocusCommitIn):
    fr = decide.commit_focus(_org(request, e), request.actor, payload.plan)
    return {"id": str(fr.pk), "budget": fr.budget}


@router.post("/events/{e}/tiebreaks", response={201: dict}, summary="Open a pairwise tie-break round")
@policy("authenticated")
def open_tb(request, e: str, payload: TiebreakIn):
    tb = decide.open_tiebreak(_org(request, e), request.actor, payload.projects, payload.boundary)
    return 201, {"id": str(tb.pk), "panel": [r.ref for r in tb.judge_roles.all()]}


@router.get("/tiebreaks/{tid}", summary="Tie-break state: fused order and adjacent probabilities")
@policy("authenticated")
def tb_state(request, tid: str):
    tb = TiebreakRound.objects.filter(pk=tid).select_related("event").first()
    if not tb:
        raise NotFound("No such tie-break.")
    request.actor.require_organizer(tb.event)
    st = decide.tiebreak_state(tb) if tb.status == "open" else tb.result
    return {"status": tb.status, "boundary": tb.boundary, "state": st}


@router.post("/tiebreaks/{tid}/resolve", summary="Resolve a tie-break (P >= 0.80) or record an organizer decision")
@policy("authenticated")
def tb_resolve(request, tid: str, payload: ResolveIn):
    tb = TiebreakRound.objects.filter(pk=tid).select_related("event").first()
    if not tb:
        raise NotFound("No such tie-break.")
    request.actor.require_organizer(tb.event)
    tb = decide.resolve_tiebreak(tb, request.actor, payload.order, payload.note)
    return {"status": tb.status, "order": tb.result.get("order")}


@router.get("/me/tiebreaks/{tid}/next", summary="Judge: the next pair to compare in a tie-break")
@policy("authenticated")
def tb_next(request, tid: str):
    tb = TiebreakRound.objects.filter(pk=tid).select_related("event").first()
    if not tb:
        raise NotFound("No such tie-break.")
    role = request.actor.judge_role(tb.event)
    if not role or not tb.judge_roles.filter(pk=role.pk).exists():
        raise Forbidden("You are not on this panel.")
    pair, done, total = decide.tiebreak_next_pair(tb, role)
    return {"pair": pair, "done": done, "total": total}


class CompareIn(Schema):
    a: str
    b: str
    outcome: str
    reason: str = ""


@router.post("/me/tiebreaks/{tid}/compare", summary="Judge: record a pairwise choice (a, b or tie)")
@policy("authenticated")
def tb_compare(request, tid: str, payload: CompareIn):
    from quorum.judging.services import record_comparison

    tb = TiebreakRound.objects.filter(pk=tid).select_related("event").first()
    if not tb:
        raise NotFound("No such tie-break.")
    role = request.actor.judge_role(tb.event)
    if not role:
        raise Forbidden("Judges only.")
    pa = Project.objects.filter(event=tb.event, ref=payload.a).first()
    pb = Project.objects.filter(event=tb.event, ref=payload.b).first()
    if not pa or not pb:
        raise NotFound("Unknown project.")
    c = record_comparison(request.actor, tb.event, role, pa, pb, payload.outcome, tiebreak=tb, reason=payload.reason)
    return {"id": str(c.pk)}


@router.post("/events/{e}/results/lock", summary="Lock results: official run, tie-breaks applied, signed checkpoint")
@policy("authenticated")
def lock(request, e: str):
    pub = decide.lock_results(_org(request, e), request.actor)
    return {"final_order": pub.final_order, "run": pub.run.output_hash, "audit_head": pub.audit_head}


@router.post("/events/{e}/results/publish", summary="Publish results, release feedback, issue certificates")
@policy("authenticated")
def publish(request, e: str):
    pub = decide.publish_results(_org(request, e), request.actor)
    return {"published_at": pub.published_at, "audit_head": pub.audit_head, "signature": pub.checkpoint_signature}


@router.post("/events/{e}/results/reopen", summary="Reopen locked (unpublished) results, with a reason")
@policy("authenticated")
def reopen(request, e: str, payload: ReasonIn):
    decide.reopen_results(_org(request, e), request.actor, payload.reason)
    return {"ok": True}


@router.get("/events/{e}/results", summary="Published results (public once published)")
@policy("public")
def public_results(request, e: str):
    ev = get_event(e)
    pub = getattr(ev, "publication", None)
    if not (ev.published and pub) and not request.actor.is_organizer(ev):
        raise Forbidden("Results are hidden until the organizers publish them.", code="results_not_published")
    if not pub:
        raise NotFound("Results are not locked yet.")
    entries = {x["project"]: x for x in pub.run.output.get("entries", [])}
    titles = {p.ref: p.title for p in Project.objects.filter(event=ev)}
    return {"published_at": pub.published_at, "run_output_hash": pub.run.output_hash, "audit_head": pub.audit_head,
            "method_hash": pub.run.method_hash, "tiebreaks": pub.tiebreak_results,
            "ranking": [{"place": i + 1, "project": r, "title": titles.get(r), "calibrated": entries[r]["calibrated"],
                         "se": entries[r]["se"], "rank_interval": [entries[r]["rank_lo"], entries[r]["rank_hi"]]}
                        for i, r in enumerate(pub.final_order) if r in entries]}
