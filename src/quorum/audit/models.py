"""Tamper-evident audit trail.

* append-only: a database trigger rejects UPDATE and DELETE on audit_auditevent
* hash chain: every row stores sha256(prev_hash || canonical(row)), per event
* checkpoints: the chain head is signed (Ed25519) at every phase change and on publish

What this proves: once a head hash has left the server (results page, export, e-mail),
any later edit or deletion of earlier rows is detectable by anyone. What it does not
prove: that the operator did not rewrite the chain before any head escaped, or that the
scores judges typed were honest. THREAT-MODEL.md says so plainly.
"""

from django.conf import settings
from django.db import models

from quorum.core.ids import uuid7
from quorum.events.models import Event


class AuditEvent(models.Model):
    seq = models.BigAutoField(primary_key=True)
    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.DO_NOTHING, db_constraint=False,
                              related_name="+")
    ts = models.DateTimeField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.DO_NOTHING,
                              db_constraint=False, related_name="+")
    actor_label = models.CharField(max_length=160, blank=True)
    actor_role = models.CharField(max_length=20, blank=True)
    action = models.CharField(max_length=48)
    target_type = models.CharField(max_length=40, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    summary = models.CharField(max_length=400)
    data = models.JSONField(default=dict)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["seq"]
        indexes = [models.Index(fields=["event", "seq"]), models.Index(fields=["event", "action"])]


class AuditCheckpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    seq = models.BigIntegerField()
    head = models.CharField(max_length=64)
    reason = models.CharField(max_length=80)
    payload = models.TextField()
    signature = models.CharField(max_length=128)
    key_id = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)


class Certificate(models.Model):
    """Signed, publicly verifiable records: participation, winners, and the numbered
    judge evaluation protocol Hackathon Raptors already issues by hand. Never contains scores."""

    class Kind(models.TextChoices):
        PARTICIPANT = "participant"
        WINNER = "winner"
        JUDGE_PROTOCOL = "judge_protocol"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="certificates")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="certificates")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    serial = models.CharField(max_length=40)
    payload = models.TextField()  # canonical JSON, exactly the bytes that were signed
    signature = models.CharField(max_length=128)
    key_id = models.CharField(max_length=32)
    issued_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=300, blank=True)  # public: shown on the record and the list

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "kind", "serial"], name="uniq_certificate_serial"),
            models.UniqueConstraint(fields=["event", "kind", "user"], name="uniq_certificate_per_user"),
        ]
