"""Put a demo back to its seed: every table emptied and the fixture and live demo event loaded
again, in one transaction. Visitors mid-request wait a few seconds for the commit instead of
seeing half a reset. Uploaded files are removed afterwards and the local model re-indexes.

The hosted public demo (QUORUM_PUBLIC_DEMO) runs this at boot and then every
QUORUM_DEMO_RESET_MINUTES, so anything a visitor changes is temporary. It refuses to run
outside demo mode: production data is never touched.
"""

import shutil

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from .boot import BOOT_LOCK


class Command(BaseCommand):
    help = "Demo mode only: delete everything and reseed the fixture and live demo event."

    def handle(self, *args, **o):
        if not settings.QUORUM_DEMO or settings.QUORUM_ENV == "production":
            raise CommandError("demo_reset runs only in demo mode; it deletes every record.")
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", [BOOT_LOCK])
        try:
            call_command("migrate", interactive=False, verbosity=0)
            call_command("check", fail_level="ERROR")
            with transaction.atomic():
                # TRUNCATE fires no row triggers, so the append-only audit log and immutable runs
                # can be emptied here and nowhere else; flush also recreates content types
                call_command("flush", interactive=False, verbosity=0)
                call_command("seed_fixtures", quiet=True)
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [BOOT_LOCK])
        if settings.MEDIA_ROOT.is_dir():
            for child in settings.MEDIA_ROOT.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        call_command("warm_intelligence", verbosity=0)
        self.stdout.write("demo reset to the seed")
