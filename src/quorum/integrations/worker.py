"""Background delivery: e-mail and webhooks from the transactional outbox, judge reminders,
and submission sealing at the deadline. Runs as `manage.py run_worker` (its own container).
Deadlines are never enforced here: the app and the database compare against the clock
directly, so a stopped worker cannot let a late submission through."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
import urllib.request
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction

from quorum.core.clock import now
from quorum.core.models import Outbox

log = logging.getLogger("quorum.worker")
BACKOFF = [10, 30, 120, 600, 1800, 3600, 7200, 14400]


def _claim(limit=20):
    with transaction.atomic():
        rows = list(Outbox.objects.select_for_update(skip_locked=True)
                    .filter(status__in=["pending", "failed"], next_attempt_at__lte=now())
                    .order_by("id")[:limit])
        for r in rows:
            r.attempts += 1
            r.save(update_fields=["attempts"])
        return rows


def deliver_email(row: Outbox):
    p = row.payload
    msg = EmailMultiAlternatives(p["subject"], p["body"], settings.DEFAULT_FROM_EMAIL, [p["to"]])
    if p.get("html"):
        msg.attach_alternative(p["html"], "text/html")
    msg.send(fail_silently=False)


def ssrf_check(url: str, allow_private: bool = False):
    """Refuse targets that resolve to loopback/private/link-local addresses (the compose
    network: db, mail) unless explicitly allowed. Returns the resolved IP to connect to."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("webhook URL must be http(s)")
    infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
            raise ValueError(f"webhook target {u.hostname} resolves to a private address")
    return infos[0][4][0]


def sign_webhook(secret: str, body: bytes, ts: int) -> str:
    return "t=%d,v1=%s" % (ts, hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest())


def deliver_webhook(row: Outbox):
    from quorum.integrations.models import WebhookEndpoint

    hook = WebhookEndpoint.objects.filter(pk=row.target, active=True).first()
    if not hook:
        return
    allow_private = hook.url.startswith(tuple(getattr(settings, "WEBHOOK_PRIVATE_ALLOWLIST", [])))
    ssrf_check(hook.url, allow_private=allow_private or settings.QUORUM_DEMO)
    body = json.dumps(row.payload, sort_keys=True).encode()
    ts = int(time.time())
    req = urllib.request.Request(hook.url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "Quorum-Webhooks/1",
        "X-Quorum-Signature": sign_webhook(hook.secret, body, ts), "X-Quorum-Event": row.payload.get("action", ""),
        "X-Quorum-Delivery": str(row.id)})
    with urllib.request.urlopen(req, timeout=8) as resp:
        hook.last_status = f"{resp.status} at {now():%Y-%m-%d %H:%M}"
    hook.save(update_fields=["last_status"])


def process_outbox() -> int:
    if settings.QUORUM_PUBLIC_DEMO:  # never mail or call addresses that anonymous visitors typed in
        return 0
    n = 0
    for row in _claim():
        try:
            if row.topic == "email":
                deliver_email(row)
            elif row.topic == "webhook":
                deliver_webhook(row)
            row.status, row.sent_at, row.last_error = "sent", now(), ""
            n += 1
        except Exception as e:  # delivery failures are retried with backoff, never lost
            row.last_error = str(e)[:500]
            if row.attempts >= len(BACKOFF):
                row.status = "dead"
            else:
                row.status = "failed"
                row.next_attempt_at = now() + timedelta(seconds=BACKOFF[row.attempts - 1])
            log.warning("delivery failed", extra={"event": row.topic, "status": row.last_error[:120]})
        row.save(update_fields=["status", "sent_at", "last_error", "next_attempt_at"])
    return n


def tick():
    from quorum.events.models import Event
    from quorum.events.services import seal_submissions
    from quorum.judging.ops import send_due_reminders

    t = now()
    for ev in Event.objects.filter(submissions_close_at__lte=t).exclude(phase="archived"):
        if not (ev.options or {}).get("sealed_at") and t >= ev.effective_close:
            seal_submissions(ev)
    send_due_reminders()
    return process_outbox()


def run_forever(interval: float = 2.0):
    log.info("worker started")
    while True:
        try:
            tick()
        except Exception:
            log.exception("worker tick failed")
        time.sleep(interval)
