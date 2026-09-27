from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Load the local embedding model and cache vectors for every listed event's submissions (boot-time warm-up)."

    def handle(self, *args, **o):
        from quorum.events.models import Event
        from quorum.intelligence import ask, embed, semantic
        from quorum.policy.repos import public_projects

        if not embed.available():
            self.stdout.write(f"intelligence: keyword fallbacks only ({embed.status()['error']})")
            return
        n = 0
        for ev in Event.objects.filter(is_listed=True):
            n += len(semantic.vectors(list(public_projects(ev))))
        ask._vectors()
        self.stdout.write(f"intelligence: {embed.MODEL_NAME} ready, {n} project vectors cached")
