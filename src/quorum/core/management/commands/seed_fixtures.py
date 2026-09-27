"""Load the official DOGFOOD fixture (and, in demo mode, a live demo event).

Idempotent: if the fixture event already exists nothing is overwritten (people's work
after first boot is kept); demo tokens are ensured and the banner is printed every boot.

Honest seeding:
* the event closes at the fixture's own `submissions_close`, so it refuses new submissions
* the duplicate submission is detected and merged under the documented policy, not deleted
* the fixture has no assignment list, so coverage gaps are *shown*, not papered over
* the judging window is placed around first boot so the judging operation is live in the demo
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction

from engine.prepare import find_duplicates
from quorum.accounts.models import ApiToken, User
from quorum.audit import service as audit
from quorum.core.clock import now
from quorum.core.demo import DEMO_LOGINS, DEMO_PASSWORD, DEMO_TOKENS, banner
from quorum.core.middleware import token_hash
from quorum.core.text import render_markdown
from quorum.events.models import (EligibilityItem, Event, EventRole, JudgeTrack, Prize, Project, ProjectRevision,
                                  Team, TeamMember, Track)
from quorum.events.services import snapshot
from quorum.judging import method as methods
from quorum.judging.models import Assignment, Criterion, JudgeBatch, Review, ReviewScore

ANCHORS = {
    "functionality": {"1": "Does not run, or the core flow is missing", "3": "Core flow works with rough edges",
                      "5": "Works reliably end to end, including edge cases"},
    "quality": {"1": "Hard to follow; no structure or docs", "3": "Reasonable structure, some docs",
                "5": "Clean, tested, documented; a stranger could maintain it"},
    "innovation": {"1": "A well-trodden idea, executed as usual", "3": "A fresh angle on a known problem",
                   "5": "Genuinely new; I would steal this"},
}
DESCRIPTIONS = {
    "functionality": "Does it work? Run it, follow the demo, try the edge cases.",
    "quality": "Is the code and documentation something a team could maintain?",
    "innovation": "Is there an idea here that others have not had?",
}


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


def _name_from_email(email: str) -> str:
    local = email.split("@")[0]
    local = "".join(ch for ch in local if not ch.isdigit()).replace("_", " ").replace(".", " ")
    return " ".join(w.capitalize() for w in local.split()) or email


class Command(BaseCommand):
    help = "Seed the DOGFOOD fixture event (idempotent) and demo credentials."

    def add_arguments(self, parser):
        parser.add_argument("--fixtures", default=str(settings.REPO_DIR / "fixtures.json"))
        parser.add_argument("--no-extras", action="store_true")
        parser.add_argument("--quiet", action="store_true")

    def handle(self, *args, **opts):
        path = Path(opts["fixtures"])
        raw = path.read_bytes()
        fx = json.loads(raw)
        created = False
        if not Event.objects.filter(ref=fx["event"]["id"]).exists():
            self.load_fixture(fx, hashlib.sha256(raw).hexdigest())
            created = True
        if settings.QUORUM_DEMO:
            self.ensure_demo_accounts()
            if not opts["no_extras"]:
                from quorum.core.demo_extras import ensure_live_demo_event

                ensure_live_demo_event()
        ev = Event.objects.get(ref=fx["event"]["id"])
        from quorum.results.models import RankingRun

        if not RankingRun.objects.filter(event=ev).exists():
            from quorum.results.service import compute_run

            compute_run(ev, kind="preview")
        summary = (f"{'seeded' if created else 'found'} {ev.name}: {ev.projects.count()} projects "
                   f"({ev.projects.filter(duplicate_of__isnull=False).count()} duplicate merged), "
                   f"{ev.roles.filter(role='judge').count()} judges, "
                   f"{Review.objects.filter(event=ev).count()} reviews, {ev.tracks.count()} tracks")
        if settings.QUORUM_DEMO and not opts["quiet"]:
            self.stdout.write(banner(summary))
        elif not opts["quiet"]:
            self.stdout.write(summary)

    # ------------------------------------------------------------------ fixture
    @transaction.atomic
    def load_fixture(self, fx: dict, sha: str):
        with connection.cursor() as cur:
            cur.execute("SET LOCAL quorum.bypass = 'on'")  # importing past submissions after their deadline
        from django.utils.dateparse import parse_datetime

        t0 = now().replace(microsecond=0)
        close = parse_datetime(fx["event"]["submissions_close"])
        organizer = self._user("organizer@demo.local", "Olu Organizer")
        ev = Event.objects.create(
            ref=fx["event"]["id"], slug="sample-hack-2026", name=fx["event"]["name"],
            tagline="The DOGFOOD 2026 fixture event: 41 submissions, 30 judges, 8 tracks, and every awkward case.",
            description_md=(
                "This event is loaded from the official DOGFOOD fixture, unmodified.\n\n"
                "It contains the cases every judging system has to survive: a judge who gave every project the "
                "same score, batches nobody finished, projects with only two reviews, and a duplicate submission."
            ),
            rules_md="Teams of up to 4. Submissions close at the time shown above; the platform refuses anything later.",
            registration_opens_at=close - timedelta(days=30), submissions_open_at=close - timedelta(hours=72),
            submissions_close_at=close, judging_opens_at=t0 - timedelta(days=6),
            judging_closes_at=t0 + timedelta(days=5), voting_opens_at=t0 - timedelta(hours=1),
            voting_closes_at=t0 + timedelta(days=14), phase=Event.Phase.JUDGING,
            voting_mode=Event.VotingMode.AUTHENTICATED, reviews_per_project=3, batch_size=12, created_by=organizer,
            options={"fixture_sha256": sha, "source": "DOGFOOD fixtures.json",
                     "judging_window_note": "fixture has no judging dates; window placed around first boot"},
        )
        EventRole.objects.create(event=ev, user=organizer, role="organizer")
        audit.record("EVENT_CREATED", f"Event {ev.name} created from the DOGFOOD fixture", event=ev,
                     actor=organizer, actor_role="organizer", target=ev, at=close - timedelta(days=31))

        tracks = {}
        for i, t in enumerate(fx["tracks"]):
            tracks[t["id"]] = Track.objects.create(event=ev, ref=t["id"], name=t["name"], position=i)
        keys = []
        for s in fx["scores"]:
            for c in s["criteria"]:
                if c not in keys:
                    keys.append(c)
        base = 10000 // len(keys)
        crits = {}
        for i, k in enumerate(keys):
            w = base + (10000 - base * len(keys) if i == 0 else 0)
            crits[k] = Criterion.objects.create(event=ev, key=k, name=k.replace("_", " ").capitalize(),
                                                description=DESCRIPTIONS.get(k, ""), weight_bp=w,
                                                anchors=ANCHORS.get(k, {}), position=i)
        Prize.objects.create(event=ev, name="Grand prize", places=3, kind="judged", value_text="Top 3 overall",
                             description="Decided by the calibrated judges' ranking; ties at a boundary go to a "
                                         "pre-registered tie-break round.")
        Prize.objects.create(event=ev, name="Best in track", places=1, kind="judged", position=1,
                             description="The top project in each of the 8 tracks.")
        Prize.objects.create(event=ev, name="People's choice", places=1, kind="community", position=2,
                             description="Community vote. Kept separate from judged prizes.")
        methods.lock(ev, organizer, at=close - timedelta(days=30))

        # judges
        judge_roles = {}
        for j in fx["judges"]:
            u = self._user(j["email"], j["name"])
            r = EventRole.objects.create(event=ev, user=u, role="judge", ref=j["id"], capacity=12,
                                         last_activity_at=None)
            for t in j.get("tracks", []):
                JudgeTrack.objects.create(event_role=r, track=tracks[t])
            judge_roles[j["id"]] = r

        # teams
        teams = {}
        for t in fx["teams"]:
            team = Team.objects.create(event=ev, ref=t["id"], name=t["name"])
            for i, email in enumerate(t["members"]):
                u = self._user(email, _name_from_email(email))
                TeamMember.objects.create(team=team, event=ev, user=u, role="owner" if i == 0 else "member")
                EventRole.objects.get_or_create(event=ev, user=u, role="participant")
            teams[t["id"]] = team

        # projects (+ duplicates)
        projects = {}
        for p in fx["projects"]:
            desc = p.get("summary", "")
            pr = Project.objects.create(
                event=ev, ref=p["id"], team=teams[p["team"]], track=tracks.get(p["track"]), title=p["title"],
                tagline=p.get("summary", ""), description_md=desc, description_html=render_markdown(desc),
                repo_url=p.get("repo_url", ""), status=Project.Status.SUBMITTED,
                submitted_at=parse_datetime(p["submitted_at"]),
            )
            snap = snapshot(pr)
            from engine.bundle import sha256_hex

            pr.content_hash = sha256_hex(snap)
            pr.version = 1
            pr.save(update_fields=["content_hash", "version"])
            ProjectRevision.objects.create(project=pr, version=1, snapshot=snap, content_hash=pr.content_hash)
            projects[p["id"]] = pr
        dups = find_duplicates(fx["projects"], fx["event"]["submissions_close"])
        remap = {}
        for g in dups:
            canon = projects[g["canonical"]]
            for s in g["superseded"]:
                remap[s] = g["canonical"]
                old = projects[s]
                old.duplicate_of = canon
                old.save(update_fields=["duplicate_of"])
                EligibilityItem.objects.create(
                    event=ev, project=old, kind="duplicate", status="accepted",
                    details={"canonical": canon.ref, "superseded": s, "reason": g["reason"],
                             "policy": "latest on-time copy is canonical; reviews pooled; a judge who scored "
                                       "both copies counts once (criterion-wise mean)"},
                    resolution_note="Resolved by the published duplicate policy at import.", resolved_at=t0)
            audit.record("DUPLICATE_MERGED",
                         f"Duplicate submission {', '.join(g['superseded'])} merged into {g['canonical']} "
                         f"(“{canon.title}”, {g['reason']})", event=ev, actor_role="system", target=canon,
                         data=g)

        # reviews: one per (judge, canonical project); same-judge doubles averaged
        cells: dict = {}
        for s in fx["scores"]:
            p = remap.get(s["project"], s["project"])
            cells.setdefault((s["judge"], p), []).append(s)
        stalled_pending = self.infer_unfinished_batches(fx, cells, remap, k=ev.reviews_per_project)
        stalled = {j for j, _ in stalled_pending}
        span = (t0 - ev.judging_opens_at).total_seconds() - 3600 * 12
        batches = {}
        for (jid, pid), rows in sorted(cells.items()):
            role = judge_roles[jid]
            if jid not in batches:
                batches[jid] = JudgeBatch.objects.create(event=ev, judge_role=role, kind="import",
                                                         sent_at=ev.judging_opens_at, due_at=ev.judging_closes_at)
            a = Assignment.objects.create(event=ev, batch=batches[jid], judge_role=role, project=projects[pid],
                                          status="submitted", source="import", strategy="fixture")
            offset = (_h(jid, pid) % 7200) if jid in stalled else (_h(jid, pid) % int(span))
            submitted = ev.judging_opens_at + timedelta(seconds=3600 * 6 + offset)
            comments = [r.get("comment", "") for r in rows if r.get("comment")]
            rv = Review.objects.create(
                assignment=a, event=ev, judge_role=role, project=projects[pid], status="submitted",
                feedback_to_team="\n\n".join(dict.fromkeys(comments)), submitted_at=submitted,
                active_seconds=240 + _h("t", jid, pid) % 900,
                source_scores=[{"project": r["project"], "criteria": r["criteria"]} for r in rows],
                moderation="pending" if comments else "approved",
            )
            for key, crit in crits.items():
                vals = [r["criteria"][key] for r in rows if key in r["criteria"]]
                if vals:
                    ReviewScore.objects.create(review=rv, criterion=crit,
                                               value=Decimal(str(round(sum(vals) / len(vals), 2))))
            if role.last_activity_at is None or submitted > role.last_activity_at:
                role.last_activity_at = submitted
                role.save(update_fields=["last_activity_at"])
        for jid, pid in stalled_pending:
            role = judge_roles[jid]
            if jid not in batches:
                batches[jid] = JudgeBatch.objects.create(event=ev, judge_role=role, kind="import",
                                                         sent_at=ev.judging_opens_at, due_at=ev.judging_closes_at)
            Assignment.objects.create(event=ev, batch=batches[jid], judge_role=role, project=projects[pid],
                                      status="pending", source="import", strategy="fixture-inferred")
        if stalled_pending:
            audit.record(
                "UNFINISHED_BATCHES_INFERRED",
                f"The fixture describes review batches nobody finished but has no assignment list. Reconstructed "
                f"{len(stalled_pending)} pending assignment(s) for {', '.join(sorted(stalled))}: each missing review of "
                f"an under-covered project is attributed to the least-complete judge (<30%) in its track.",
                event=ev, actor_role="system", data={"pending": [list(x) for x in stalled_pending]})
        audit.record(
            "IMPORT_COMPLETED",
            f"DOGFOOD fixture imported: {len(fx['projects'])} projects, {len(fx['judges'])} judges, "
            f"{len(fx['scores'])} score rows -> {len(cells)} reviews", event=ev, actor_role="system",
            data={"fixture_sha256": sha, "projects": len(fx["projects"]), "judges": len(fx["judges"]),
                  "score_rows": len(fx["scores"]), "reviews": len(cells), "duplicates": dups},
        )
        audit.checkpoint(ev, "fixture imported")

    @staticmethod
    def infer_unfinished_batches(fx, cells, remap, k=3, threshold=0.30):
        """Reconstruct the fixture's 'review batches nobody finished' (it has no assignment
        list). For every canonical project below k reviews, attribute each missing review to
        the least-complete judge in its track, if that judge completed < threshold of the
        projects in their tracks. On the DOGFOOD fixture this yields jdg_23 and jdg_12."""
        canon = [p for p in fx["projects"] if p["id"] not in remap]
        track_of = {p["id"]: p["track"] for p in canon}
        per_track = {}
        for p in canon:
            per_track.setdefault(p["track"], []).append(p["id"])
        done = {}
        for (j, p) in cells:
            done.setdefault(j, set()).add(p)
        judges = {j["id"]: j for j in fx["judges"]}
        completion = {}
        for jid, j in judges.items():
            pool = sum(len(per_track.get(t, [])) for t in j.get("tracks", []))
            completion[jid] = len(done.get(jid, ())) / pool if pool else 1.0
        counts = {}
        for (j, p) in cells:
            counts[p] = counts.get(p, 0) + 1
        out = []
        for pid in sorted(track_of):
            missing = k - counts.get(pid, 0)
            if missing <= 0:
                continue
            cands = sorted((completion[j], len(done.get(j, ())), j) for j, jj in judges.items()
                           if track_of[pid] in jj.get("tracks", []) and pid not in done.get(j, set())
                           and completion[j] < threshold)
            for _, _, j in cands[:missing]:
                out.append((j, pid))
        return out

    def _user(self, email: str, name: str) -> User:
        u = User.objects.filter(email__iexact=email).first()
        if u:
            return u
        return User.objects.create_user(email=email, name=name)

    # ------------------------------------------------------------------ demo
    def ensure_demo_accounts(self):
        admin = self._user("admin@demo.local", "Ada Admin")
        if not admin.is_superuser:
            admin.is_superuser = admin.is_staff = True
            admin.save(update_fields=["is_superuser", "is_staff"])
        self._user("organizer@demo.local", "Olu Organizer")
        for email in DEMO_LOGINS:
            u = User.objects.filter(email__iexact=email).first()
            if u and not u.has_usable_password():
                u.set_password(DEMO_PASSWORD)
                u.save(update_fields=["password"])
        for role, (email, secret) in DEMO_TOKENS.items():
            u = User.objects.filter(email__iexact=email).first()
            if not u:
                continue
            ApiToken.objects.get_or_create(
                token_hash=token_hash(secret),
                defaults={"user": u, "name": f"demo {role}", "prefix": secret[:16], "is_demo": True},
            )
