"""The audit writer. Call `record()` inside the same transaction as the change it records.
Writers for one event are serialised with a Postgres advisory lock so the chain is linear."""

from __future__ import annotations

import hashlib

from django.db import connection, transaction

from engine.bundle import canonical_json
from quorum.core.clock import now

from .models import AuditCheckpoint, AuditEvent

GENESIS = "0" * 64


def _lock(event_id):
    key = int(hashlib.sha256(str(event_id or "system").encode()).hexdigest()[:15], 16)
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def row_digest(prev_hash: str, row: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(row)).encode()).hexdigest()


def _row(ev: AuditEvent) -> dict:
    return {
        "event": str(ev.event_id) if ev.event_id else None,
        "ts": ev.ts.isoformat(),
        "actor": str(ev.actor_id) if ev.actor_id else None,
        "actor_role": ev.actor_role,
        "action": ev.action,
        "target_type": ev.target_type,
        "target_id": ev.target_id,
        "summary": ev.summary,
        "data": ev.data,
    }


def record(action: str, summary: str, *, event=None, actor=None, actor_role: str = "", target=None,
           target_type: str = "", target_id: str = "", data: dict | None = None, at=None) -> AuditEvent:
    with transaction.atomic():
        event_id = getattr(event, "pk", event)
        _lock(event_id)
        last = AuditEvent.objects.filter(event_id=event_id).order_by("-seq").only("hash").first()
        prev = last.hash if last else GENESIS
        if target is not None:
            target_type = target_type or target._meta.model_name
            target_id = target_id or str(getattr(target, "ref", "") or target.pk)
        user = getattr(actor, "user", actor)
        ev = AuditEvent(event_id=event_id, ts=at or now(), actor=user if getattr(user, "pk", None) else None,
                        actor_label=str(user) if getattr(user, "pk", None) else (actor_role or "system"),
                        actor_role=actor_role, action=action, target_type=target_type, target_id=target_id,
                        summary=summary[:400], data=data or {}, prev_hash=prev)
        ev.hash = row_digest(prev, _row(ev))
        ev.save(force_insert=True)
        _fanout(ev)
        return ev


def _fanout(ev: AuditEvent):
    """Queue webhook deliveries for this action in the same transaction (outbox)."""
    if not ev.event_id:
        return
    from quorum.integrations.models import WebhookEndpoint

    from quorum.core.models import Outbox

    for hook in WebhookEndpoint.objects.filter(event_id=ev.event_id, active=True):
        if hook.topics and not any(ev.action.startswith(t) for t in hook.topics):
            continue
        Outbox.objects.create(topic="webhook", target=str(hook.pk), payload={
            "id": ev.seq, "action": ev.action, "summary": ev.summary, "ts": ev.ts.isoformat(),
            "target_type": ev.target_type, "target_id": ev.target_id, "data": ev.data,
            "audit_hash": ev.hash, "event": str(ev.event_id),
        })


def head(event) -> tuple[int, str]:
    last = AuditEvent.objects.filter(event_id=getattr(event, "pk", event)).order_by("-seq").first()
    return (last.seq, last.hash) if last else (0, GENESIS)


def verify_chain(event) -> dict:
    """Recompute every hash for an event's chain; report the first broken row."""
    prev = GENESIS
    n = 0
    for ev in AuditEvent.objects.filter(event_id=getattr(event, "pk", event)).order_by("seq").iterator():
        n += 1
        if ev.prev_hash != prev:
            return {"ok": False, "checked": n, "broken_seq": ev.seq, "reason": "prev_hash does not link"}
        if row_digest(prev, _row(ev)) != ev.hash:
            return {"ok": False, "checked": n, "broken_seq": ev.seq, "reason": "row content was modified"}
        prev = ev.hash
    return {"ok": True, "checked": n, "head": prev}


def checkpoint(event, reason: str) -> AuditCheckpoint:
    from quorum.core.crypto import sign

    seq, h = head(event)
    payload = canonical_json({"type": "quorum.audit-checkpoint/v1", "event": str(getattr(event, "pk", event)),
                              "seq": seq, "head": h, "reason": reason, "ts": now().isoformat()})
    key_id, sig = sign(payload)
    return AuditCheckpoint.objects.create(event_id=getattr(event, "pk", event), seq=seq, head=h, reason=reason,
                                          payload=payload, signature=sig, key_id=key_id)
