"""Sign-in helpers: passwords, magic links, API tokens."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from quorum.core.clock import now
from quorum.core.mail import queue_email
from quorum.core.middleware import token_hash
from quorum.policy.errors import Invalid

from .models import ApiToken, MagicLink, User


def issue_magic_link(email: str, purpose: str, next_url: str = "", payload: dict | None = None,
                     ttl_minutes: int = 15, subject: str | None = None, intro: str | None = None) -> str:
    """Create a single-use link and queue the e-mail. Returns the URL (tests/demo use it)."""
    email = (email or "").strip().lower()
    token = secrets.token_urlsafe(32)
    MagicLink.objects.create(email=email, token_hash=hashlib.sha256(token.encode()).hexdigest(), purpose=purpose,
                             next_url=next_url[:300], payload=payload or {},
                             expires_at=now() + timedelta(minutes=ttl_minutes))
    url = f"{settings.PUBLIC_ORIGIN}/magic/{token}"
    queue_email(email, subject or "Your Quorum sign-in link",
                (intro or "Use this link to sign in to Quorum. It works once and expires in 15 minutes.")
                + f"\n\n{url}\n\nIf you did not ask for this, ignore this e-mail.")
    return url


@transaction.atomic
def consume_magic_link(token: str) -> MagicLink | None:
    h = hashlib.sha256((token or "").encode()).hexdigest()
    link = MagicLink.objects.select_for_update().filter(token_hash=h).first()
    if not link or link.used_at or link.expires_at < now():
        return None
    link.used_at = now()
    link.save(update_fields=["used_at"])
    return link


def user_for_email(email: str, name: str = "") -> User:
    email = email.strip().lower()
    u = User.objects.filter(email__iexact=email).first()
    return u or User.objects.create_user(email=email, name=name)


def create_token(user: User, name: str, days: int | None = 90) -> tuple[ApiToken, str]:
    name = (name or "").strip()[:80]
    if not name:
        raise Invalid("Give the token a name so you recognise it later.")
    secret = "qm_" + secrets.token_urlsafe(32)
    tok = ApiToken.objects.create(user=user, name=name, prefix=secret[:12], token_hash=token_hash(secret),
                                  expires_at=now() + timedelta(days=days) if days else None)
    return tok, secret
