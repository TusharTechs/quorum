"""⌘K search only surfaces what the caller could open anyway."""

import pytest


def _titles(c, q, event="sample-hack-2026"):
    d = c.get("/api/v1/search", {"q": q, "event": event}).json()
    return {g["title"]: [(i["title"], i["url"]) for i in g["items"]] for g in d["groups"]}


@pytest.mark.django_db
def test_search_is_role_aware(client_as):
    org = _titles(client_as("organizer"), "who hasn't started")
    assert ("Judging ops: progress and reminders", "/o/sample-hack-2026/ops") in org["Go to"]
    for role in (None, "participant", "judge_b"):
        got = _titles(client_as(role), "results")
        urls = [u for items in got.values() for _, u in items]
        assert not any(u.startswith("/o/") for u in urls), f"{role} was offered organizer pages: {urls}"
    judge_names = _titles(client_as("organizer"), "diego")
    assert any("Diego" in t for t, _ in judge_names.get("People and teams", []))
    assert "People and teams" not in _titles(client_as("participant"), "diego")


@pytest.mark.django_db
def test_search_finds_public_projects_only(client_as):
    from quorum.events.models import Project

    p = Project.objects.filter(event__ref="evt_01", status="submitted", duplicate_of__isnull=True).first()
    anon = _titles(client_as(None), p.title.split()[0].lower())
    assert any(url == f"/p/{p.pk}" for _, url in anon.get("Projects", []))
    dup = Project.objects.filter(event__ref="evt_01", duplicate_of__isnull=False).first()
    hits = _titles(client_as(None), dup.title.lower())
    assert all(url != f"/p/{dup.pk}" for _, url in hits.get("Projects", [])), "a merged duplicate must not be searchable"
