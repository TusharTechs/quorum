"""Lookups shared by API routers. Protected lookups never distinguish 'does not exist'
from 'not yours' (uniform 403), so IDs cannot be enumerated by probing."""

from __future__ import annotations

import re

from quorum.events.models import Event, EventRole, Project
from quorum.policy.errors import Forbidden, NotFound

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$")


def get_event(key: str) -> Event:
    qs = Event.objects.all()
    ev = qs.filter(ref=key).first() or qs.filter(slug=key).first()
    if not ev and UUID_RE.match(key or ""):
        ev = qs.filter(pk=key).first()
    if not ev:
        raise NotFound("No such event.")
    return ev


def get_project(event: Event, key: str) -> Project:
    qs = Project.objects.filter(event=event).select_related("team", "track", "event")
    p = qs.filter(ref=key).first()
    if not p and UUID_RE.match(key or ""):
        p = qs.filter(pk=key).first()
    if not p:
        raise NotFound("No such project.")
    return p


def find_project(key: str) -> Project:
    """Global lookup by UUID (used by /projects/{id} routes)."""
    if not UUID_RE.match(key or ""):
        raise NotFound("No such project.")
    p = Project.objects.select_related("team", "track", "event").filter(pk=key).first()
    if not p:
        raise NotFound("No such project.")
    return p


def get_judge_role_or_403(event: Event, key: str) -> EventRole:
    qs = EventRole.objects.filter(event=event, role="judge").select_related("user")
    r = qs.filter(ref=key).first()
    if not r and UUID_RE.match(key or ""):
        r = qs.filter(pk=key).first()
    if not r:
        raise Forbidden("You may not read these scores.")
    return r
