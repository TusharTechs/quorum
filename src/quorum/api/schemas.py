
from datetime import datetime
from typing import Any

from ninja import Schema


class Error(Schema):
    error: str
    detail: str


class ProjectIn(Schema):
    title: str | None = None
    tagline: str | None = None
    summary: str | None = None
    description_md: str | None = None
    demo_video_url: str | None = None
    repo_url: str | None = None
    live_url: str | None = None
    declared_commit_sha: str | None = None
    tech_tags: list[str] | None = None
    track: str | None = None
    answers: dict[str, Any] | None = None
    submit: bool = False


class ProjectPublic(Schema):
    id: str
    ref: str
    event: str
    title: str
    tagline: str
    track: str | None
    team: str
    description_html: str
    repo_url: str
    demo_video_url: str
    live_url: str
    tech_tags: list[str]
    thumbnail_url: str | None
    submitted_at: datetime | None


class ProjectTeamView(ProjectPublic):
    status: str
    description_md: str
    declared_commit_sha: str
    version: int
    content_hash: str
    answers: dict[str, Any]


class ScoreOut(Schema):
    review_id: str
    event: str
    project: str
    project_title: str
    judge: str
    status: str
    criteria: dict[str, float]
    weighted_total: float | None
    feedback_to_team: str
    submitted_at: datetime | None
