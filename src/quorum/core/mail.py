"""E-mail goes through the outbox: queued in the same transaction as the change, delivered
by the worker. Offline (demo), the SMTP target is the Mailpit container, so every invite,
reminder and magic link is visible at http://localhost:8025 with the network off. A public
demo (QUORUM_PUBLIC_DEMO) queues but never sends, so it cannot be used to mail strangers."""

from django.conf import settings

from .models import Outbox


def demo_mail_hint() -> str:
    """Where a demo visitor should look for the e-mail they just asked for."""
    if settings.QUORUM_PUBLIC_DEMO:
        return " (This public demo sends no e-mail: use the one-click demo roles on the home page.)"
    if settings.QUORUM_DEMO:
        return " (Offline demo: open the mail catcher at localhost:8025.)"
    return ""


def queue_email(to: str, subject: str, body: str, html: str | None = None):
    return Outbox.objects.create(topic="email", target=to, payload={"to": to, "subject": subject, "body": body,
                                                                    "html": html})
