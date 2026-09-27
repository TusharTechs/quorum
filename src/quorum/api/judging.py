
from ninja import Router

from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden
from quorum.policy.repos import own_reviews, reviews_of_judge

from .common import get_event, get_judge_role_or_403
from .schemas import ScoreOut
from .serialize import review_out

router = Router(tags=["judging"])


def _weights(event):
    return {c.key: c.weight_bp for c in event.criteria.all()}


@router.get("/me/scores", response=list[ScoreOut], summary="A judge's own scores")
@policy("authenticated")
def my_scores(request, event: str = None):
    """Only the caller's own reviews. Non-judges get 403 (a participant is not a judge)."""
    actor = request.actor
    if not actor.user.event_roles.filter(role="judge").exists():
        raise Forbidden("Only judges have scores.")
    qs = own_reviews(actor, get_event(event) if event else None).prefetch_related("scores__criterion")
    weights = {}
    out = []
    for r in qs.order_by("event_id", "project__ref"):
        if r.event_id not in weights:
            weights[r.event_id] = _weights(r.event)
        out.append(review_out(r, weights[r.event_id]))
    return out


@router.get("/events/{e}/judges/{j}/scores", response=list[ScoreOut], summary="One judge's scores")
@policy("authenticated")
def judge_scores(request, e: str, j: str):
    """Judge isolation, enforced here and nowhere else: the judge themself or an organizer.
    Any other caller gets 403 whether or not that judge exists."""
    ev = get_event(e)
    role = get_judge_role_or_403(ev, j)
    qs = reviews_of_judge(request.actor, ev, role).prefetch_related("scores__criterion")
    w = _weights(ev)
    return [review_out(r, w) for r in qs.order_by("project__ref")]
