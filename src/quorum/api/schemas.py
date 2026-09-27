
from datetime import datetime
from typing import Any, Optional

from ninja import Schema


class Error(Schema):
    error: str
    detail: str


class ProjectIn(Schema):
    title: Optional[str] = None
    tagline: Optional[str] = None
    summary: Optional[str] = None
    description_md: Optional[str] = None
    demo_video_url: Optional[str] = None
    repo_url: Optional[str] = None
    live_url: Optional[str] = None
    declared_commit_sha: Optional[str] = None
    tech_tags: Optional[list[str]] = None
    track: Optional[str] = None
    answers: Optional[dict[str, Any]] = None
    submit: bool = False


class ProjectPublic(Schema):
    id: str
    ref: str
    event: str
    title: str
    tagline: str
    track: Optional[str]
    team: str
    description_html: str
    repo_url: str
    demo_video_url: str
    live_url: str
    tech_tags: list[str]
    thumbnail_url: Optional[str]
    submitted_at: Optional[datetime]


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
    weighted_total: Optional[float]
    feedback_to_team: str
    submitted_at: Optional[datetime]
