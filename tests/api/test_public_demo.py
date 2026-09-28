"""The hosted public demo: it resets to the seed, never sends mail, says what it is, and finds its
database and address from the hosting platform."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError


@pytest.mark.django_db
def test_demo_reset_restores_the_seed_and_forgets_visitors(event, settings, tmp_path):
    from quorum.accounts.models import User
    from quorum.audit.service import verify_chain
    from quorum.events.models import Event, Project

    settings.MEDIA_ROOT = tmp_path
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "upload.png").write_bytes(b"visitor upload")
    name, projects = event.name, Project.objects.filter(event=event).count()
    Event.objects.filter(pk=event.pk).update(name="Vandalised by a visitor")
    User.objects.create_user("visitor@example.org", password="a-long-visitor-password")

    call_command("demo_reset")

    ev = Event.objects.get(ref="evt_01")
    assert ev.name == name and Project.objects.filter(event=ev).count() == projects
    assert not User.objects.filter(email="visitor@example.org").exists()
    assert User.objects.filter(email="organizer@demo.local").exists()   # the one-click roles work again
    assert verify_chain(ev)["ok"]
    assert not any(tmp_path.iterdir())


@pytest.mark.django_db
def test_demo_reset_never_runs_outside_demo_mode(settings):
    settings.QUORUM_ENV = "production"
    with pytest.raises(CommandError, match="only in demo mode"):
        call_command("demo_reset")
    settings.QUORUM_ENV, settings.QUORUM_DEMO = "demo", False
    with pytest.raises(CommandError, match="only in demo mode"):
        call_command("demo_reset")


@pytest.mark.django_db
def test_public_demo_queues_but_never_delivers(settings, mailoutbox):
    from quorum.core.mail import demo_mail_hint, queue_email
    from quorum.integrations.worker import process_outbox

    settings.QUORUM_PUBLIC_DEMO = True
    row = queue_email("stranger@example.org", "hello", "typed in by a visitor")
    assert process_outbox() == 0
    row.refresh_from_db()
    assert row.status == "pending" and mailoutbox == []
    assert "sends no e-mail" in demo_mail_hint()
    settings.QUORUM_PUBLIC_DEMO = False
    assert "localhost:8025" in demo_mail_hint()


@pytest.mark.django_db
def test_public_demo_banner_counts_down_to_the_next_reset(client, settings):
    settings.QUORUM_PUBLIC_DEMO, settings.DEMO_RESET_MINUTES = True, 60
    page = client.get("/").content.decode()
    assert "Public demo." in page and "resets in" in page and "localhost:8025" not in page
    assert "one-click role" in client.get("/login").content.decode()
    settings.QUORUM_PUBLIC_DEMO = False
    page = client.get("/").content.decode()
    assert "Public demo." not in page and "Demo mode" in page


def test_database_url_and_platform_origin(monkeypatch):
    from quorum.settings import database_from_url, platform_origin

    db = database_from_url("postgresql://postgres:p%40ss@postgres.railway.internal:6543/railway?sslmode=require")
    assert db == {"NAME": "railway", "USER": "postgres", "PASSWORD": "p@ss", "HOST": "postgres.railway.internal",
                  "PORT": "6543", "OPTIONS": {"sslmode": "require"}}
    assert database_from_url("postgres://q:q@db/quorum")["PORT"] == "5432"
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    assert platform_origin() == ""
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "quorum-demo.up.railway.app")
    assert platform_origin() == "https://quorum-demo.up.railway.app"
