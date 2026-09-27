"""Comparative (pairwise) judging per track: who may compare what, the Bradley-Terry board,
and the promise that comparisons never move the official ranking."""

import json

import pytest

TRACK = "trk_01"  # Developer tools: judges A (jdg_24) and B (jdg_26) both judge it
URL = "/api/v1/me/events/evt_01/pairwise"


def _pool(judge, track=TRACK):
    from quorum.events.models import EventRole, Track
    from quorum.judging.pairwise import pool

    return pool(EventRole.objects.get(event__ref="evt_01", ref=judge), Track.objects.get(event__ref="evt_01", ref=track))


def _post(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json")


def _mine(c):
    tracks = c.get(URL).json()["tracks"]
    return next(t for t in tracks if t["track"] == TRACK)


def _compare_all(c, prefer=min):
    """Walk the judge through every pair the server offers; always prefer `prefer` of the two refs."""
    seen = set()
    while True:
        st = _mine(c)
        if not st["next"]:
            return seen, st
        a, b = st["next"]
        assert frozenset((a, b)) not in seen, "the server offered a pair twice"
        seen.add(frozenset((a, b)))
        r = _post(c, URL, {"a": a, "b": b, "outcome": "a" if prefer(a, b) == a else "b"})
        assert r.status_code == 201, r.content


@pytest.fixture
def comparative(event, actor):
    from quorum.events.models import Track
    from quorum.judging import pairwise

    pairwise.set_mode(event, actor("organizer@demo.local"), Track.objects.get(event=event, ref=TRACK), True)


@pytest.mark.django_db
def test_off_by_default_and_switching_on_is_audited(client_as, event):
    from quorum.audit.models import AuditEvent
    from quorum.core.models import Outbox

    ja = client_as("judge_a")
    a, b = _pool("jdg_24")[:2]
    r = _post(ja, URL, {"a": a, "b": b, "outcome": "a"})
    assert r.status_code == 409 and r.json()["error"] == "pairwise_disabled"
    assert ja.get(URL).json()["tracks"] == []
    assert _post(ja, f"/api/v1/events/evt_01/tracks/{TRACK}/pairwise", {"enabled": True}).status_code == 403
    mails = Outbox.objects.filter(topic="email").count()
    r = _post(client_as("organizer"), f"/api/v1/events/evt_01/tracks/{TRACK}/pairwise", {"enabled": True})
    assert r.status_code == 200 and r.json() == {"track": TRACK, "enabled": True}
    assert AuditEvent.objects.filter(event_id=event.pk, action="PAIRWISE_MODE_CHANGED").count() == 1
    assert Outbox.objects.filter(topic="email").count() > mails  # the track's judges are told


@pytest.mark.django_db
def test_judge_compares_only_reviewed_projects_and_never_twice(client_as, comparative):
    ja = client_as("judge_a")
    pool_a = set(_pool("jdg_24"))
    st = _mine(ja)
    n = len(pool_a)
    assert set(st["pool"]) == pool_a and st["pairs"] == n * (n - 1) // 2 and st["target"] == min(10, st["pairs"])
    seen, st = _compare_all(ja)
    assert len(seen) == st["pairs"] == st["done"]
    assert all(pair <= pool_a for pair in seen)
    a, b = sorted(next(iter(seen)))
    r = _post(ja, URL, {"a": b, "b": a, "outcome": "tie"})  # same pair, other order
    assert r.status_code == 409 and r.json()["error"] == "duplicate_comparison"
    pool_b = set(_pool("jdg_26"))
    not_by_b = sorted(pool_a - pool_b)  # in B's track, reviewed by A, never by B
    assert not_by_b
    r = _post(client_as("judge_b"), URL, {"a": sorted(pool_b)[0], "b": not_by_b[0], "outcome": "a"})
    assert r.status_code == 403
    education = _pool("jdg_24", "trk_07")
    assert _post(ja, URL, {"a": sorted(pool_a)[0], "b": education[0], "outcome": "a"}).status_code == 403
    for role in ("participant", "organizer"):
        assert _post(client_as(role), URL, {"a": a, "b": b, "outcome": "a"}).status_code == 403
    assert _mine(client_as("judge_b"))["done"] == 0  # B sees nothing of A's choices


@pytest.mark.django_db
def test_database_refuses_a_repeated_comparison(event, comparative):
    from django.db import IntegrityError, transaction

    from quorum.events.models import EventRole, Project
    from quorum.judging.models import PairwiseComparison

    role = EventRole.objects.get(event=event, ref="jdg_24")
    x, y = (Project.objects.get(event=event, ref=r) for r in _pool("jdg_24")[:2])
    PairwiseComparison.objects.create(event=event, judge_role=role, project_a=x, project_b=y, outcome="a")
    with pytest.raises(IntegrityError), transaction.atomic():  # NULLS NOT DISTINCT: tiebreak IS NULL still unique
        PairwiseComparison.objects.create(event=event, judge_role=role, project_a=x, project_b=y, outcome="b")


@pytest.mark.django_db
def test_board_follows_the_choices_and_the_official_ranking_does_not_move(client_as, comparative, event, actor):
    from quorum.integrations.bundle import export_bundle, import_bundle
    from quorum.results.models import RankingRun
    from quorum.results.service import compute_run

    org = actor("organizer@demo.local")
    before = compute_run(event, org, kind=RankingRun.Kind.PREVIEW, heavy=False).output_hash
    seen, _ = _compare_all(client_as("judge_a"), prefer=min)  # a complete, consistent round robin
    assert compute_run(event, org, kind=RankingRun.Kind.PREVIEW, heavy=False).output_hash == before

    assert client_as("judge_a").get("/api/v1/events/evt_01/pairwise").status_code == 403
    boards = client_as("organizer").get("/api/v1/events/evt_01/pairwise").json()["tracks"]
    board = next(b for b in boards if b["track"]["ref"] == TRACK)
    assert board["comparisons"] == len(seen) and board["per_judge"] == {"jdg_24": len(seen)}
    assert board["components"] == 1 and -1.0 <= board["tau"] <= 1.0
    rows = board["projects"]
    assert [p["ref"] for p in rows] == sorted(_pool("jdg_24"))  # lower ref always preferred -> that order
    assert rows[0]["losses"] == 0 and rows[-1]["wins"] == 0
    assert all(p["rank_lo"] <= p["rank"] <= p["rank_hi"] for p in rows)

    csv = client_as("organizer").get("/api/v1/events/evt_01/exports/comparisons.csv").content.decode()
    assert csv.count(",comparative,") == len(seen)

    copy, report = import_bundle(org, json.loads(json.dumps(export_bundle(event), default=str)))
    assert report["all_match"] is True
    assert copy.tracks.get(ref=TRACK).pairwise is True
    assert copy.comparisons.filter(tiebreak__isnull=True).count() == len(seen)


@pytest.mark.django_db
def test_web_pages(client_as, comparative):
    from django.test import Client

    from quorum.accounts.models import User

    judge = Client()
    judge.force_login(User.objects.get(email="diego.herrera@example.org"))
    inbox = judge.get("/j/sample-hack-2026").content.decode()
    assert "Comparisons · Developer tools" in inbox
    page = judge.get(f"/j/sample-hack-2026/compare/{TRACK}")
    assert page.status_code == 200 and b"Which of these two is stronger overall?" in page.content
    org = Client()
    org.force_login(User.objects.get(email="organizer@demo.local"))
    page = org.get("/o/sample-hack-2026/results/pairwise")
    assert page.status_code == 200 and b"Comparative judging" in page.content
    other = Client()
    other.force_login(User.objects.get(email="jonas.vogel@example.org"))  # judge B does not judge Education
    assert other.get("/j/sample-hack-2026/compare/trk_07").status_code == 403
