"""UX surfaces that carry rules of their own: demo sign-in and the feedback coach."""

import pytest


@pytest.mark.django_db
def test_demo_sign_in_is_post_only_known_roles_only_and_off_outside_demo(client, settings):
    settings.QUORUM_DEMO = True
    assert client.get("/demo/as/judge").status_code == 404
    assert client.post("/demo/as/admin").status_code == 404  # only the three showcase roles
    r = client.post("/demo/as/judge")
    assert r.status_code == 302 and r["Location"] == "/j/sample-hack-2026"
    assert client.get("/j/sample-hack-2026").status_code == 200
    settings.QUORUM_DEMO = False
    assert client.post("/demo/as/organizer").status_code == 404


@pytest.mark.django_db
def test_feedback_coach_is_for_judges_and_gives_useful_advice(client_as):
    url = "/j/sample-hack-2026/coach"
    assert client_as("participant").post(url, {"feedback": "hi"}).status_code == 403
    judge = client_as("judge_a")
    harsh = judge.post(url, {"feedback": "Useless. The whole thing is garbage."}).content.decode()
    assert "Feedback coach" in harsh and "may read as harsh" in harsh
    good = judge.post(url, {"feedback": "The demo runs cleanly and install worked first time. The idea is original. "
                                        "Consider adding tests for the parser and a README section on setup."}).content.decode()
    assert "may read as harsh" not in good and good.count('class="chip on"') == 3  # all three criteria touched


def test_coach_unit_checks():
    from types import SimpleNamespace as C

    from quorum.intelligence.coach import advise

    crits = [C(key="functionality", name="Functionality", description="Does it work?"),
             C(key="quality", name="Quality", description="Is the code maintainable?")]
    a = advise("", crits, 80)
    assert a["empty"] and a["coverage"] == []
    a = advise("It works: the demo ran. Consider adding tests.", crits, 20)
    assert all(c["ok"] for c in a["checks"]) and a["missing"] == []  # "works"/"demo" and "tests" cover both
    assert advise("It works: the demo ran.", crits, 10)["missing"] == ["Quality"]
