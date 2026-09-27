"""One full event lifecycle through the services: the four pillars end to end."""

import json

import pytest


@pytest.mark.django_db
def test_full_lifecycle(event, actor, client_as):
    from quorum.audit import service as audit
    from quorum.audit.certificates import check
    from quorum.audit.models import Certificate
    from quorum.core.models import Outbox
    from quorum.events.models import Project
    from quorum.judging import feedback as fb
    from quorum.judging import ops
    from quorum.judging import services as judging
    from quorum.judging.models import Assignment
    from quorum.results import decide
    from quorum.results.service import compute_run

    org = actor("organizer@demo.local")
    # Pillar 1: the fixture's unfinished batches are visible and self-heal
    prog = {p["ref"]: p for p in ops.judge_progress(event)}
    assert prog["jdg_23"]["status"] == "stalled" and prog["jdg_12"]["status"] == "stalled"
    assert ops.coverage(event)["below"] == 3
    plan = ops.plan_rebalance(event, list(ops.inactive_role_ids(event)))
    assert plan["metrics"]["conflicts_violated"] == 0 and plan["metrics"]["components"] == 1
    assert plan["metrics"]["projects_at_target"] == plan["metrics"]["projects"]
    assert all(j not in ("jdg_23", "jdg_12") for j, _ in plan["new"])
    assert ops.commit_plan(event, org, plan, source="rebalance", batch_kind="rebalance") == 8
    assert ops.coverage(event)["below"] == 0
    assert Outbox.objects.filter(topic="email").exists()  # judges were e-mailed their new batch
    # a judge works through one new assignment
    a = Assignment.objects.filter(event=event, status="pending").select_related("judge_role__user").first()
    j = actor(a.judge_role.user.email)
    from quorum.policy.errors import Invalid

    with pytest.raises(Invalid):
        judging.save_review(j, a, scores={"functionality": 4, "quality": 4, "innovation": 4}, feedback="too short", submit=True)
    judging.save_review(j, a, scores={"functionality": 4, "quality": 3, "innovation": 5}, submit=True,
                        feedback="Clear scope and a working demo. The README needs setup steps; tests are missing.")
    # Pillar 2: focus round
    fp = decide.plan_focus(event, 4)
    assert fp["rows"] and all(r["se_after"] < r["se_before"] for r in fp["rows"])
    decide.commit_focus(event, org, fp)
    # Pillar 3: tie at the podium -> pairwise round -> decision
    run = compute_run(event, org, heavy=False)
    ties = decide.boundary_ties(run, event.prize_positions)
    assert ties and ties[0]["boundary"] == 1
    tb = decide.open_tiebreak(event, org, ties[0]["projects"][:3], ties[0]["boundary"])
    assert tb.judge_roles.count() == 3
    for role in tb.judge_roles.all():
        ja = actor(role.user.email)
        while True:
            pair, _, _ = decide.tiebreak_next_pair(tb, role)
            if not pair:
                break
            pa, pb = (Project.objects.get(event=event, ref=r) for r in pair)
            judging.record_comparison(ja, event, role, pa, pb, "a" if pa.ref < pb.ref else "b", tiebreak=tb)
    st = decide.tiebreak_state(tb)
    assert st["comparisons"] == 9
    decide.resolve_tiebreak(tb, org, None if st["resolved"] else st["order"], "" if st["resolved"] else "panel unanimous")
    # lock and publish
    fb.approve_all_pending(org, event)
    pub = decide.lock_results(event, org)
    assert pub.final_order[:len(st["order"])] == tb.result["order"] or set(tb.result["order"]) <= set(pub.final_order)
    assert client_as("participant").get("/api/v1/events/evt_01/results").status_code == 403  # locked, not published
    decide.publish_results(event, org)
    r = client_as(None).get("/e/sample-hack-2026/results")
    assert r.status_code == 200 and pub.run.output_hash.encode() in r.content
    # Pillar 4: every team gets a scorecard; judges get signed protocols
    sc = json.loads(client_as("participant").get("/api/v1/me/scorecard?event=evt_01").content)
    assert sc["project"] == "prj_01" and sc["calibrated"] is not None and "explanation" in sc
    assert all(not str(line["judge"]).startswith("jdg_") for line in sc["explanation"]["lines"])  # pseudonymous
    proto = Certificate.objects.filter(event=event, kind="judge_protocol").first()
    assert proto and check(proto.payload, proto.signature, proto.key_id)["valid"]
    payload = json.loads(proto.payload)  # protocols document work, never scores
    assert not {"scores", "criteria", "calibrated", "rank"} & set(payload)
    assert audit.verify_chain(event)["ok"]
    # portability: the bundle recomputes
    from quorum.integrations.bundle import export_bundle, verify_bundle

    assert verify_bundle(json.loads(json.dumps(export_bundle(event), default=str)))["all_match"] is True
