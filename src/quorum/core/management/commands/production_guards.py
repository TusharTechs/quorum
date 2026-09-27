from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Refuse to start a production deployment with demo credentials or insecure defaults."

    def handle(self, *args, **o):
        if settings.QUORUM_ENV != "production":
            return
        from quorum.accounts.models import ApiToken

        problems = []
        if settings.DEFAULT_SECRET:
            problems.append("DJANGO_SECRET_KEY is the demo default")
        if settings.DEBUG:
            problems.append("DJANGO_DEBUG is on")
        if settings.QUORUM_DEMO:
            problems.append("QUORUM_DEMO is on")
        if ApiToken.objects.filter(is_demo=True, revoked_at__isnull=True).exists():
            problems.append("demo API tokens exist (run: manage.py rotate_demo_tokens --revoke)")
        if "console" in settings.EMAIL_BACKEND:
            problems.append("e-mail uses the console backend")
        if problems:
            raise CommandError("Refusing to start in production: " + "; ".join(problems))
