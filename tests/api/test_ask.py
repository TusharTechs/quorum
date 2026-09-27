"""Ask Quorum answers only from the event's data, only what the asker may know, or nothing."""

import pytest


def _ask(client, q, event="evt_01"):
    r = client.get("/api/v1/ask", {"q": q, "event": event})
    assert r.status_code == 200, r.content
    return r.json()["answer"]


@pytest.mark.django_db
def test_organizer_questions_run_named_skills(client_as):
    org = client_as("organizer")
    a = _ask(org, "who hasn't started?")
    assert a["skill"] == "stalled_judges" and "stalled" in a["text"] and "jdg_23" in " ".join(a["lines"])
    assert _ask(org, "is there a tie for first place")["skill"] == "ties"
    why = _ask(org, "why is Small Meadow ranked where it is")   # the name is found and masked
    assert why["skill"] == "why_rank" and why["text"].startswith("Small Meadow is #")
    assert any(link["url"].startswith("/o/sample-hack-2026/results/") for link in why["links"])
    assert _ask(org, "which teams will get no feedback")["skill"] == "feedback"
    assert "skill" in _ask(org, "what should I do next")["how"]


@pytest.mark.django_db
def test_answers_respect_roles(client_as):
    for role in ("judge_a", "judge_b", "participant", None):
        c = client_as(role)
        assert _ask(c, "who is winning") is None, f"{role} was told the unpublished ranking"
        assert _ask(c, "which judges are stalled") is None
    assert _ask(client_as("judge_a"), "how many reviews do I have left")["skill"] == "my_batch"
    assert _ask(client_as(None), "when do submissions close")["skill"] == "dates"


@pytest.mark.django_db
def test_off_topic_gets_no_answer_and_keywords_work_without_the_model(client_as, monkeypatch):
    org = client_as("organizer")
    assert _ask(org, "best pizza in town") is None
    from quorum.intelligence import embed

    monkeypatch.setattr(embed, "available", lambda: False)
    a = _ask(org, "which judges are behind or stalled")
    assert a["skill"] == "stalled_judges" and "keywords" in a["how"]


@pytest.mark.django_db
def test_palette_carries_the_answer(client_as):
    d = client_as("organizer").get("/api/v1/search", {"q": "is there a tie for first", "event": "sample-hack-2026"}).json()
    assert d["answer"]["skill"] == "ties"
    assert d["groups"][0]["title"] == "From the answer"
