"""The single source of 'now'. Deadlines are enforced against the server clock only
(never a client-supplied time); tests freeze this function."""

from django.utils import timezone


def now():
    return timezone.now()
