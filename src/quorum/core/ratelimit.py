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


def _proxy_networks():
    import ipaddress

    nets = []
    for t in getattr(settings, "TRUSTED_PROXIES", []):
        try:
            nets.append(ipaddress.ip_network(t, strict=False))  # "10.0.0.5" or "172.16.0.0/12"
        except ValueError:
            continue
    return nets


def _is_trusted(ip: str, nets) -> bool:
    import ipaddress

    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in n for n in nets)


def client_ip(request) -> str:
    """The client's address. X-Forwarded-For is only believed when the direct peer is a trusted
    proxy, and then read from the right: the first hop that is not itself a trusted proxy is the
    client. The left-most entry is whatever the client chose to send, so it is never used.
    Platforms whose edge puts the client in a header of its own (Railway's X-Real-IP) name it in
    TRUSTED_CLIENT_IP_HEADER; it is read only from a trusted peer, which overwrites it."""
    import ipaddress

    ip = request.META.get("REMOTE_ADDR", "")
    nets = _proxy_networks()
    if not nets or not _is_trusted(ip, nets):
        return ip
    header = getattr(settings, "CLIENT_IP_HEADER", "")
    if header:
        v = request.META.get("HTTP_" + header.upper().replace("-", "_"), "").strip()
        try:
            return str(ipaddress.ip_address(v))
        except ValueError:
            pass
    hops = [h.strip() for h in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if h.strip()]
    for h in reversed(hops):
        if not _is_trusted(h, nets):
            return h
    return hops[0] if hops else ip


def client_net(request) -> str:
    """Client network (/24 for IPv4, /48 for IPv6), never stored raw."""
    ip = client_ip(request)
    if ":" in ip:
        return ":".join(ip.split(":")[:3]) + "::/48"
    parts = ip.split(".")
    return ".".join(parts[:3]) + ".0/24" if len(parts) == 4 else ip
