"""Audit chain, exports, uploads, voting integrity."""

import io
import json
from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, transaction


def test_audit_log_is_append_only(db, event):
    from quorum.audit.models import AuditEvent

    a = AuditEvent.objects.filter(event_id=event.pk).first()
    with pytest.raises(DatabaseError, match="append-only"):
        with transaction.atomic():
            AuditEvent.objects.filter(pk=a.pk).update(summary="rewritten history")
    with pytest.raises(DatabaseError, match="append-only"):
        with transaction.atomic():
            AuditEvent.objects.filter(pk=a.pk).delete()


def test_hash_chain_detects_tampering_by_a_superuser(db, event):
    from quorum.audit import service as audit
    from quorum.audit.models import AuditEvent

    assert audit.verify_chain(event)["ok"]
    victim = AuditEvent.objects.filter(event_id=event.pk).order_by("seq")[2]
    with connection.cursor() as cur:  # what a DBA with superuser rights could do
        cur.execute("ALTER TABLE audit_auditevent DISABLE TRIGGER audit_append_only")
        cur.execute("UPDATE audit_auditevent SET summary = 'forged' WHERE seq = %s", [victim.seq])
        cur.execute("ALTER TABLE audit_auditevent ENABLE TRIGGER audit_append_only")
    res = audit.verify_chain(event)
    assert not res["ok"] and res["broken_seq"] == victim.seq and "modified" in res["reason"]


def test_signed_checkpoint_verifies_and_detects_tampering(db, event):
    from quorum.audit import service as audit
    from quorum.core.crypto import verify

    cp = audit.checkpoint(event, "test")
    assert verify(cp.payload, cp.signature, cp.key_id)
    assert not verify(cp.payload.replace('"seq":', '"seq":1', 1), cp.signature, cp.key_id)


def test_csv_exports_neutralise_formulas(db, client_as):
    from quorum.core.csvsafe import safe_cell, to_csv
    from quorum.events.models import Project

    assert safe_cell("=HYPERLINK(\"http://evil\")") == "'=HYPERLINK(\"http://evil\")"
    assert safe_cell("+1") == "'+1" and safe_cell("@SUM(A1)") == "'@SUM(A1)" and safe_cell(-5) == -5
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SET LOCAL quorum.bypass = 'on'")
        Project.objects.filter(ref="prj_05").update(title='=HYPERLINK("http://evil.example","x")')
        body = client_as("organizer").get("/api/v1/events/evt_01/exports/projects.csv").content.decode()
    assert "'=HYPERLINK" in body and ',=HYPERLINK' not in body


def test_image_upload_rejects_svg_and_polyglots(db, actor):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from quorum.accounts.models import User
    from quorum.events import services as ev
    from quorum.events.models import Event
    from quorum.policy.errors import Invalid

    live = Event.objects.get(ref="live-demo")
    User.objects.create_user("uploader@example.test", "pw-up-123456")
    a = actor("uploader@example.test")
    ev.create_team(a, live, "Uploaders")
    p = ev.create_project(a, live, {"title": "Pics", "track": "trk_01"})
    svg = SimpleUploadedFile("x.svg", b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>')
    with pytest.raises(Invalid):
        ev.add_image(a, p, svg)
    fake = SimpleUploadedFile("x.png", b"\x89PNG\r\n\x1a\n<script>alert(1)</script>")
    with pytest.raises(Invalid):
        ev.add_image(a, p, fake)
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 10, 10)).save(buf, "JPEG")
    img = ev.add_image(a, p, SimpleUploadedFile("ok.jpg", buf.getvalue()))
    assert img.path.endswith(".webp")  # re-encoded, metadata stripped


def test_markdown_is_sanitised(db):
    from quorum.core.text import render_markdown

    html = render_markdown('<script>alert(1)</script> [x](javascript:alert(1)) **ok**')
    assert "<script" not in html and 'href="javascript:' not in html and "<strong>ok</strong>" in html


def _burst(event, project, n=8, same_network=True):
    from django.utils import timezone

    from quorum.voting.models import Vote, Voter

    t = timezone.now().replace(second=5, microsecond=0)
    for i in range(n):
        v = Voter.objects.create(event=event, kind="open", voter_key=f"burst{project.ref}{i}", verified_at=t)
        Vote.objects.filter(pk=Vote.objects.create(
            event=event, voter=v, project=project, net_key="netA" if same_network else f"net{i}", ua_key="uaA",
            ballot_issued_at=t, ballot_position=1).pk).update(created_at=t + timedelta(seconds=i))


def test_vote_burst_is_flagged_and_can_be_voided_with_a_reason(db, event, actor):
    from quorum.events.models import Project
    from quorum.voting import services as voting
    from quorum.voting.models import IntegrityFlag, Vote

    p = Project.objects.get(ref="prj_03")
    _burst(event, p)
    voting.scan_flags(event)
    rules = set(IntegrityFlag.objects.filter(event=event).values_list("rule", flat=True))
    assert {"F1_velocity", "F2_fingerprint_cluster", "F3_new_identity_burst", "F6_bot_speed"} <= rules
    f = IntegrityFlag.objects.get(event=event, rule="F2_fingerprint_cluster")
    org = actor("organizer@demo.local")
    assert voting.resolve_flags(org, event, [f.pk], "void", "same network and browser, one person") == 1
    assert Vote.objects.filter(event=event, status="void").count() == len(f.vote_ids)
    t = {r["project"].ref: r for r in voting.tallies(event)}
    assert t["prj_03"]["valid"] < t["prj_03"]["raw"]  # raw and reviewed tallies both kept
    assert voting.scan_flags(event) == 0  # idempotent


def test_ballot_order_is_stable_per_voter_and_differs_between_voters(db, event):
    from quorum.policy.repos import public_projects
    from quorum.voting import services as voting
    from quorum.voting.models import Voter

    projects = list(public_projects(event))
    v1 = Voter.objects.create(event=event, kind="user", voter_key="a" * 64)
    v2 = Voter.objects.create(event=event, kind="user", voter_key="b" * 64)
    o1 = [p.ref for p in voting.ballot_order(event, v1, projects)]
    assert o1 == [p.ref for p in voting.ballot_order(event, v1, projects)]
    assert o1 != [p.ref for p in voting.ballot_order(event, v2, projects)]
