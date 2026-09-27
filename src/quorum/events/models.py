"""Events, roles, tracks, prizes, teams and submissions (T1)."""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q

from quorum.core.ids import uuid7


class Event(models.Model):
    class Phase(models.TextChoices):
        DRAFT = "draft", "Draft"
        REGISTRATION = "registration", "Registration"
        SUBMISSIONS = "submissions", "Submissions open"
        CLOSED = "closed", "Submissions closed"
        JUDGING = "judging", "Judging"
        FOCUS = "focus", "Focus round"
        DELIBERATION = "deliberation", "Deliberation"
        LOCKED = "locked", "Results locked"
        PUBLISHED = "published", "Results published"
        ARCHIVED = "archived", "Archived"

    class VotingMode(models.TextChoices):
        OFF = "off", "Off"
        OPEN = "open", "Open link"
        EMAIL = "email", "Email-verified"
        AUTHENTICATED = "authenticated", "Signed-in accounts"
        CODE = "code", "One-time codes"

    class RankDisplay(models.TextChoices):
        EXACT = "exact", "Exact rank"
        BAND = "band", "Rank band"
        NONE = "none", "No rank"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    ref = models.SlugField(max_length=40, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=140)
    tagline = models.CharField(max_length=200, blank=True)
    description_md = models.TextField(blank=True)
    rules_md = models.TextField(blank=True)
    is_listed = models.BooleanField(default=True)
    is_demo = models.BooleanField(default=False)

    registration_opens_at = models.DateTimeField()
    submissions_open_at = models.DateTimeField()
    submissions_close_at = models.DateTimeField()
    grace_seconds = models.PositiveIntegerField(default=0)
    judging_opens_at = models.DateTimeField()
    judging_closes_at = models.DateTimeField()
    voting_opens_at = models.DateTimeField(null=True, blank=True)
    voting_closes_at = models.DateTimeField(null=True, blank=True)
    feedback_released_at = models.DateTimeField(null=True, blank=True)
    results_published_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    phase = models.CharField(max_length=16, choices=Phase.choices, default=Phase.DRAFT)
    team_size_max = models.PositiveSmallIntegerField(default=4)
    reviews_per_project = models.PositiveSmallIntegerField(default=3)
    batch_size = models.PositiveSmallIntegerField(default=12)
    min_feedback_chars = models.PositiveSmallIntegerField(default=80)
    focus_budget_pct = models.PositiveSmallIntegerField(default=15)
    prize_positions = models.PositiveSmallIntegerField(default=3)
    voting_mode = models.CharField(max_length=16, choices=VotingMode.choices, default=VotingMode.OFF)
    votes_per_voter = models.PositiveSmallIntegerField(default=3)
    quadratic_voting = models.BooleanField(default=False)
    rank_display = models.CharField(max_length=8, choices=RankDisplay.choices, default=RankDisplay.EXACT)
    auto_approve_feedback = models.BooleanField(default=False)
    options = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        ordering = ["-submissions_close_at"]
        constraints = [
            models.CheckConstraint(condition=Q(submissions_open_at__lte=models.F("submissions_close_at")),
                                   name="event_submission_window_ordered"),
            models.CheckConstraint(condition=Q(judging_opens_at__lte=models.F("judging_closes_at")),
                                   name="event_judging_window_ordered"),
        ]

    def __str__(self):
        return self.name

    @property
    def effective_close(self):
        return self.submissions_close_at + timedelta(seconds=self.grace_seconds)

    def submissions_open(self, at) -> bool:
        return self.submissions_open_at <= at < self.effective_close

    def voting_open(self, at) -> bool:
        return bool(self.voting_mode != "off" and self.voting_opens_at and self.voting_closes_at
                    and self.voting_opens_at <= at < self.voting_closes_at)

    @property
    def published(self) -> bool:
        return self.results_published_at is not None


class EventRole(models.Model):
    class Role(models.TextChoices):
        ORGANIZER = "organizer"
        JUDGE = "judge"
        PARTICIPANT = "participant"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="roles")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="event_roles")
    role = models.CharField(max_length=12, choices=Role.choices)
    ref = models.CharField(max_length=40, blank=True)  # e.g. "jdg_24" for judges (stable external key)
    capacity = models.PositiveSmallIntegerField(null=True, blank=True)  # judges: max reviews
    available = models.BooleanField(default=True)  # judges: organizer can mark unavailable
    last_activity_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "user", "role"], name="uniq_event_user_role"),
            models.UniqueConstraint(fields=["event", "ref"], condition=~Q(ref=""), name="uniq_event_role_ref"),
        ]
        indexes = [models.Index(fields=["event", "role"])]

    def __str__(self):
        return f"{self.user} · {self.role} · {self.event_id}"


class Track(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tracks")
    ref = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    # Comparative judging (spec 9.7): judges also compare pairs from their own reviewed batch.
    # Advisory evidence shown beside the rubric ranking; it never changes the official order.
    pairwise = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "name"]
        constraints = [models.UniqueConstraint(fields=["event", "ref"], name="uniq_track_ref")]

    def __str__(self):
        return self.name


class JudgeTrack(models.Model):
    """Which tracks a judge may see. Track isolation is enforced from this table."""

    event_role = models.ForeignKey(EventRole, on_delete=models.CASCADE, related_name="judge_tracks")
    track = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="judge_links")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["event_role", "track"], name="uniq_judge_track")]


class Prize(models.Model):
    class Kind(models.TextChoices):
        JUDGED = "judged"
        COMMUNITY = "community"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prizes")
    track = models.ForeignKey(Track, null=True, blank=True, on_delete=models.CASCADE, related_name="prizes")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    places = models.PositiveSmallIntegerField(default=1)
    # awarded in every track ("best in track"); a prize with `track` set is for that track only
    per_track = models.BooleanField(default=False)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.JUDGED)
    value_text = models.CharField(max_length=80, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]


class CustomQuestion(models.Model):
    class Kind(models.TextChoices):
        TEXT = "text"
        LONG_TEXT = "long_text"
        URL = "url"
        CHOICE = "choice"
        BOOL = "bool"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questions")
    ref = models.CharField(max_length=40)
    label = models.CharField(max_length=200)
    help = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.TEXT)
    required = models.BooleanField(default=False)
    options = models.JSONField(default=list, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["event", "ref"], name="uniq_question_ref")]


class Team(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="teams")
    ref = models.CharField(max_length=40)
    name = models.CharField(max_length=80)
    invite_code_hash = models.CharField(max_length=64, blank=True)
    invite_expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            # names are NOT unique: the DOGFOOD fixture has two different teams called "OpenSignal"
            models.UniqueConstraint(fields=["event", "ref"], name="uniq_team_ref"),
        ]

    def __str__(self):
        return self.name


class TeamMember(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner"
        MEMBER = "member"

    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="team_memberships")
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # one team per person per event is a database fact, not an if-statement
        constraints = [models.UniqueConstraint(fields=["event", "user"], name="uniq_one_team_per_event")]


class Project(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        SUBMITTED = "submitted"
        WITHDRAWN = "withdrawn"
        DISQUALIFIED = "disqualified"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="projects")
    ref = models.CharField(max_length=40)
    team = models.ForeignKey(Team, on_delete=models.PROTECT, related_name="projects")
    track = models.ForeignKey(Track, null=True, blank=True, on_delete=models.PROTECT, related_name="projects")
    title = models.CharField(max_length=120)
    tagline = models.CharField(max_length=200, blank=True)
    description_md = models.TextField(blank=True)
    description_html = models.TextField(blank=True)
    thumbnail = models.CharField(max_length=200, blank=True)
    demo_video_url = models.URLField(blank=True, max_length=400)
    repo_url = models.URLField(blank=True, max_length=400)
    live_url = models.URLField(blank=True, max_length=400)
    declared_commit_sha = models.CharField(max_length=64, blank=True)
    tech_tags = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates")
    submitted_at = models.DateTimeField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["submitted_at", "title"]
        constraints = [
            models.UniqueConstraint(fields=["event", "ref"], name="uniq_project_ref"),
        ]
        indexes = [models.Index(fields=["event", "status", "track"])]

    def __str__(self):
        return self.title

    @property
    def is_canonical(self):
        return self.duplicate_of_id is None


class ProjectImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="images")
    path = models.CharField(max_length=200)
    position = models.PositiveSmallIntegerField(default=0)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ["position"]


class ProjectAnswer(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="answers")
    question = models.ForeignKey(CustomQuestion, on_delete=models.CASCADE, related_name="answers")
    value = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "question"], name="uniq_answer")]


class ProjectRevision(models.Model):
    """Every save of a submission. Proves what the judges saw and that nothing changed
    after the deadline."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="revisions")
    version = models.PositiveIntegerField()
    snapshot = models.JSONField()
    content_hash = models.CharField(max_length=64)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-version"]
        constraints = [models.UniqueConstraint(fields=["project", "version"], name="uniq_revision")]


class EligibilityItem(models.Model):
    class Kind(models.TextChoices):
        DUPLICATE = "duplicate"
        MISSING_FIELD = "missing_field"
        LATE_ATTEMPT = "late_attempt"
        TEAM_SIZE = "team_size"
        CONFLICT = "conflict"

    class Status(models.TextChoices):
        OPEN = "open"
        ACCEPTED = "accepted"
        REJECTED = "rejected"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="eligibility_items")
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="eligibility_items")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    details = models.JSONField(default=dict)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    resolution_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class Comment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="comments")
    body = models.TextField()
    body_html = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    hidden_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at"]
