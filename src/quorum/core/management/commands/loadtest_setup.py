"""Create load-test identities on the fixture event and print their API tokens as JSON.

Demo/test stacks only (refuses in production). Used by tools/loadtest.py:

    docker compose exec -T web python manage.py loadtest_setup --voters 300 --judges 20
"""

import json
import random

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Create load-test voters and judges (with batches) on the fixture event; print tokens as JSON."

    def add_arguments(self, parser):
        parser.add_argument("--voters", type=int, default=300)
        parser.add_argument("--judges", type=int, default=20)
        parser.add_argument("--batch", type=int, default=10)
        parser.add_argument("--event", default="evt_01")

    @transaction.atomic
    def handle(self, *args, **o):
        from quorum.accounts.models import ApiToken
        from quorum.accounts.services import create_token, user_for_email
        from quorum.events.models import Event, EventRole, JudgeTrack
        from quorum.judging.models import Assignment
        from quorum.policy.repos import public_projects

        if not settings.QUORUM_DEMO:
            raise CommandError("loadtest_setup only runs on demo/test stacks (QUORUM_DEMO=1).")
        ev = Event.objects.get(ref=o["event"])
        projects = list(public_projects(ev))
        tracks = list(ev.tracks.all())
        rng = random.Random(2026)
        out = {"event": ev.ref, "slug": ev.slug, "criteria": list(ev.criteria.values_list("key", flat=True)),
               "min_feedback": ev.min_feedback_chars, "projects": [p.ref for p in projects], "voters": [], "judges": []}
        for i in range(o["voters"]):
            u = user_for_email(f"lt-voter-{i:04d}@load.test", f"Load voter {i}")
            ApiToken.objects.filter(user=u, name="loadtest").delete()
            out["voters"].append(create_token(u, "loadtest", days=1)[1])
        for j in range(o["judges"]):
            u = user_for_email(f"lt-judge-{j:03d}@load.test", f"Load judge {j}")
            role, _ = EventRole.objects.get_or_create(event=ev, user=u, role="judge", defaults={"ref": f"lt_{j:03d}"})
            for t in tracks:
                JudgeTrack.objects.get_or_create(event_role=role, track=t)
            mine = list(Assignment.objects.filter(judge_role=role).values_list("pk", flat=True))
            if not mine:  # re-runs reuse the batch
                mine = [Assignment.objects.create(event=ev, judge_role=role, project=p, source="import").pk
                        for p in rng.sample(projects, min(o["batch"], len(projects)))]
            ApiToken.objects.filter(user=u, name="loadtest").delete()
            out["judges"].append({"ref": role.ref, "token": create_token(u, "loadtest", days=1)[1],
                                  "assignments": [str(a) for a in mine]})
        self.stdout.write(json.dumps(out))
