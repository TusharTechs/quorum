"""Signed, publicly verifiable records (DOGFOOD T4).

Hackathon Raptors already gives every judge "a signed evaluation protocol" and "a numbered
certificate of judging", documenting "work that was completed, not an appointment that was
accepted". Quorum generates exactly that from the data: numbered, Ed25519-signed, anchored
to an audit checkpoint, and verifiable offline by anyone with the public key. Records never
contain scores.

Honest scope: a signature proves the platform instance's key-holder attested the record
at that checkpoint. It does not prove the organizer is honest; the hash chain and the
public head hash are what make later tampering detectable.
"""

from __future__ import annotations

from collections import defaultdict

from django.db import transaction

from engine.bundle import canonical_json
from quorum.core.clock import now
from quorum.core.crypto import sign, verify
from quorum.events.models import EventRole, Project, TeamMember
from quorum.judging.models import Assignment, PairwiseComparison

from . import service as audit
from .models import Certificate


def _serial(event, kind: str) -> str:
    prefix = {"judge_protocol": "J", "participant": "P", "winner": "W"}[kind]
    n = Certificate.objects.filter(event=event, kind=kind).count() + 1
    return f"{event.ref.upper()}-{prefix}-{n:04d}"


def _issue(event, user, kind: str, body: dict) -> Certificate:
    existing = Certificate.objects.filter(event=event, kind=kind, user=user).first()
    if existing:
        return existing
    serial = _serial(event, kind)
    seq, head = audit.head(event)
    payload = canonical_json({
        "type": f"quorum.{kind.replace('_', '-')}/v1", "serial": serial, "event": {"id": event.ref, "name": event.name},
        "holder": {"name": user.name or user.email.split("@")[0]}, **body,
        "audit_checkpoint": {"seq": seq, "head": head}, "issued_at": now().isoformat(),
    })
    key_id, sig = sign(payload)
    c = Certificate.objects.create(event=event, user=user, kind=kind, serial=serial, payload=payload, signature=sig,
                                   key_id=key_id)
    audit.record("CERTIFICATE_ISSUED", f"{kind.replace('_', ' ')} {serial} issued to {user}", event=event,
                 actor_role="system", target=c, data={"serial": serial, "kind": kind})
    return c


@transaction.atomic
def issue_judge_protocol(event, role: EventRole) -> Certificate | None:
    done = Assignment.objects.filter(event=event, judge_role=role, status="submitted").select_related("project__track")
    n = done.count()
    comps = PairwiseComparison.objects.filter(event=event, judge_role=role).count()
    if n == 0 and comps == 0:
        return None
    tracks = sorted({a.project.track.name for a in done if a.project.track})
    return _issue(event, role.user, "judge_protocol", {
        "role": "judge", "reviews_completed": n, "pairwise_comparisons": comps, "tracks": tracks,
        "period": {"from": event.judging_opens_at.date().isoformat(), "to": event.judging_closes_at.date().isoformat()},
        "statement": "Documents evaluation work completed on this platform. Contains no scores.",
    })


@transaction.atomic
def issue_all(event) -> dict:
    counts = defaultdict(int)
    for role in EventRole.objects.filter(event=event, role="judge").select_related("user"):
        if issue_judge_protocol(event, role):
            counts["judge_protocol"] += 1
    pub = getattr(event, "publication", None)
    winners = set((pub.final_order or [])[: event.prize_positions]) if pub else set()
    for p in Project.objects.filter(event=event, status="submitted", duplicate_of__isnull=True).select_related("team"):
        for m in TeamMember.objects.filter(team=p.team).select_related("user"):
            _issue(event, m.user, "participant", {"role": "participant", "team": p.team.name, "project": p.title})
            counts["participant"] += 1
            if p.ref in winners:
                place = pub.final_order.index(p.ref) + 1
                _issue(event, m.user, "winner", {"role": "winner", "team": p.team.name, "project": p.title,
                                                 "place": place})
                counts["winner"] += 1
    return dict(counts)


def check(payload: str, signature: str, key_id: str) -> dict:
    ok = verify(payload, signature, key_id)
    cert = Certificate.objects.filter(signature=signature).first()
    return {"valid": ok, "known": bool(cert), "revoked": bool(cert and cert.revoked_at),
            "serial": cert.serial if cert else None}
