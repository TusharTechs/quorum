from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from quorum.accounts import services as accounts
from quorum.accounts.models import ApiToken
from quorum.audit import service as audit
from quorum.core import ratelimit
from quorum.core.clock import now
from quorum.policy.decorators import policy
from quorum.policy.errors import RateLimited


def _safe_next(request, default="/"):
    nxt = request.POST.get("next") or request.GET.get("next") or default
    return nxt if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) else default


@policy("public")
def login_view(request):
    ctx = {"next": _safe_next(request, "/me"), "nav": ""}
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        ok1, _ = ratelimit.hit("login:" + ratelimit.hkey(email), 10, 900)
        ok2, _ = ratelimit.hit("login-net:" + ratelimit.hkey(ratelimit.client_net(request)), 50, 900)
        if not (ok1 and ok2):
            raise RateLimited("Too many sign-in attempts. Wait a few minutes.")
        if request.POST.get("mode") == "magic":
            if email:
                accounts.issue_magic_link(email, "login", next_url=ctx["next"])
            messages.success(request, "If that address can sign in, a link is on its way. "
                                      "(Offline demo: open the mail catcher at localhost:8025.)")
            return redirect("/login")
        user = authenticate(request, username=email, password=request.POST.get("password", ""))
        if user is None:
            ctx["error"] = "That e-mail and password do not match."
            ctx["email"] = email
            return render(request, "auth/login.html", ctx, status=400)
        login(request, user)
        return redirect(ctx["next"])
    return render(request, "auth/login.html", ctx)


@policy("public")
def signup_view(request):
    ctx = {"next": _safe_next(request, "/me"), "nav": ""}
    if request.method == "POST":
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        from quorum.accounts.models import User

        email = (request.POST.get("email") or "").strip().lower()
        name = (request.POST.get("name") or "").strip()[:120]
        pw = request.POST.get("password", "")
        errors = {}
        if "@" not in email:
            errors["email"] = "Enter a valid e-mail."
        elif User.objects.filter(email__iexact=email).exists():
            errors["email"] = "An account exists for this e-mail. Sign in, or use an e-mail link."
        try:
            validate_password(pw)
        except ValidationError as e:
            errors["password"] = " ".join(e.messages)
        if errors:
            return render(request, "auth/signup.html", {**ctx, "errors": errors, "email": email, "name": name}, status=400)
        user = User.objects.create_user(email=email, password=pw, name=name)
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        audit.record("USER_SIGNED_UP", f"{user.email} created an account", actor=user, actor_role="participant")
        return redirect(ctx["next"])
    return render(request, "auth/signup.html", ctx)


@policy("public")
def logout_view(request):
    if request.method == "POST":
        logout(request)
    return redirect("/")


@policy("public")
def magic_view(request, token):
    link = accounts.consume_magic_link(token)
    if not link:
        return render(request, "auth/magic_invalid.html", status=400)
    user = accounts.user_for_email(link.email, link.payload.get("name", ""))
    if link.purpose == "voter":
        from quorum.voting import services as voting

        return voting.complete_email_verification(request, link)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    nxt = link.next_url if url_has_allowed_host_and_scheme(link.next_url, allowed_hosts={request.get_host()}) else "/me"
    return redirect(nxt or "/me")


@policy("authenticated")
def tokens_view(request):
    new_secret = None
    if request.method == "POST":
        if request.POST.get("revoke"):
            ApiToken.objects.filter(user=request.user, pk=request.POST["revoke"]).update(revoked_at=now())
            messages.success(request, "Token revoked.")
            return redirect("/settings/tokens")
        tok, new_secret = accounts.create_token(request.user, request.POST.get("name", ""))
        audit.record("TOKEN_CREATED", f"API token '{tok.name}' created", actor=request.user, actor_role="user",
                     target=tok)
    tokens = ApiToken.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "auth/tokens.html", {"tokens": tokens, "new_secret": new_secret, "nav": ""})
