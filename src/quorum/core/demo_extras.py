"""A second, clearly-labelled demo event that is *open* for submissions, so the full
participant flow (team -> invite -> draft -> submit -> deadline) can be tried at once.
It uses a Raptors-style rubric (Code Olympics weights) and is created once, on first boot."""

from datetime import timedelta

from django.db import transaction

from quorum.accounts.models import User
from quorum.core.clock import now
from quorum.events.models import Event, EventRole, Prize, Track
from quorum.judging import method as methods
from quorum.judging.models import Criterion

RUBRIC = [
    ("functionality", "Functionality & reliability", 4000, "Does it run, and keep running?"),
    ("constraints", "Constraint mastery", 3000, "How well does it use the event's constraints?"),
    ("code_quality", "Code quality", 2000, "Would a senior reviewer approve this codebase?"),
    ("innovation", "Innovation", 1000, "Is there an idea here others did not have?"),
]


@transaction.atomic
def ensure_live_demo_event():
    if Event.objects.filter(ref="live-demo").exists():
        return
    t0 = now().replace(microsecond=0)
    org = User.objects.get(email="organizer@demo.local")
    ev = Event.objects.create(
        ref="live-demo", slug="quorum-live-demo", name="Quorum Live Demo", is_demo=True,
        tagline="An open event for trying the whole flow: team, invite, draft, submit, judge, publish.",
        description_md="Synthetic demo event (not from the DOGFOOD fixture). Submissions are open.",
        registration_opens_at=t0 - timedelta(days=3), submissions_open_at=t0 - timedelta(days=2),
        submissions_close_at=t0 + timedelta(days=2), judging_opens_at=t0 + timedelta(days=3),
        judging_closes_at=t0 + timedelta(days=13), phase=Event.Phase.SUBMISSIONS,
        voting_mode=Event.VotingMode.AUTHENTICATED, created_by=org,
    )
    EventRole.objects.create(event=ev, user=org, role="organizer")
    for i, (name) in enumerate(["Developer tools", "Open data", "Accessibility"]):
        Track.objects.create(event=ev, ref=f"trk_{i + 1:02d}", name=name, position=i)
    for i, (key, name, w, desc) in enumerate(RUBRIC):
        Criterion.objects.create(event=ev, key=key, name=name, weight_bp=w, description=desc, position=i,
                                 anchors={"1": "Missing or broken", "3": "Solid", "5": "Exceptional"})
    Prize.objects.create(event=ev, name="Grand prize", places=3, value_text="$800 / $500 / $350")
    methods.lock(ev, org)
