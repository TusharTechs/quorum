"""Community voting (T3). Identity strength depends on the access mode; the anti-abuse
design is: hard-block what is unambiguous, flag what is statistical, let a human decide,
and never delete a vote (voided votes keep their reason)."""

from django.conf import settings
from django.db import models

from quorum.core.ids import uuid7
from quorum.events.models import Event, Project


class Voter(models.Model):
    class Kind(models.TextChoices):
        OPEN = "open"
        EMAIL = "email"
        USER = "user"
        CODE = "code"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="voters")
    kind = models.CharField(max_length=6, choices=Kind.choices)
    voter_key = models.CharField(max_length=64)  # HMAC of the identity; never the raw email
    email_domain = models.CharField(max_length=120, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    credits_spent = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "voter_key"], name="uniq_voter")]


class VoteCode(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="vote_codes")
    code_hash = models.CharField(max_length=64, unique=True)
    batch = models.CharField(max_length=40, blank=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    redeemed_by = models.OneToOneField(Voter, null=True, blank=True, on_delete=models.SET_NULL, related_name="code")


class Vote(models.Model):
    class Status(models.TextChoices):
        VALID = "valid"
        VOID = "void"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="votes")
    voter = models.ForeignKey(Voter, on_delete=models.CASCADE, related_name="votes")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="votes")
    category = models.CharField(max_length=40, default="overall")
    weight = models.PositiveSmallIntegerField(default=1)  # quadratic mode: votes bought
    ballot_position = models.PositiveSmallIntegerField(null=True, blank=True)
    net_key = models.CharField(max_length=40, blank=True)
    ua_key = models.CharField(max_length=40, blank=True)
    ballot_issued_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=5, choices=Status.choices, default=Status.VALID)
    void_reason = models.CharField(max_length=300, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["voter", "project", "category"], name="uniq_vote")]
        indexes = [models.Index(fields=["event", "project", "created_at"])]


class IntegrityFlag(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        KEPT = "kept"
        VOIDED = "voided"
        DISMISSED = "dismissed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="flags")
    rule = models.CharField(max_length=40)
    severity = models.CharField(max_length=8, default="medium")
    subject_type = models.CharField(max_length=20)  # "project" | "voter" | "judge"
    subject_id = models.CharField(max_length=64)
    summary = models.CharField(max_length=300)
    evidence = models.JSONField(default=dict)
    vote_ids = models.JSONField(default=list, blank=True)
    fingerprint = models.CharField(max_length=64)  # dedupe: same rule + subject + window
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    resolution_note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["event", "fingerprint"], name="uniq_flag_fingerprint")]
        indexes = [models.Index(fields=["event", "status"])]
