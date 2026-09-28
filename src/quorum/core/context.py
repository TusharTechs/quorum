import os

from django.conf import settings

from engine import ENGINE_VERSION


def demo_reset_in() -> int | None:
    """Public demo: minutes until the next scheduled reset. The fixture event is recreated by every
    reset, so its creation time is the last reset, shared by every worker and replica."""
    from quorum.core.clock import now
    from quorum.events.models import Event

    last = Event.objects.filter(slug="sample-hack-2026").values_list("created_at", flat=True).first()
    if last is None:
        return None
    left = settings.DEMO_RESET_MINUTES * 60 - (now() - last).total_seconds()
    return max(1, round(left / 60))


def quorum_context(request):
    my = {"organizer": False, "judge": False, "participant": False}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        roles = set(user.event_roles.values_list("role", flat=True))
        my = {"organizer": "organizer" in roles or user.is_superuser, "judge": "judge" in roles,
              "participant": "participant" in roles}
    return {
        "QUORUM_DEMO": settings.QUORUM_DEMO,
        "PUBLIC_DEMO": settings.QUORUM_PUBLIC_DEMO,
        "demo_reset_in": demo_reset_in() if settings.QUORUM_PUBLIC_DEMO else None,
        "SOURCE_URL": settings.QUORUM_SOURCE_URL,
        "QUORUM_ENV": settings.QUORUM_ENV,
        "PUBLIC_ORIGIN": settings.PUBLIC_ORIGIN,
        "my": my,
        "build_info": f"{ENGINE_VERSION} · {os.environ.get('QUORUM_BUILD', 'dev')}",
    }
