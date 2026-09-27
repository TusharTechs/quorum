from django.db import IntegrityError, InternalError
from django.http import HttpRequest
from ninja import NinjaAPI
from ninja.errors import AuthenticationError
from ninja.security.base import AuthBase

from quorum.policy.errors import PolicyError


class QuorumAuth(AuthBase):
    """Authentication is the FIRST check on every non-public operation: ninja runs auth
    callbacks before parsing parameters or bodies, so an anonymous or invalid-token request
    gets 401, never a 422 that would leak the shape of a protected endpoint. The request was
    already authenticated by middleware (Bearer token or session); this only enforces it."""

    openapi_type = "http"
    openapi_scheme = "bearer"

    def __call__(self, request):
        user = getattr(request, "user", None)
        return user if user is not None and user.is_authenticated else None


quorum_auth = QuorumAuth()

api = NinjaAPI(
    title="Quorum API",
    version="1.0",
    description=(
        "Every action in the Quorum UI is available here. Authenticate with "
        "`Authorization: Bearer <token>` (create tokens at /settings/tokens) or a browser session "
        "with a CSRF token. Errors are `{\"error\": code, \"detail\": text}` with stable codes: "
        "`not_authenticated` (401), `forbidden` / `deadline_passed` / `window_closed` (403), "
        "`not_found` (404), `conflict` (409), `validation_error` (422), `rate_limited` (429)."
    ),
    urls_namespace="api",
)


@api.exception_handler(AuthenticationError)
def auth_error(request: HttpRequest, exc):
    invalid = getattr(request, "auth_via", "") == "invalid_token"
    return api.create_response(request, {
        "error": "invalid_token" if invalid else "not_authenticated",
        "detail": "The API token is invalid, expired or revoked." if invalid else "Sign in or send an API token.",
    }, status=401)


@api.exception_handler(InternalError)
@api.exception_handler(IntegrityError)
def integrity_error(request: HttpRequest, exc):
    """Safety net: a uniqueness or trigger violation is a conflict, never a 500. The first
    line of a trigger message (e.g. 'deadline_passed: ...') is passed through."""
    msg = str(exc).split("\n")[0][:200]
    code = "deadline_passed" if "deadline_passed" in msg else "conflict"
    return api.create_response(request, {"error": code, "detail": msg}, status=403 if code == "deadline_passed" else 409)


@api.exception_handler(PolicyError)
def policy_error(request: HttpRequest, exc: PolicyError):
    return api.create_response(request, {"error": exc.code, "detail": exc.detail, **exc.extra}, status=exc.status)


def _register():
    from . import audit, data, events, exports, feedback, judges, judging, ops, pairwise, projects, results, teams, voting

    for mod in (events, teams, projects, judging, judges, pairwise, ops, results, feedback, voting, audit, exports, data):
        api.add_router("", mod.router)
    # derive ninja-level auth from the @policy marker (single source of truth)
    for _prefix, router in api._routers:
        for path_view in router.path_operations.values():
            for op in path_view.operations:
                if getattr(op.view_func, "quorum_policy", "public") != "public":
                    op.auth_callbacks = [quorum_auth]
                    op.auth_param = quorum_auth


_register()
