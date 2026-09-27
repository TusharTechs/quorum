from django.db import models

from .ids import uuid7


class RateBucket(models.Model):
    """Fixed-window counters for rate limiting without Redis. Incremented atomically with
    INSERT ... ON CONFLICT DO UPDATE, so limits hold across gunicorn workers."""

    key = models.CharField(max_length=200)
    window_start = models.DateTimeField()
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "window_start"], name="uniq_rate_bucket")]
        indexes = [models.Index(fields=["window_start"])]


class Outbox(models.Model):
    """Transactional outbox: side effects (email, webhooks) are queued in the same database
    transaction as the change that caused them, then delivered by the worker."""

    class Status(models.TextChoices):
        PENDING = "pending"
        SENT = "sent"
        FAILED = "failed"
        DEAD = "dead"

    id = models.BigAutoField(primary_key=True)
    topic = models.CharField(max_length=64)  # "email" | "webhook"
    payload = models.JSONField()
    target = models.CharField(max_length=300, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(auto_now_add=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "next_attempt_at"])]


class SigningKey(models.Model):
    """Public half of an Ed25519 key. The private key lives in QUORUM_DATA_DIR/keys (0600),
    never in the database, so a database dump cannot forge records."""

    key_id = models.CharField(max_length=32, primary_key=True)
    public_key_b64 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True, blank=True)


class Job(models.Model):
    """Background job queue on Postgres (SELECT ... FOR UPDATE SKIP LOCKED)."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    kind = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, default="pending")
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "created_at"])]
