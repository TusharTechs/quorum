"""Leak tests. Status codes are not enough: an aggregate, an export, a search result or an
embed can leak data while returning 200. Canary strings are planted in private places and
every readable route is crawled as each lower-privileged role."""

import json
import re

import pytest

CANARIES = {
    "draft": "CANARYDRAFT7Q",
    "note": "CANARYNOTE7Q",
    "feedback": "CANARYFEEDBACK7Q",
    "comment_hidden": "CANARYHIDDEN7Q",
}


@pytest.fixture
def planted(db, actor):
    from quorum.events import services as ev
    from quorum.events.models import Comment, Event, Project
    from quorum.judging.models import Review

    live = Event.objects.get(ref="live-demo")
    from quorum.accounts.models import User

    u = User.objects.create_user("canary-maker@example.test", "pw-canary-123", name="Canary Maker")
    a = actor("canary-maker@example.test")
    ev.create_team(a, live, "Canary team")
    p = ev.create_project(a, live, {"title": CANARIES["draft"], "tagline": "private draft", "track": "trk_01"})
    rv = Review.objects.filter(event__ref="evt_01", judge_role__ref="jdg_26").first()
    Review.objects.filter(pk=rv.pk).update(note_to_organizers=CANARIES["note"],
                                           feedback_to_team=CANARIES["feedback"], moderation="pending")
    c = Comment.objects.create(project=Project.objects.get(ref="prj_02"), author=u, body=CANARIES["comment_hidden"],
                               body_html=CANARIES["comment_hidden"])
    from django.utils import timezone

    Comment.objects.filter(pk=c.pk).update(hidden_at=timezone.now())
    return {"draft_project": p}


def readable_urls(planted):
    from quorum.events.models import Project

    p2 = Project.objects.get(ref="prj_02")
    api = ["/api/v1/events", "/api/v1/events/evt_01", "/api/v1/events/evt_01/projects",
           "/api/v1/events/live-demo/projects", "/api/v1/events/evt_01/projects?q=CANARY",
           f"/api/v1/projects/{planted['draft_project'].pk}", f"/api/v1/projects/{p2.pk}",
           f"/api/v1/projects/{p2.pk}/comments", "/api/v1/me/scores", "/api/v1/me/assignments",
           "/api/v1/events/evt_01/judges/jdg_26/scores", "/api/v1/events/evt_01/rankings/latest",
           "/api/v1/events/evt_01/feedback?status=all", "/api/v1/events/evt_01/exports/reviews.csv",
           "/api/v1/events/evt_01/exports/feedback.csv", "/api/v1/events/evt_01/exports/projects.csv",
           "/api/v1/events/evt_01/exports/bundle.json", "/api/v1/events/evt_01/audit",
           "/api/v1/events/evt_01/results", "/api/v1/events/evt_01/votes/tally"]
    html = ["/", "/projects", "/projects?q=CANARY", "/e/quorum-live-demo/projects", "/e/sample-hack-2026",
            f"/p/{p2.pk}", f"/p/{planted['draft_project'].pk}", "/embed/events/quorum-live-demo/gallery",
            "/embed/events/sample-hack-2026/gallery", "/j/sample-hack-2026", "/me", "/o/sample-hack-2026/feedback",
            "/e/sample-hack-2026/results", "/e/sample-hack-2026/vote"]
    return api + html


@pytest.mark.parametrize("role", [None, "participant", "judge_a"])
def test_no_canary_leaks_to_lower_roles(role, planted, client_as):
    c = client_as(role)
    for url in readable_urls(planted):
        body = c.get(url).content.decode("utf-8", "replace")
        for name, canary in CANARIES.items():
            assert canary not in body, f"{name} leaked to {role or 'anonymous'} via {url}"


def test_organizer_does_see_private_material(planted, client_as):
    c = client_as("organizer")
    body = c.get("/api/v1/events/evt_01/feedback?status=all").content.decode()
    assert CANARIES["feedback"] in body
    body = c.get("/api/v1/events/evt_01/exports/reviews.csv").content.decode()
    assert CANARIES["note"] not in body  # private notes never leave the review screen, even in exports
    assert CANARIES["feedback"] in body


def test_judge_b_sees_own_private_note_only_in_own_scores(planted, client_as):
    own = client_as("judge_b").get("/api/v1/me/scores").content.decode()
    assert CANARIES["feedback"] in own
    peer = client_as("judge_a").get("/api/v1/events/evt_01/judges/jdg_26/scores")
    assert peer.status_code == 403 and CANARIES["feedback"] not in peer.content.decode()


def test_same_track_peer_is_refused_and_uniform_for_unknown_judges(client_as):
    c = client_as("judge_b")
    peer = c.get("/api/v1/events/evt_01/judges/jdg_24/scores")
    unknown = c.get("/api/v1/events/evt_01/judges/jdg_99/scores")
    # no existence oracle: a real peer and a judge who does not exist get the identical refusal
    assert peer.status_code == unknown.status_code == 403 and peer.content == unknown.content
    assert c.get("/api/v1/events/evt_01/judges/jdg_26/scores").status_code == 200


def test_invalid_token_is_401_even_with_a_valid_session(client, db):
    from quorum.accounts.models import User

    client.force_login(User.objects.get(email="organizer@demo.local"))
    r = client.get("/api/v1/events/evt_01/judges", HTTP_AUTHORIZATION="Bearer not-a-real-token")
    assert r.status_code == 401 and json.loads(r.content)["error"] == "invalid_token"


def test_session_writes_need_csrf_but_bearer_writes_do_not(db):
    from django.test import Client

    from quorum.accounts.models import User

    c = Client(enforce_csrf_checks=True)
    c.force_login(User.objects.get(email="organizer@demo.local"))
    r = c.post("/api/v1/events/evt_01/tracks", data=json.dumps({"name": "x"}), content_type="application/json")
    assert r.status_code == 403 and json.loads(r.content)["error"] == "csrf_failed"
    b = Client(enforce_csrf_checks=True, HTTP_AUTHORIZATION="Bearer qm_demo_organizer_7f2a91c4e8b3d6a0")
    r = b.post("/api/v1/events/evt_01/tracks", data=json.dumps({"name": "x"}), content_type="application/json")
    assert r.status_code == 201


def test_every_route_has_a_policy(db):
    from quorum.core.checks import every_route_has_a_policy

    assert every_route_has_a_policy(None) == []


PROTECTED = {"Review", "ReviewScore", "PairwiseComparison", "Vote"}
ALLOWED_MODULES = ("policy/", "results/inputs.py", "integrations/", "judging/", "voting/", "results/", "audit/",
                   "management/", "migrations/", "web/views_org.py", "web/views_judge.py", "api/feedback.py",
                   "api/judges.py", "core/demo")


def test_protected_models_are_only_queried_in_scoped_modules():
    """A lint: views and public API modules may not query protected models directly; they
    must go through policy.repos (or the service modules that enforce the rules)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "quorum"
    offenders = []
    for f in root.rglob("*.py"):
        rel = f.relative_to(root).as_posix()
        if any(rel.startswith(a) or f"/{a}" in rel for a in ALLOWED_MODULES):
            continue
        text = f.read_text()
        for m in PROTECTED:
            if re.search(rf"\b{m}\.objects\b", text):
                offenders.append(f"{rel}: {m}.objects")
    assert not offenders, offenders
