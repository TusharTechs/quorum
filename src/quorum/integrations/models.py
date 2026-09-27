from django.conf import settings
from django.db import models

from quorum.core.ids import uuid7
from quorum.events.models import Event


class ImportJob(models.Model):
    class Source(models.TextChoices):
        FIXTURES = "fixtures", "DOGFOOD fixtures JSON"
        BUNDLE = "bundle", "Quorum bundle JSON"
        DEVPOST = "devpost_csv", "Devpost CSV export"
        UNSTOP = "unstop_csv", "Unstop CSV export"
        GENERIC = "generic_csv", "Any CSV (map columns)"

    class Status(models.TextChoices):
        DRY_RUN = "dry_run"
        COMMITTED = "committed"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, null=True, blank=True, on_delete=models.CASCADE, related_name="imports")
    source = models.CharField(max_length=12, choices=Source.choices)
    filename = models.CharField(max_length=200)
    file_sha256 = models.CharField(max_length=64)
    content = models.TextField()
    mapping = models.JSONField(default=dict, blank=True)
    report = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRY_RUN)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    committed_at = models.DateTimeField(null=True, blank=True)


class WebhookEndpoint(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="webhooks")
    url = models.URLField(max_length=400)
    secret = models.CharField(max_length=64)
    topics = models.JSONField(default=list)  # audit action prefixes, e.g. ["PROJECT_", "RESULTS_"]; [] = all
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    last_status = models.CharField(max_length=80, blank=True)
