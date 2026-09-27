
from ninja import Router, Schema

from quorum.events import organize
from quorum.events.models import Event
from quorum.judging import method as methods
from quorum.policy.decorators import policy

from .common import get_event

router = Router(tags=["events"])


class EventIn(Schema):
    name: str | None = None
    tagline: str | None = None
    description_md: str | None = None
    rules_md: str | None = None
    submissions_open_at: str | None = None
    submissions_close_at: str | None = None
    judging_opens_at: str | None = None
    judging_closes_at: str | None = None
    voting_opens_at: str | None = None
    voting_closes_at: str | None = None
    grace_seconds: int | None = None
    team_size_max: int | None = None
    reviews_per_project: int | None = None
    batch_size: int | None = None
    min_feedback_chars: int | None = None
    focus_budget_pct: int | None = None
    prize_positions: int | None = None
    voting_mode: str | None = None
    votes_per_voter: int | None = None
    quadratic_voting: bool | None = None
    rank_display: str | None = None
    template: str | None = "raptors"


class CloneIn(Schema):
    name: str
    shift_days: int = 30


class CriterionIn(Schema):
    key: str | None = None
    name: str
    description: str = ""
    weight_pct: float
    scale_max: int = 5


class RubricIn(Schema):
    criteria: list[CriterionIn]
    override_reason: str | None = None


class NamedIn(Schema):
    name: str


class PrizeIn(Schema):
    name: str
    places: int = 1
    kind: str = "judged"
    value_text: str = ""
    description: str = ""
    per_track: bool = False  # awarded in every track (best in track)


class QuestionIn(Schema):
    label: str
    kind: str = "text"
    required: bool = False
    options: str = ""


class PhaseIn(Schema):
    phase: str


def event_out(ev: Event) -> dict:
    m = methods.current(ev)
    return {
        "id": str(ev.pk), "ref": ev.ref, "slug": ev.slug, "name": ev.name, "tagline": ev.tagline, "phase": ev.phase,
        "submissions_open_at": ev.submissions_open_at, "submissions_close_at": ev.submissions_close_at,
        "grace_seconds": ev.grace_seconds, "judging_opens_at": ev.judging_opens_at, "judging_closes_at": ev.judging_closes_at,
        "voting_mode": ev.voting_mode, "voting_opens_at": ev.voting_opens_at, "voting_closes_at": ev.voting_closes_at,
        "results_published_at": ev.results_published_at,
        "tracks": [{"ref": t.ref, "name": t.name} for t in ev.tracks.all()],
        "prizes": [{"name": p.name, "places": p.places, "kind": p.kind, "value": p.value_text, "per_track": p.per_track}
                   for p in ev.prizes.all()],
        "criteria": [{"key": c.key, "name": c.name, "weight_pct": c.weight_pct, "scale": [c.scale_min, c.scale_max]}
                     for c in ev.criteria.all()],
        "method": {"version": m.version, "spec_hash": m.spec_hash, "locked_at": m.locked_at,
                   "overridden": bool(m.override_reason)} if m else None,
    }


@router.get("/events", summary="List public events")
@policy("public")
def list_events(request):
    return [event_out(e) for e in Event.objects.filter(is_listed=True)]


@router.post("/events", response={201: dict}, summary="Create an event (organizers/admins)")
@policy("authenticated")
def create_event(request, payload: EventIn):
    data = payload.dict(exclude_none=True)
    return 201, event_out(organize.create_event(request.actor, data, data.pop("template", "raptors")))


@router.get("/events/{e}", summary="Read an event, its rubric and locked method hash")
@policy("public")
def read_event(request, e: str):
    return event_out(get_event(e))


@router.patch("/events/{e}", summary="Edit an event (deadline changes are audited)")
@policy("authenticated")
def patch_event(request, e: str, payload: EventIn):
    return event_out(organize.update_event(request.actor, get_event(e), payload.dict(exclude_none=True)))


@router.post("/events/{e}/clone", response={201: dict}, summary="Clone an event with shifted dates")
@policy("authenticated")
def clone_event(request, e: str, payload: CloneIn):
    return 201, event_out(organize.clone_event(request.actor, get_event(e), payload.name, payload.shift_days))


@router.put("/events/{e}/rubric", summary="Replace the rubric (before the method is locked, or admin override)")
@policy("authenticated")
def put_rubric(request, e: str, payload: RubricIn):
    ev = get_event(e)
    organize.save_criteria(request.actor, ev, [c.dict() for c in payload.criteria], payload.override_reason)
    return event_out(ev)


@router.get("/events/{e}/method", summary="The locked judging method (canonical spec + sha256)")
@policy("public")
def get_method(request, e: str):
    ev = get_event(e)
    return [{"version": m.version, "spec": m.spec, "spec_hash": m.spec_hash, "locked_at": m.locked_at,
             "override_reason": m.override_reason} for m in ev.methods.order_by("version")]


@router.post("/events/{e}/publish", summary="Publish the event: opens registration and locks the method")
@policy("authenticated")
def publish_event(request, e: str):
    ev = get_event(e)
    organize.open_registration(request.actor, ev)
    return event_out(ev)


@router.post("/events/{e}/phase", summary="Move a judging phase (judging, focus, deliberation, archived)")
@policy("authenticated")
def set_phase(request, e: str, payload: PhaseIn):
    ev = get_event(e)
    organize.set_phase(request.actor, ev, payload.phase)
    return event_out(ev)


@router.post("/events/{e}/tracks", response={201: dict}, summary="Add a track")
@policy("authenticated")
def add_track(request, e: str, payload: NamedIn):
    t = organize.add_track(request.actor, get_event(e), payload.name)
    return 201, {"ref": t.ref, "name": t.name}


@router.post("/events/{e}/prizes", response={201: dict}, summary="Add a prize")
@policy("authenticated")
def add_prize(request, e: str, payload: PrizeIn):
    p = organize.add_prize(request.actor, get_event(e), payload.name, payload.places, payload.kind, payload.value_text,
                           payload.description, payload.per_track)
    return 201, {"name": p.name, "places": p.places, "kind": p.kind, "per_track": p.per_track}


@router.post("/events/{e}/questions", response={201: dict}, summary="Add a custom submission question")
@policy("authenticated")
def add_question(request, e: str, payload: QuestionIn):
    q = organize.add_question(request.actor, get_event(e), payload.label, payload.kind, payload.required, payload.options)
    return 201, {"ref": q.ref, "label": q.label, "kind": q.kind}
