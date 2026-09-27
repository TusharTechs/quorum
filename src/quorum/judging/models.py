"""Rubric, method pre-registration, assignment, reviews and pairwise comparisons (T2)."""

from django.conf import settings
from django.db import models
from django.db.models import Q

from quorum.core.ids import uuid7
from quorum.events.models import Event, EventRole, Project, Team


class Criterion(models.Model):
    """A rubric criterion. Weights are integer basis points (sum 10000) so there is no
    floating-point drift between what the organizer published and what the engine used."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="criteria")
    key = models.SlugField(max_length=40)
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True)
    weight_bp = models.PositiveIntegerField()
    scale_min = models.PositiveSmallIntegerField(default=1)
    scale_max = models.PositiveSmallIntegerField(default=5)
    anchors = models.JSONField(default=dict, blank=True)  # {"1": "...", "3": "...", "5": "..."}
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "key"]
        constraints = [
            models.UniqueConstraint(fields=["event", "key"], name="uniq_criterion_key"),
            models.CheckConstraint(condition=Q(weight_bp__gt=0), name="criterion_weight_positive"),
            models.CheckConstraint(condition=Q(scale_min__lt=models.F("scale_max")), name="criterion_scale_ordered"),
        ]

    def __str__(self):
        return self.name

    @property
    def weight_pct(self):
        return self.weight_bp / 100


class JudgingMethod(models.Model):
    """Pre-registration. The complete judging method (criteria, weights, calibration,
    tie policy, focus budget, voting rule) is serialised to canonical JSON and hashed
    when registration opens. The hash is public; changing the method afterwards needs an
    admin override with a reason, and the results page says so forever."""

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="methods")
    version = models.PositiveIntegerField()
    spec = models.JSONField()
    spec_hash = models.CharField(max_length=64)
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    override_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [models.UniqueConstraint(fields=["event", "version"], name="uniq_method_version")]


class Conflict(models.Model):
    class Source(models.TextChoices):
        DECLARED = "declared"
        SAME_EMAIL = "same_email"
        SAME_DOMAIN = "same_domain"
        ORGANIZER = "organizer"

    class Status(models.TextChoices):
        SUGGESTED = "suggested"
        CONFIRMED = "confirmed"
        DISMISSED = "dismissed"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="conflicts")
    judge_role = models.ForeignKey(EventRole, on_delete=models.CASCADE, related_name="conflicts")
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="conflicts")
    source = models.CharField(max_length=12, choices=Source.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUGGESTED)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["judge_role", "team"], name="uniq_conflict")]


class JudgeBatch(models.Model):
    class Kind(models.TextChoices):
        BASELINE = "baseline"
        FOCUS = "focus"
        TIEBREAK = "tiebreak"
        REBALANCE = "rebalance"
        IMPORT = "import"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="batches")
    judge_role = models.ForeignKey(EventRole, on_delete=models.CASCADE, related_name="batches")
    round = models.PositiveSmallIntegerField(default=1)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BASELINE)
    sent_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Assignment(models.Model):
    """Every score has a reason to exist: review -> assignment -> batch -> (strategy, seed)."""

    class Status(models.TextChoices):
        PENDING = "pending"
        IN_PROGRESS = "in_progress"
        SUBMITTED = "submitted"
        RECUSED = "recused"
        REASSIGNED = "reassigned"

    class Source(models.TextChoices):
        ALGORITHM = "algorithm"
        MANUAL = "manual"
        IMPORT = "import"
        FOCUS = "focus"
        REBALANCE = "rebalance"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="assignments")
    batch = models.ForeignKey(JudgeBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="assignments")
    judge_role = models.ForeignKey(EventRole, on_delete=models.CASCADE, related_name="assignments")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="assignments")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.ALGORITHM)
    strategy = models.CharField(max_length=40, blank=True)
    seed = models.BigIntegerField(null=True, blank=True)
    assigned_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField(null=True, blank=True)
    reassigned_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["judge_role", "project"], condition=~Q(status="reassigned"),
                                    name="uniq_live_assignment"),
        ]
        indexes = [models.Index(fields=["judge_role", "status"]), models.Index(fields=["project", "status"])]


class Review(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"

    class Quotable(models.TextChoices):
        NO = "no", "Private"
        ANONYMOUS = "anonymous", "Quotable anonymously"
        NAMED = "named", "Quotable with my name"

    class Moderation(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        EDITED = "edited"
        HIDDEN = "hidden"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    assignment = models.OneToOneField(Assignment, on_delete=models.CASCADE, related_name="review")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="reviews")
    judge_role = models.ForeignKey(EventRole, on_delete=models.CASCADE, related_name="reviews")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="reviews")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    feedback_to_team = models.TextField(blank=True)
    note_to_organizers = models.TextField(blank=True)
    quotable = models.CharField(max_length=10, choices=Quotable.choices, default=Quotable.NO)
    moderation = models.CharField(max_length=10, choices=Moderation.choices, default=Moderation.PENDING)
    moderated_text = models.TextField(blank=True)
    moderation_note = models.CharField(max_length=300, blank=True)
    moderated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    active_seconds = models.PositiveIntegerField(default=0)
    source_scores = models.JSONField(default=list, blank=True)  # raw rows merged into this review (duplicates)

    class Meta:
        indexes = [models.Index(fields=["event", "status"]), models.Index(fields=["judge_role", "status"])]

    @property
    def feedback_for_team(self):
        if self.moderation == self.Moderation.HIDDEN:
            return ""
        return self.moderated_text if self.moderation == self.Moderation.EDITED else self.feedback_to_team


class ReviewScore(models.Model):
    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="scores")
    criterion = models.ForeignKey(Criterion, on_delete=models.PROTECT, related_name="scores")
    value = models.DecimalField(max_digits=4, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["review", "criterion"], name="uniq_review_criterion")]


class PairwiseComparison(models.Model):
    class Outcome(models.TextChoices):
        A = "a"
        B = "b"
        TIE = "tie"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="comparisons")
    tiebreak = models.ForeignKey("results.TiebreakRound", null=True, blank=True, on_delete=models.CASCADE,
                                 related_name="comparisons")
    judge_role = models.ForeignKey(EventRole, on_delete=models.CASCADE, related_name="comparisons")
    project_a = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    project_b = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="+")
    outcome = models.CharField(max_length=3, choices=Outcome.choices)
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    active_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(project_a=models.F("project_b")), name="pairwise_distinct"),
            models.UniqueConstraint(fields=["judge_role", "tiebreak", "project_a", "project_b"],
                                    name="uniq_pairwise_per_judge"),
        ]


class Reminder(models.Model):
    class Kind(models.TextChoices):
        BATCH_SENT = "batch_sent", "Batch sent"
        MIDPOINT = "midpoint", "Midpoint nudge"
        DUE_24H = "due_24h", "24 hours left"
        OVERDUE = "overdue", "Overdue"
        CUSTOM = "custom", "Custom"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="reminders")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    send_at = models.DateTimeField()
    sent_at = models.DateTimeField(null=True, blank=True)
    only_incomplete = models.BooleanField(default=True)
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["send_at"]


class ReminderDelivery(models.Model):
    """Idempotency: a reminder is delivered to a judge at most once."""

    reminder = models.ForeignKey(Reminder, on_delete=models.CASCADE, related_name="deliveries")
    judge_role = models.ForeignKey(EventRole, on_delete=models.CASCADE, related_name="+")
    delivered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["reminder", "judge_role"], name="uniq_reminder_delivery")]
