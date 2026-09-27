"""Focus rounds plan against every prize the event pays: overall places and, when the event
awards them, track prizes (the fixture's "Best in track")."""

import pytest


@pytest.mark.django_db
def test_focus_plans_against_track_prizes_when_the_event_pays_them(event):
    from quorum.events.models import Event
    from quorum.results import decide
    from quorum.results.inputs import build_input

    places = build_input(event)["method"]["track_places"]
    assert set(places) == set(event.tracks.values_list("ref", flat=True)) and set(places.values()) == {1}
    plan = decide.plan_focus(event, 12, seed=1)
    assert plan["track_prizes"] and plan["rows"] and all("p_track_prize" in r for r in plan["rows"])
    # at least one review goes to a project that is nearly out overall but open in its track
    assert any(r["p_prize"] < 0.1 and r["p_track_prize"] > 0.2 for r in plan["rows"])

    demo = Event.objects.get(ref="live-demo")  # no track prize: the engine input is unchanged
    assert "track_places" not in build_input(demo)["method"]
