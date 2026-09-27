"""The deadline holds: in the app (refused for the RIGHT reason), and in the database."""

import json

import pytest
from django.db import DatabaseError, transaction

LATE = {"title": "dogfood-late-submission-probe", "summary": "probe"}


def test_checker_probe_is_refused_because_of_the_deadline(client_as):
    r = client_as("participant").post("/api/v1/events/evt_01/projects", data=json.dumps(LATE),
                                      content_type="application/json")
    body = json.loads(r.content)
    assert r.status_code == 403 and body["error"] == "deadline_passed" and body["closed_at"] == "2026-03-01T18:00:00Z"


def test_late_edit_submit_and_upload_are_refused(client_as, db):
    from quorum.events.models import Project

    pid = Project.objects.get(ref="prj_01").pk
    c = client_as("participant")
    for method, url, data in [("patch", f"/api/v1/projects/{pid}", {"title": "changed"}),
                              ("post", f"/api/v1/projects/{pid}/submit", {}),
                              ("post", f"/api/v1/projects/{pid}/withdraw", {})]:
        r = getattr(c, method)(url, data=json.dumps(data), content_type="application/json")
        assert r.status_code == 403 and json.loads(r.content)["error"] == "deadline_passed", url
    assert Project.objects.get(pk=pid).title == "Glass Signal"


def test_database_trigger_refuses_late_content_change_even_bypassing_the_app(db):
    from quorum.events.models import Project

    with pytest.raises(DatabaseError, match="deadline_passed"):
        with transaction.atomic():
            Project.objects.filter(ref="prj_01").update(title="shell edit after the deadline")


def test_status_change_after_deadline_is_allowed_for_organizers(db):
    from quorum.events.models import Project

    with transaction.atomic():
        Project.objects.filter(ref="prj_02").update(status="disqualified")
    assert Project.objects.get(ref="prj_02").status == "disqualified"


def test_open_event_flow_and_grace_window(db, actor):
    from datetime import timedelta

    from django.utils import timezone

    from quorum.accounts.models import User
    from quorum.events import services as ev
    from quorum.events.models import Event
    from quorum.policy.errors import DeadlinePassed

    live = Event.objects.get(ref="live-demo")
    User.objects.create_user("flow@example.test", "pw-flow-12345")
    a = actor("flow@example.test")
    team, code = ev.create_team(a, live, "Flow team")
    p = ev.create_project(a, live, {"title": "Flow", "tagline": "t", "description_md": "d",
                                    "repo_url": "https://example.org/r", "track": "trk_01",
                                    "status": "submitted", "submitted_at": "2020-01-01T00:00:00Z"})
    assert p.status == "draft" and p.submitted_at is None  # server-set fields are never taken from input
    ev.submit_project(a, p)
    live.submissions_close_at = timezone.now() - timedelta(minutes=10)
    live.grace_seconds = 3600
    live.save()
    ev.update_project(a, p, {"tagline": "inside the grace window"})
    live.grace_seconds = 0
    live.save()
    with pytest.raises(DeadlinePassed):
        ev.update_project(a, p, {"tagline": "too late"})


def test_one_team_per_person_is_a_database_fact(db):
    from django.db import IntegrityError

    from quorum.events.models import Event, Team, TeamMember

    live = Event.objects.get(ref="live-demo")
    from quorum.accounts.models import User

    u = User.objects.create_user("twoteams@example.test")
    t1 = Team.objects.create(event=live, ref="tx1", name="A")
    t2 = Team.objects.create(event=live, ref="tx2", name="B")
    TeamMember.objects.create(team=t1, event=live, user=u)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            TeamMember.objects.create(team=t2, event=live, user=u)
