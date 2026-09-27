from django.core.management.base import BaseCommand
from django.utils import timezone

from quorum.accounts.models import ApiToken


class Command(BaseCommand):
    help = "Revoke every demo API token (do this before going to production)."

    def add_arguments(self, parser):
        parser.add_argument("--revoke", action="store_true")

    def handle(self, *args, **o):
        n = ApiToken.objects.filter(is_demo=True, revoked_at__isnull=True).update(revoked_at=timezone.now())
        self.stdout.write(f"revoked {n} demo token(s)")
