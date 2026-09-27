
from django.http import HttpResponse
from ninja import Router

from quorum.audit import service as audit
from quorum.integrations import exports
from quorum.policy.decorators import policy
from quorum.policy.errors import NotFound

from .common import get_event

router = Router(tags=["exports"])


@router.get("/events/{e}/exports/{kind}.csv", summary="CSV export at every stage (organizers)")
@policy("authenticated")
def export_csv(request, e: str, kind: str):
    """kind: projects | teams | judges | assignments | reviews | scores | results | votes | feedback | audit.
    Cells that could be interpreted as spreadsheet formulas are neutralised."""
    ev = get_event(e)
    request.actor.require_organizer(ev)
    if kind not in exports.CSV_EXPORTS:
        raise NotFound(f"Unknown export '{kind}'.")
    body = exports.CSV_EXPORTS[kind](ev)
    audit.record("EXPORT_DOWNLOADED", f"{kind}.csv exported", event=ev, actor=request.actor,
                 actor_role="organizer", data={"kind": kind})
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{ev.ref}-{kind}.csv"'
    return resp
