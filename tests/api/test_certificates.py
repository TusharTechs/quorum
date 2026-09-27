"""Signed records after issue: revocation (final, public, on a signed list) and key rotation
(old records keep verifying, nothing new can be signed with the retired key)."""

import json

import pytest


@pytest.fixture
def keydir(settings, tmp_path):
    settings.KEY_DIR = tmp_path  # rotation deletes key files; never touch the real ones
    return tmp_path


@pytest.fixture
def protocol(event, keydir):
    from quorum.audit.certificates import issue_judge_protocol
    from quorum.events.models import EventRole

    return issue_judge_protocol(event, EventRole.objects.get(event=event, ref="jdg_24"))


def _post(c, url, body):
    return c.post(url, data=json.dumps(body), content_type="application/json")


@pytest.mark.django_db
def test_revocation_is_organizer_only_final_and_public(client_as, protocol):
    from quorum.audit.certificates import check, revocation_list
    from quorum.audit.models import AuditEvent
    from quorum.core.crypto import verify

    url = f"/api/v1/certificates/{protocol.pk}/revoke"
    for role in ("judge_a", "judge_b", "participant"):
        assert _post(client_as(role), url, {"reason": "not allowed to do this"}).status_code == 403
    assert _post(client_as(None), url, {"reason": "not allowed to do this"}).status_code == 401
    assert _post(client_as("organizer"), url, {"reason": "short"}).status_code == 422
    r = _post(client_as("organizer"), url, {"reason": "Issued in error: batch reassigned before review"})
    assert r.status_code == 200 and r.json()["serial"] == protocol.serial
    again = _post(client_as("organizer"), url, {"reason": "Issued in error: batch reassigned before review"})
    assert again.status_code == 409 and again.json()["error"] == "already_revoked"
    assert AuditEvent.objects.filter(action="CERTIFICATE_REVOKED", target_id=str(protocol.pk)).count() == 1

    res = check(protocol.payload, protocol.signature, protocol.key_id)
    assert res["valid"] is True and res["revoked"] is True and "reassigned" in res["revoked_reason"]
    public = client_as(None).get(f"/api/v1/certificates/{protocol.pk}").json()
    assert public["revoked"] is True and public["revoked_reason"].startswith("Issued in error")
    page = client_as(None).get(f"/certificates/{protocol.pk}").content.decode()
    assert "Revoked on" in page and "Issued in error" in page

    lst = revocation_list()  # the list is itself a signed record
    assert verify(lst["payload"], lst["signature"], lst["key_id"])
    rows = json.loads(lst["payload"])["revoked"]
    assert [(x["serial"], x["event"]) for x in rows] == [(protocol.serial, "evt_01")]
    served = client_as(None).get("/.well-known/quorum-revocations.json").json()
    assert json.loads(served["payload"])["revoked"][0]["serial"] == protocol.serial


@pytest.mark.django_db
def test_database_freezes_signed_records_and_revocation(protocol):
    from django.db import DatabaseError, transaction

    from quorum.audit.models import Certificate
    from quorum.core.clock import now

    with pytest.raises(DatabaseError, match="signed and cannot change"), transaction.atomic():
        Certificate.objects.filter(pk=protocol.pk).update(payload=protocol.payload.replace("judge", "winner"))
    Certificate.objects.filter(pk=protocol.pk).update(revoked_at=now(), revoked_reason="first reason given")
    with pytest.raises(DatabaseError, match="revocation is final"), transaction.atomic():  # un-revoking
        Certificate.objects.filter(pk=protocol.pk).update(revoked_at=None)
    with pytest.raises(DatabaseError, match="revocation is final"), transaction.atomic():  # rewording
        Certificate.objects.filter(pk=protocol.pk).update(revoked_reason="a nicer reason")


@pytest.mark.django_db
def test_key_rotation_keeps_old_records_valid(client_as, event, protocol, keydir):
    from django.core.management import call_command

    from quorum.audit.certificates import check, issue_judge_protocol
    from quorum.audit.models import AuditEvent
    from quorum.core.crypto import sign
    from quorum.core.models import SigningKey
    from quorum.events.models import EventRole

    old = protocol.key_id
    call_command("rotate_signing_key")
    assert SigningKey.objects.get(key_id=old).retired_at is not None
    assert AuditEvent.objects.filter(action="SIGNING_KEY_ROTATED", event_id__isnull=True).exists()
    new_kid, _ = sign("{}")
    assert new_kid != old and len(list(keydir.iterdir())) == 1  # the retired private key is gone

    res = check(protocol.payload, protocol.signature, protocol.key_id)
    assert res["valid"] is True and res["key"]["retired_at"]  # signed before retirement: still valid
    fresh = issue_judge_protocol(event, EventRole.objects.get(event=event, ref="jdg_26"))
    assert fresh.key_id == new_kid and check(fresh.payload, fresh.signature, fresh.key_id)["valid"]

    jwks = {k["kid"]: k for k in client_as(None).get("/.well-known/quorum-keys.json").json()["keys"]}
    assert jwks[old]["retired"] is True and jwks[new_kid]["retired"] is False

    # a record claiming to be signed by the retired key after it was retired is refused
    SigningKey.objects.filter(key_id=old).update(retired_at="2000-01-01T00:00:00+00:00")
    late = check(protocol.payload, protocol.signature, protocol.key_id)
    assert late["valid"] is False and "retired" in late["error"]


@pytest.mark.django_db
def test_organizer_lists_and_revokes_from_the_audit_page(client_as, protocol):
    from django.test import Client

    from quorum.accounts.models import User

    rows = client_as("organizer").get("/api/v1/events/evt_01/certificates").json()
    assert [r["serial"] for r in rows] == [protocol.serial]
    assert client_as("judge_a").get("/api/v1/events/evt_01/certificates").status_code == 403
    org = Client()
    org.force_login(User.objects.get(email="organizer@demo.local"))
    assert protocol.serial in org.get("/o/sample-hack-2026/audit").content.decode()
    r = org.post("/o/sample-hack-2026/audit", {"action": "revoke", "certificate": str(protocol.pk),
                                                "reason": "Judge asked for it to be withdrawn"})
    assert r.status_code == 302
    protocol.refresh_from_db()
    assert protocol.revoked_at and protocol.revoked_reason == "Judge asked for it to be withdrawn"
    assert org.post("/o/sample-hack-2026/audit", {"action": "revoke", "certificate": "not-a-uuid",
                                                   "reason": "whatever it is here"}).status_code == 404
