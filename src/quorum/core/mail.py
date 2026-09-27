"""E-mail goes through the outbox: queued in the same transaction as the change, delivered
by the worker. Offline (demo), the SMTP target is the Mailpit container, so every invite,
reminder and magic link is visible at http://localhost:8025 with the network off."""

from .models import Outbox


def queue_email(to: str, subject: str, body: str, html: str | None = None):
    return Outbox.objects.create(topic="email", target=to, payload={"to": to, "subject": subject, "body": body,
                                                                    "html": html})
