import hashlib

from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from quorum.policy.errors import PolicyError


class SecurityHeadersMiddleware:
    """Content-Security-Policy without inline scripts; only /embed/ pages may be framed."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path
        embed = path.startswith("/embed/")
        docs = path.startswith("/api/v1/docs")
        frame = "*" if embed else "'none'"
        script = "'self' 'unsafe-inline'" if docs else "'self'"
        response.setdefault(
            "Content-Security-Policy",
            f"default-src 'self'; script-src {script}; style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
            f"base-uri 'none'; form-action 'self'; frame-ancestors {frame}",
        )
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if embed and "X-Frame-Options" in response:
            del response["X-Frame-Options"]
        return response


def token_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class BearerTokenMiddleware:
    """`Authorization: Bearer <token>` authenticates the request and *replaces* any cookie
    session. Browsers never attach this header on their own, so token requests are not
    CSRF-able and skip the CSRF check; cookie requests keep Django's CSRF protection."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if auth[:7].lower() == "bearer ":
            from quorum.accounts.models import ApiToken

            secret = auth[7:].strip()
            tok = (ApiToken.objects.select_related("user")
                   .filter(token_hash=token_hash(secret), revoked_at__isnull=True).first())
            now = timezone.now()
            if tok and (tok.expires_at is None or tok.expires_at > now) and tok.user.is_active:
                request.user = tok.user
                request.auth_via = "token"
                if not tok.last_used_at or (now - tok.last_used_at).total_seconds() > 60:
                    ApiToken.objects.filter(pk=tok.pk).update(last_used_at=now)
            else:
                request.user = AnonymousUser()
                request.auth_via = "invalid_token"
            request._dont_enforce_csrf_checks = True
        else:
            request.auth_via = "session"
        return self.get_response(request)


class PolicyErrorMiddleware:
    """Turns policy refusals raised in HTML views into proper 401/403 pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exc):
        if not isinstance(exc, PolicyError):
            return None
        wants_json = request.path.startswith("/api/") or "application/json" in request.META.get("HTTP_ACCEPT", "")
        if wants_json:
            return JsonResponse({"error": exc.code, "detail": exc.detail, **exc.extra}, status=exc.status)
        if exc.status == 401 and request.method == "GET":
            return HttpResponseRedirect(f"/login?next={request.get_full_path()}")
        return render(request, "error.html", {"error": exc}, status=exc.status)
