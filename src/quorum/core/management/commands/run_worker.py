from django.core.management.base import BaseCommand

from quorum.integrations.worker import run_forever, tick


class Command(BaseCommand):
    help = "Deliver e-mail/webhooks, send judge reminders, seal submissions at the deadline."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **o):
        if o["once"]:
            self.stdout.write(f"delivered {tick()} message(s)")
        else:
            run_forever()
