import os

from django.conf import settings

from engine import ENGINE_VERSION


def quorum_context(request):
    my = {"organizer": False, "judge": False, "participant": False}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        roles = set(user.event_roles.values_list("role", flat=True))
        my = {"organizer": "organizer" in roles or user.is_superuser, "judge": "judge" in roles,
              "participant": "participant" in roles}
    return {
        "QUORUM_DEMO": settings.QUORUM_DEMO,
        "QUORUM_ENV": settings.QUORUM_ENV,
        "PUBLIC_ORIGIN": settings.PUBLIC_ORIGIN,
        "my": my,
        "build_info": f"{ENGINE_VERSION} · {os.environ.get('QUORUM_BUILD', 'dev')}",
    }
