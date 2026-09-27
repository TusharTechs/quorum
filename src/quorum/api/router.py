from django.http import HttpRequest
from ninja import NinjaAPI

from quorum.policy.errors import PolicyError

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


@api.exception_handler(PolicyError)
def policy_error(request: HttpRequest, exc: PolicyError):
    return api.create_response(request, {"error": exc.code, "detail": exc.detail, **exc.extra}, status=exc.status)


def _register():
    from . import exports, judging, projects

    api.add_router("", projects.router)
    api.add_router("", judging.router)
    api.add_router("", exports.router)


_register()
