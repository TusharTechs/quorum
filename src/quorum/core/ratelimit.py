"""Database-backed fixed-window rate limiting (no Redis)."""

import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import connection

from .clock import now


def hkey(*parts: str) -> str:
    """Keyed hash so rate-limit keys never store raw emails or IP addresses."""
    msg = "|".join(parts).encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]


def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Count one event against `key`. Returns (allowed, count_in_window)."""
    t = now()
    epoch = int(t.timestamp())
    start = t - timedelta(seconds=epoch % window_seconds)
    start = start.replace(microsecond=0)
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core_ratebucket (key, window_start, count) VALUES (%s, %s, 1)
            ON CONFLICT (key, window_start) DO UPDATE SET count = core_ratebucket.count + 1
            RETURNING count
            """,
            [key, start],
        )
        count = cur.fetchone()[0]
    return count <= limit, count


def client_net(request) -> str:
    """Client network (/24 for IPv4, /48 for IPv6), never stored raw."""
    ip = request.META.get("REMOTE_ADDR", "")
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    trusted = getattr(settings, "TRUSTED_PROXIES", [])
    if xff and ip in trusted:
        ip = xff.split(",")[0].strip()
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + "::/48"
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else ip
