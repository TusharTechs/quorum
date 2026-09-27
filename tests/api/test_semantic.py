"""Meaning-based search, similar projects and duplicate suggestions (local embeddings)."""

import pytest


@pytest.fixture
def fixture_projects(db):
    from quorum.events.models import Project

    return list(Project.objects.filter(event__ref="evt_01", status="submitted").select_related("team", "track"))


def test_model_is_present_and_verified():
    from quorum.intelligence import embed

    assert embed.available(), embed.status()
    v = embed.encode(["tools for blind users", "screen reader accessibility app", "a trading bot"])
    assert v.shape == (3, 384)
    assert v[0] @ v[1] > v[0] @ v[2]  # meaning, not spelling


@pytest.mark.django_db
def test_boilerplate_is_ignored_and_only_the_real_duplicate_is_suggested(fixture_projects):
    from quorum.intelligence import semantic

    assert semantic.boilerplate(fixture_projects) == {"one line of what it does."}
    pairs = {frozenset((d["a"].ref, d["b"].ref)) for d in semantic.possible_duplicates(fixture_projects)}
    assert pairs == {frozenset(("prj_07", "prj_41"))}


@pytest.mark.django_db
def test_smart_gallery_search_and_exact_mode(client):
    smart = client.get("/e/sample-hack-2026/projects", {"q": "climate"}).content.decode()
    exact = client.get("/e/sample-hack-2026/projects", {"q": "climate", "mode": "exact"}).content.decode()
    from quorum.events.models import Project

    climate = list(Project.objects.filter(event__ref="evt_01", track__name="Climate", duplicate_of__isnull=True)
                   .values_list("title", flat=True))
    assert climate and all(t in smart for t in climate)
    assert 'value="exact"' in smart and "Smart" in smart
    assert client.get("/projects").status_code == 200 and "Small Meadow" in client.get("/projects").content.decode()
    assert exact.count('class="pcard"') <= smart.count('class="pcard"')


@pytest.mark.django_db
def test_similar_projects_on_the_project_page(client, fixture_projects):
    p = next(x for x in fixture_projects if x.duplicate_of_id is None)
    html = client.get(f"/p/{p.pk}").content.decode()
    assert "Similar projects" in html


def test_a_tampered_model_is_refused_and_features_fall_back(monkeypatch):
    from quorum.intelligence import embed

    monkeypatch.setitem(embed.SHA256, "tokenizer.json", "0" * 64)
    monkeypatch.setattr(embed, "_state", {"loaded": False, "session": None, "tok": None, "error": None})
    assert not embed.available() and "checksum" in embed.status()["error"]


def test_feedback_themes_quote_exactly_and_group_by_criterion():
    from types import SimpleNamespace as C

    from quorum.intelligence.themes import themes

    crit = [C(key="functionality", name="Functionality", description="Does it work? Run it, follow the demo."),
            C(key="quality", name="Quality", description="Is the code and documentation maintainable?"),
            C(key="innovation", name="Innovation", description="Is there an idea others have not had?")]
    fb = [{"judge": "Judge 1", "text": "The demo ran first time and the offline sync worked. The README has no setup steps; consider adding them."},
          {"judge": "Judge 2", "text": "A genuinely new take on spaced repetition. Tests are missing for the scheduler, which worries me."},
          {"judge": "Judge 3", "text": "It crashed when I imported a large deck. Try streaming the import instead of loading it all."}]
    t = themes(fb, crit)
    groups = {g["name"]: [q["text"] for q in g["quotes"]] for g in t["by_criterion"]}
    assert "The README has no setup steps; consider adding them." in groups["Quality"]
    assert "A genuinely new take on spaced repetition." in groups["Innovation"]
    assert "It crashed when I imported a large deck." in groups["Functionality"]
    written = " ".join(f["text"] for f in fb)
    assert all(q["text"] in written for g in t["by_criterion"] for q in g["quotes"])  # extractive only
    assert all(q["text"] in written for q in t["next_steps"]) and len(t["next_steps"]) >= 2
