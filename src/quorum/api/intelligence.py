"""Search and ask: the ⌘K palette's API (role-aware; see quorum.intelligence)."""

from ninja import Router

from quorum.api.common import get_event
from quorum.intelligence import ask as asking
from quorum.intelligence import embed, palette
from quorum.policy.decorators import policy

router = Router(tags=["search & assistant"])


@router.get("/search", summary="Search pages, actions, projects and people the caller may open")
@policy("public")
def search(request, q: str = "", event: str = ""):
    return palette.search(request.actor, q, event or None)


@router.get("/ask", summary="Ask Quorum: a plain-language question answered only from the event's own data")
@policy("public")
def ask(request, q: str, event: str):
    """Matched to a named skill, run with the caller's permissions; returns the answer, the skill that
    ran and how it was computed, or `answer: null` when no skill fits the question."""
    return {"answer": asking.ask(request.actor, q, get_event(event)), "engine": embed.status()["model"],
            "semantic": embed.available()}
