"""Deny by default. Every API operation and every HTML view must carry a policy marker;
a system check (quorum.core.checks) fails startup if one is missing. The marker names the
coarse rule; object-level checks happen in the scoped repositories (policy.repos)."""

from __future__ import annotations

import functools

from django.middleware.csrf import CsrfViewMiddleware

from .actor import Actor
from .errors import Forbidden

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_csrf_checker = CsrfViewMiddleware(lambda r: None)


def enforce_csrf(request):
    """Cookie-authenticated writes must carry Django's CSRF token. Bearer-token requests are
    exempt (browsers never attach that header by themselves). API views are csrf_exempt at
    the framework level, so this is where the check happens for them."""
    if request.method in SAFE_METHODS or getattr(request, "auth_via", "session") != "session":
        return
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return
    if _csrf_checker.process_view(request, None, (), {}) is not None:
        raise Forbidden("CSRF token missing or invalid.", code="csrf_failed")


RULES = {"public", "authenticated", "participant", "judge", "organizer", "admin", "event-member"}


def policy(rule: str):
    if rule not in RULES:
        raise ValueError(f"unknown policy rule {rule!r}")

    def deco(fn):
        @functools.wraps(fn)
        def inner(request, *args, **kwargs):
            actor = Actor.of(request)
            if getattr(request, "auth_via", None) == "invalid_token":
                from .errors import NotAuthenticated

                raise NotAuthenticated("The API token is invalid, expired or revoked.", code="invalid_token")
            enforce_csrf(request)
            if rule != "public":
                actor.require_auth()
            if rule == "admin":
                actor.require_admin()
            request.actor = actor
            return fn(request, *args, **kwargs)

        inner.quorum_policy = rule
        return inner

    return deco
