import time

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.utils import OperationalError


class Command(BaseCommand):
    help = "Wait until Postgres accepts connections (and, with --migrated, until migrations ran)."

    def add_arguments(self, parser):
        parser.add_argument("--migrated", action="store_true")
        parser.add_argument("--timeout", type=int, default=120)

    def handle(self, *args, **o):
        deadline = time.time() + o["timeout"]
        while True:
            try:
                with connection.cursor() as cur:
                    cur.execute("SELECT 1")
                    if o["migrated"]:
                        cur.execute("SELECT to_regclass('public.audit_auditevent') IS NOT NULL")
                        if not cur.fetchone()[0]:
                            raise OperationalError("not migrated yet")
                        cur.execute("SELECT count(*) FROM events_event")
                return
            except OperationalError as e:
                if time.time() > deadline:
                    raise
                self.stdout.write(f"waiting for database: {str(e).splitlines()[0][:80]}")
                connection.close()
                time.sleep(1)
