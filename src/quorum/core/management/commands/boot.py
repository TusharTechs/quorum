"""Start-up for one or many web replicas: migrate, check, guard and seed, one replica at a time.

A session-level Postgres advisory lock serialises the steps, so replicas starting together never
race each other's migrations; the first does the work and the rest find nothing left to do (the
seed is idempotent).
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection

BOOT_LOCK = 7_205_512_026  # arbitrary, fixed


class Command(BaseCommand):
    help = "Migrate, run the deny-by-default check and production guards, and seed, under a cluster-wide lock."

    def handle(self, *args, **o):
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", [BOOT_LOCK])
        try:
            call_command("migrate", interactive=False, verbosity=0)
            call_command("check", fail_level="ERROR")      # deny-by-default: refuses to boot if a route lacks a policy
            call_command("production_guards")               # refuses demo credentials / default secret in production
            call_command("seed_fixtures")
        finally:
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", [BOOT_LOCK])
