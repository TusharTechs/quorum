from __future__ import annotations

from django.db import transaction

from engine import ENGINE_VERSION
from engine.pipeline import compute
from quorum.audit import service as audit
from quorum.judging import method as methods

from .inputs import build_input
from .models import RankingRun


def latest_run(event, kind=None):
    qs = RankingRun.objects.filter(event=event)
    if kind:
        qs = qs.filter(kind=kind)
    return qs.order_by("-created_at").first()


@transaction.atomic
def compute_run(event, actor=None, kind=RankingRun.Kind.PREVIEW, heavy=True) -> RankingRun:
    """Compute (or reuse) a ranking run for the event's current data and locked method.
    Runs are content-addressed: identical input -> the existing run is returned."""
    m = methods.current(event)
    inp = build_input(event, m)
    from engine.pipeline import input_hash

    h = input_hash(inp)
    existing = RankingRun.objects.filter(event=event, input_hash=h, kind=kind).first()
    if existing and (not heavy or "loo" in existing.output):
        return existing
    out = compute(inp, heavy=heavy)
    run = RankingRun.objects.create(event=event, kind=kind, method_hash=m.spec_hash if m else "",
                                    engine_version=ENGINE_VERSION, input=inp, input_hash=h, output=out,
                                    output_hash=out["output_hash"], created_by=getattr(actor, "user", actor))
    sig = out.get("signal", {})
    audit.record("RANKING_COMPUTED",
                 f"{kind.title()} ranking computed over {out['n_reviews']} reviews "
                 f"(signal: {sig.get('verdict', 'n/a')}, output {out['output_hash'][:12]}…)",
                 event=event, actor=actor, actor_role="organizer" if actor else "system", target=run,
                 data={"input_hash": h, "output_hash": out["output_hash"], "kind": kind})
    return run
