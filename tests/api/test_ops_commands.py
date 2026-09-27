"""Operational commands: safe to run twice, and the load-test helper refuses production."""

import io
import json

import pytest
from django.core.management import CommandError, call_command


@pytest.mark.django_db
def test_boot_is_idempotent_under_its_lock():
    for _ in range(2):  # a second replica finds nothing left to do
        call_command("boot", stdout=io.StringIO())
    from quorum.events.models import Event

    assert Event.objects.filter(ref="evt_01").count() == 1


@pytest.mark.django_db
def test_loadtest_setup_is_demo_only_and_creates_real_batches(settings):
    settings.QUORUM_DEMO = False
    with pytest.raises(CommandError):
        call_command("loadtest_setup", voters=1, judges=1, stdout=io.StringIO())
    settings.QUORUM_DEMO = True
    out = io.StringIO()
    call_command("loadtest_setup", voters=3, judges=2, batch=4, stdout=out)
    d = json.loads(out.getvalue())
    assert len(d["voters"]) == 3 and len(d["judges"]) == 2 and all(len(j["assignments"]) == 4 for j in d["judges"])
