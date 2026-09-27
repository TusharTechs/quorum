"""Search and ask: the ⌘K palette's API (role-aware; see quorum.intelligence)."""

from ninja import Router

from quorum.intelligence import palette
from quorum.policy.decorators import policy

router = Router(tags=["search & assistant"])


@router.get("/search", summary="Search pages, actions, projects and people the caller may open")
@policy("public")
def search(request, q: str = "", event: str = ""):
    return palette.search(request.actor, q, event or None)
