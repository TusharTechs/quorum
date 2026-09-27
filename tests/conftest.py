"""Test setup: a real Postgres test database (triggers are part of what we test), seeded
once per session with the official DOGFOOD fixture exactly as `docker compose up` does."""

import os

import pytest

os.environ.setdefault("QUORUM_ENV", "test")
os.environ.setdefault("DJANGO_DEBUG", "1")
os.environ.setdefault("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.locmem.EmailBackend")

from quorum.core.demo import DEMO_TOKENS  # noqa: E402

HEADERS = {role: f"Bearer {secret}" for role, (_, secret) in DEMO_TOKENS.items()}


@pytest.fixture(scope="session")
def django_db_setup(django_db_setup, django_db_blocker):
    from django.core.management import call_command

    with django_db_blocker.unblock():
        from quorum.events.models import Event

        if not Event.objects.filter(ref="evt_01").exists():
            call_command("seed_fixtures", quiet=True)


@pytest.fixture
def client_as(client, db):
    """client_as("judge_a").get(url) sends the demo Bearer token for that role."""
    from django.test import Client

    def make(role=None):
        c = Client()
        if role:
            c.defaults["HTTP_AUTHORIZATION"] = HEADERS[role]
        return c

    return make


@pytest.fixture
def event(db):
    from quorum.events.models import Event

    return Event.objects.get(ref="evt_01")


@pytest.fixture
def actor():
    from quorum.accounts.models import User
    from quorum.policy.actor import Actor

    def make(email):
        return Actor(user=User.objects.get(email=email), via="session")

    return make
