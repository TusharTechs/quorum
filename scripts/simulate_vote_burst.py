#!/usr/bin/env python
"""Demo: simulate a suspicious burst of community votes, then open Organize > Voting.

Creates 14 'voters' who all arrive from one network and one browser within a few minutes
and vote for the same project, plus a handful of ordinary votes spread over time. The
integrity scan should raise F1 (velocity), F2 (same network + browser), F3 (new-identity
burst) and F6 (bot speed) flags, which the organizer can void with a reason.

    docker compose exec web python /app/scripts/simulate_vote_burst.py [project_ref]
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quorum.settings")
import django  # noqa: E402

django.setup()

import hashlib  # noqa: E402
import random  # noqa: E402

from django.utils import timezone  # noqa: E402

from quorum.events.models import Event, Project  # noqa: E402
from quorum.voting import services as voting  # noqa: E402
from quorum.voting.models import Vote, Voter  # noqa: E402

ev = Event.objects.get(ref="evt_01")
target = Project.objects.get(event=ev, ref=sys.argv[1] if len(sys.argv) > 1 else "prj_23")
others = list(Project.objects.filter(event=ev, status="submitted", duplicate_of__isnull=True).exclude(pk=target.pk))
now = timezone.now()
rng = random.Random(7)
run = hashlib.sha256(str(now).encode()).hexdigest()[:6]
for i in range(18):  # ordinary voters, spread over the last day, different networks
    v = Voter.objects.create(event=ev, kind="user", voter_key=hashlib.sha256(f"normal{run}{i}".encode()).hexdigest(),
                             verified_at=now - timedelta(hours=20))
    for p in rng.sample(others, 2):
        vt = Vote.objects.create(event=ev, voter=v, project=p, net_key=f"net-{run}-{i}", ua_key=f"ua-{i % 5}",
                                 ballot_issued_at=now - timedelta(hours=rng.randint(2, 20)), ballot_position=rng.randint(1, 40))
        Vote.objects.filter(pk=vt.pk).update(created_at=vt.ballot_issued_at + timedelta(seconds=rng.randint(20, 400)))
t0 = now - timedelta(minutes=4)
for i in range(14):  # the burst: same network, same browser, seconds apart, single-issue voters
    v = Voter.objects.create(event=ev, kind="user", voter_key=hashlib.sha256(f"burst{run}{i}".encode()).hexdigest(),
                             verified_at=t0)
    Voter.objects.filter(pk=v.pk).update(first_seen_at=t0 + timedelta(seconds=10 * i))
    vt = Vote.objects.create(event=ev, voter=v, project=target, net_key=f"net-burst-{run}", ua_key="ua-headless",
                             ballot_issued_at=t0 + timedelta(seconds=10 * i), ballot_position=rng.randint(1, 40))
    Vote.objects.filter(pk=vt.pk).update(created_at=t0 + timedelta(seconds=10 * i + 1))
n = voting.scan_flags(ev)
print(f"Simulated 14 burst votes for “{target.title}” and 36 ordinary votes; {n} new integrity flag(s).")
print("Open http://localhost:8080/o/sample-hack-2026/voting to review them.")
