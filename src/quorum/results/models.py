"""Ranking runs (immutable, content-addressed), focus rounds, tie-breaks, publication."""

from django.conf import settings
from django.db import models

from quorum.core.ids import uuid7
from quorum.events.models import Event, Project
from quorum.judging.models import Review


class RankingRun(models.Model):
    """An immutable, content-addressed computation. `input` is the exact canonical JSON
    handed to the engine; `output_hash` lets anyone holding an export recompute it."""

    class Kind(models.TextChoices):
        PREVIEW = "preview"
        OFFICIAL = "official"
        TIEBREAK = "tiebreak"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="runs")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PREVIEW)
    method_hash = models.CharField(max_length=64, blank=True)
    engine_version = models.CharField(max_length=40)
    input = models.JSONField()
    input_hash = models.CharField(max_length=64, db_index=True)
    output = models.JSONField()
    output_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class FocusRound(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="focus_rounds")
    run = models.ForeignKey(RankingRun, on_delete=models.PROTECT, related_name="+")
    budget = models.PositiveSmallIntegerField()
    plan = models.JSONField()
    committed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)


class TiebreakRound(models.Model):
    class Status(models.TextChoices):
        OPEN = "open"
        RESOLVED = "resolved"
        ESCALATED = "escalated"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tiebreaks")
    run = models.ForeignKey(RankingRun, on_delete=models.PROTECT, related_name="+")
    projects = models.ManyToManyField(Project, related_name="+")
    judge_roles = models.ManyToManyField("events.EventRole", related_name="tiebreaks")
    boundary = models.PositiveSmallIntegerField(default=1)  # prize position being decided
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    result = models.JSONField(null=True, blank=True)
    decision_note = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class ResultPublication(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.OneToOneField(Event, on_delete=models.CASCADE, related_name="publication")
    run = models.ForeignKey(RankingRun, on_delete=models.PROTECT, related_name="+")
    locked_at = models.DateTimeField()
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    audit_seq = models.BigIntegerField(null=True, blank=True)
    audit_head = models.CharField(max_length=64, blank=True)
    checkpoint_signature = models.TextField(blank=True)
    tiebreak_results = models.JSONField(default=list, blank=True)
    final_order = models.JSONField(default=list, blank=True)  # project ids after tie-breaks


class FeaturedQuote(models.Model):
    class Attribution(models.TextChoices):
        ANONYMOUS = "anonymous"
        NAMED = "named"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="featured_quotes")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="featured_quotes")
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="+")
    text = models.TextField()
    attribution = models.CharField(max_length=10, choices=Attribution.choices, default=Attribution.ANONYMOUS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event", "project"], name="uniq_featured_quote")]
