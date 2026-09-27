import uuid

from ninja import Router

from quorum.audit import certificates
from quorum.audit import service as audit
from quorum.audit.models import AuditCheckpoint, AuditEvent, Certificate
from quorum.policy.decorators import policy
from quorum.policy.errors import NotFound

from .common import get_event

router = Router(tags=["audit & records"])


def _cert(cid: str) -> Certificate:
    try:
        c = Certificate.objects.filter(pk=uuid.UUID(cid)).select_related("event").first()
    except ValueError:
        c = None
    if not c:
        raise NotFound("No such certificate.")
    return c


@router.get("/events/{e}/audit", summary="Audit trail (organizers). Filter by action prefix, paginate with before=")
@policy("authenticated")
def audit_list(request, e: str, action: str = "", before: int = None, limit: int = 100):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    qs = AuditEvent.objects.filter(event_id=ev.pk).order_by("-seq")
    if action:
        qs = qs.filter(action__startswith=action)
    if before:
        qs = qs.filter(seq__lt=before)
    return [{"seq": a.seq, "ts": a.ts, "actor": a.actor_label, "role": a.actor_role, "action": a.action,
             "target": f"{a.target_type}:{a.target_id}", "summary": a.summary, "data": a.data, "prev_hash": a.prev_hash,
             "hash": a.hash} for a in qs[: min(limit, 500)]]


@router.get("/events/{e}/audit/verify", summary="Recompute the hash chain; report the first broken entry")
@policy("authenticated")
def audit_verify(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return audit.verify_chain(ev)


@router.get("/events/{e}/audit/checkpoints", summary="Signed checkpoints of the chain head (public)")
@policy("public")
def checkpoints(request, e: str):
    ev = get_event(e)
    return [{"seq": c.seq, "head": c.head, "reason": c.reason, "payload": c.payload, "signature": c.signature,
             "key_id": c.key_id} for c in AuditCheckpoint.objects.filter(event_id=ev.pk).order_by("seq")]


@router.get("/me/certificates", summary="My certificates and evaluation protocols")
@policy("authenticated")
def my_certs(request):
    return [{"id": str(c.pk), "kind": c.kind, "serial": c.serial, "event": c.event.ref, "payload": c.payload,
             "signature": c.signature, "key_id": c.key_id} for c in Certificate.objects.filter(user=request.user).select_related("event")]


@router.get("/certificates/{cid}", summary="A signed record (public)")
@policy("public")
def cert(request, cid: str):
    c = _cert(cid)
    return {"serial": c.serial, "kind": c.kind, "payload": c.payload, "signature": c.signature, "key_id": c.key_id,
            "revoked": bool(c.revoked_at), "revoked_at": c.revoked_at, "revoked_reason": c.revoked_reason}


@router.get("/events/{e}/certificates", summary="Every signed record issued for an event (organizers)")
@policy("authenticated")
def event_certs(request, e: str):
    ev = get_event(e)
    request.actor.require_organizer(ev)
    return [{"id": str(c.pk), "serial": c.serial, "kind": c.kind, "holder": c.user.name or c.user.email,
             "issued_at": c.issued_at, "key_id": c.key_id, "revoked_at": c.revoked_at, "revoked_reason": c.revoked_reason}
            for c in Certificate.objects.filter(event=ev).select_related("user").order_by("kind", "serial")]


from ninja import Schema  # noqa: E402


class RecordIn(Schema):
    payload: str
    signature: str
    key_id: str


class RevokeIn(Schema):
    reason: str


@router.post("/certificates/{cid}/revoke", summary="Revoke a signed record with a public reason (organizers; final)")
@policy("authenticated")
def revoke(request, cid: str, payload: RevokeIn):
    c = _cert(cid)
    request.actor.require_organizer(c.event)
    c = certificates.revoke(c.event, request.actor, c, payload.reason)
    return {"serial": c.serial, "revoked_at": c.revoked_at, "revoked_reason": c.revoked_reason}


@router.post("/verify", summary="Verify an Ed25519-signed record against the published key")
@policy("public")
def verify(request, payload: RecordIn):
    return certificates.check(payload.payload, payload.signature, payload.key_id)
