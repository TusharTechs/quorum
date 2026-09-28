"""Scoped repositories: the ONLY way application code reads protected rows.

A lint test (tests/api/test_scoped_queries.py) fails the build if Review, ReviewScore,
PairwiseComparison, Vote or RankingRun are queried outside this module, the engine input
builder, exports, or management commands. Each function starts from what the actor is
allowed to see and narrows from there; nothing starts from `.objects.all()` and filters out.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from quorum.events.models import Event, EventRole, JudgeTrack, Project, TeamMember
from quorum.judging.models import Assignment, Conflict, PairwiseComparison, Review

from .actor import Actor
from .errors import Forbidden


def public_projects(event: Event | None = None) -> QuerySet:
    """What a stranger may see: submitted, canonical, not disqualified, in listed events.
    Drafts never appear here, so the gallery cannot leak unfinished work."""
    qs = (Project.objects.filter(status=Project.Status.SUBMITTED, duplicate_of__isnull=True,
                                 event__is_listed=True)
          .select_related("team", "track", "event"))
    if event is not None:
        qs = qs.filter(event=event)
    return qs


def team_projects(actor: Actor, event: Event | None = None) -> QuerySet:
    if not actor.authenticated:
        return Project.objects.none()
    teams = TeamMember.objects.filter(user=actor.user).values("team_id")
    qs = Project.objects.filter(team_id__in=teams).select_related("team", "track", "event")
    return qs.filter(event=event) if event is not None else qs


def can_view_project(actor: Actor, project: Project) -> bool:
    if project.status == Project.Status.SUBMITTED and project.event.is_listed:
        return True
    if not actor.authenticated:
        return False
    if actor.is_organizer(project.event):
        return True
    return TeamMember.objects.filter(team_id=project.team_id, user=actor.user).exists()


def judge_tracks(role: EventRole) -> set:
    return set(JudgeTrack.objects.filter(event_role=role).values_list("track_id", flat=True))


def judge_visible_projects(actor: Actor, event: Event) -> QuerySet:
    """Projects a judge may evaluate: their assignments, restricted to their tracks,
    minus confirmed conflicts. A judge never sees another track's judging data."""
    role = actor.judge_role(event)
    if role is None:
        return Project.objects.none()
    conflicted = Conflict.objects.filter(judge_role=role, status=Conflict.Status.CONFIRMED).values("team_id")
    assigned = Assignment.objects.filter(judge_role=role).exclude(status=Assignment.Status.REASSIGNED).values("project_id")
    qs = Project.objects.filter(event=event, pk__in=assigned).exclude(team_id__in=conflicted)
    tracks = judge_tracks(role)
    if tracks:
        qs = qs.filter(Q(track_id__in=tracks) | Q(assignments__judge_role=role, assignments__source__in=["focus", "manual"]))
    return qs.distinct().select_related("team", "track")


def visible_reviews(actor: Actor, event: Event | None = None) -> QuerySet:
    """Organizers/admins: every review of their events. Judges: only their own.
    Everyone else: nothing."""
    qs = Review.objects.select_related("judge_role", "project", "event", "judge_role__user")
    if not actor.authenticated:
        return qs.none()
    if actor.is_admin:
        return qs.filter(event=event) if event is not None else qs
    organizer_events = EventRole.objects.filter(user=actor.user, role="organizer").values("event_id")
    mine = EventRole.objects.filter(user=actor.user, role="judge").values("pk")
    cond = Q(event_id__in=organizer_events) | Q(judge_role_id__in=mine)
    qs = qs.filter(cond)
    return qs.filter(event=event) if event is not None else qs


def own_reviews(actor: Actor, event: Event | None = None) -> QuerySet:
    if not actor.authenticated:
        return Review.objects.none()
    mine = EventRole.objects.filter(user=actor.user, role="judge").values("pk")
    qs = Review.objects.filter(judge_role_id__in=mine).select_related("judge_role", "project", "event")
    return qs.filter(event=event) if event is not None else qs


def reviews_of_judge(actor: Actor, event: Event, judge_role: EventRole) -> QuerySet:
    """The peer-isolation rule: a judge may read their own scores; an organizer may read
    anyone's; nobody else may read anything. Uniform 403, whether or not the judge exists."""
    actor.require_auth()
    if actor.is_organizer(event):
        return Review.objects.filter(event=event, judge_role=judge_role).select_related("project", "event", "judge_role")
    own = actor.judge_role(event)
    if own is not None and own.pk == judge_role.pk:
        return Review.objects.filter(event=event, judge_role=judge_role).select_related("project", "event", "judge_role")
    raise Forbidden("You may not read these scores.")  # word for word the unknown-judge refusal


def judge_assignment(actor: Actor, assignment_id) -> Assignment:
    """An assignment the actor may review (their own, not reassigned, not conflicted)."""
    actor.require_auth()
    a = (Assignment.objects.select_related("project", "project__track", "project__team", "event", "judge_role")
         .filter(pk=assignment_id, judge_role__user=actor.user).exclude(status=Assignment.Status.REASSIGNED).first())
    if a is None:
        raise Forbidden("This review is not assigned to you.")
    if Conflict.objects.filter(judge_role=a.judge_role, team=a.project.team, status="confirmed").exists():
        raise Forbidden("You declared a conflict with this team.")
    return a


def own_comparisons(actor: Actor, event: Event) -> QuerySet:
    role = actor.judge_role(event)
    if role is None:
        return PairwiseComparison.objects.none()
    return PairwiseComparison.objects.filter(event=event, judge_role=role)
