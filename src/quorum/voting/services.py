"""Community voting (T3): identity by access mode, hard blocks, statistical flags, review.

Design: hard-block only what is unambiguous (duplicate vote, over the cap, outside the
window, no valid ballot token, voting for your own team, rate limits). Everything
statistical (bursts, clusters, disposable domains, bot-speed votes) is FLAGGED with its
evidence for an organizer, who keeps or voids votes with a written reason. Nothing is
deleted; tallies are shown raw and reviewed. Tallies stay hidden from everyone but
organizers until the window closes.
"""

from __future__ import annotations

import hashlib
import hmac
import random
import statistics
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum

from quorum.audit import service as audit
from quorum.core import ratelimit
from quorum.core.clock import now
from quorum.core.text import render_markdown
from quorum.events.models import Comment, Event, Project, TeamMember
from quorum.policy.errors import Conflict, Forbidden, Invalid, NotAuthenticated, RateLimited, WindowClosed

from .models import IntegrityFlag, Vote, VoteCode, Voter

BALLOT_TTL = timedelta(hours=2)
FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
            "me.com", "proton.me", "protonmail.com", "aol.com", "gmx.com", "mail.com", "yandex.com", "qq.com"}
_DISPOSABLE = None


def disposable_domains() -> set:
    global _DISPOSABLE
    if _DISPOSABLE is None:
        p = Path(__file__).with_name("disposable_domains.txt")
        _DISPOSABLE = {l.strip().lower() for l in p.read_text().splitlines() if l.strip() and not l.startswith("#")} \
            if p.exists() else set()
    return _DISPOSABLE


def event_key(event: Event) -> bytes:
    return hmac.new(settings.SECRET_KEY.encode(), f"vote:{event.pk}".encode(), hashlib.sha256).digest()


def _h(event, *parts) -> str:
    return hmac.new(event_key(event), "|".join(parts).encode(), hashlib.sha256).hexdigest()


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


# --------------------------------------------------------------------------- identity

def cookie_name(event):
    return f"qv_{event.ref}"


def current_voter(request, event: Event) -> Voter | None:
    if event.voting_mode == "authenticated":
        if not request.user.is_authenticated:
            return None
        return Voter.objects.filter(event=event, voter_key=_h(event, "user", str(request.user.pk))).first()
    vid = request.get_signed_cookie(cookie_name(event), default=None, salt="quorum-voter")
    return Voter.objects.filter(event=event, pk=vid).first() if vid else None


def ensure_user_voter(request, event: Event) -> Voter:
    if not request.user.is_authenticated:
        raise NotAuthenticated("Sign in to vote in this event.")
    v, _ = Voter.objects.get_or_create(event=event, voter_key=_h(event, "user", str(request.user.pk)),
                                       defaults={"kind": "user", "user": request.user, "verified_at": now()})
    return v


def ensure_open_voter(request, event: Event) -> Voter:
    v = current_voter(request, event)
    if v:
        return v
    import secrets

    return Voter.objects.create(event=event, kind="open", voter_key=_h(event, "open", secrets.token_hex(16)),
                                verified_at=now())


def start_email_verification(request, event: Event, email: str):
    from quorum.accounts.services import issue_magic_link

    if event.voting_mode != "email":
        raise Invalid("This event does not use e-mail verified voting.", code="wrong_voting_mode")
    email = normalize_email(email)
    if "@" not in email:
        raise Invalid("Enter a valid e-mail address.")
    ok1, _ = ratelimit.hit("vote-link:" + ratelimit.hkey(email), 3, 3600)
    ok2, _ = ratelimit.hit("vote-link-net:" + ratelimit.hkey(ratelimit.client_net(request)), 30, 3600)
    if not (ok1 and ok2):
        raise RateLimited("Too many verification links requested. Try again later.")
    issue_magic_link(email, "voter", payload={"event": str(event.pk)}, next_url=f"/e/{event.slug}/vote",
                     subject=f"Confirm your vote in {event.name}",
                     intro="Open this link to confirm your e-mail and cast your votes. It works once.")


def complete_email_verification(request, link):
    from django.shortcuts import redirect

    event = Event.objects.get(pk=link.payload["event"])
    email = normalize_email(link.email)
    domain = email.rsplit("@", 1)[-1]
    v, _ = Voter.objects.get_or_create(event=event, voter_key=_h(event, "email", email),
                                       defaults={"kind": "email", "email_domain": domain, "verified_at": now()})
    resp = redirect(f"/e/{event.slug}/vote")
    resp.set_signed_cookie(cookie_name(event), str(v.pk), salt="quorum-voter", max_age=60 * 60 * 24 * 30,
                           httponly=True, samesite="Lax", secure=settings.SESSION_COOKIE_SECURE)
    return resp


@transaction.atomic
def redeem_code(request, event: Event, code: str) -> Voter:
    ok, _ = ratelimit.hit("vote-code-net:" + ratelimit.hkey(ratelimit.client_net(request)), 20, 600)
    if not ok:
        raise RateLimited("Too many attempts.")
    h = hashlib.sha256((code or "").strip().upper().encode()).hexdigest()
    vc = VoteCode.objects.select_for_update().filter(event=event, code_hash=h).first()
    if not vc:
        raise Invalid("That code is not valid for this event.")
    if vc.redeemed_by_id:
        return vc.redeemed_by
    v = Voter.objects.create(event=event, kind="code", voter_key=_h(event, "code", h), verified_at=now())
    vc.redeemed_by = v
    vc.save(update_fields=["redeemed_by"])
    return v


@transaction.atomic
def issue_codes(actor, event: Event, n: int, batch: str = "") -> list[str]:
    import secrets

    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    codes = []
    for _ in range(min(max(n, 1), 2000)):
        c = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
        VoteCode.objects.create(event=event, code_hash=hashlib.sha256(c.encode()).hexdigest(), batch=batch[:40])
        codes.append(c)
    audit.record("VOTE_CODES_ISSUED", f"{len(codes)} one-time voting codes issued", event=event, actor=actor,
                 actor_role="organizer", data={"count": len(codes), "batch": batch})
    return codes


# --------------------------------------------------------------------------- ballot

def ballot_order(event: Event, voter: Voter, projects: list, category: str = "overall") -> list:
    """Per-voter shuffle seeded by HMAC(event key, voter, category): stable for a voter,
    different across voters, so no project benefits from being listed first."""
    seed = int(_h(event, "order", voter.voter_key, category)[:16], 16)
    items = sorted(projects, key=lambda p: p.ref)
    random.Random(seed).shuffle(items)
    return items


def ballot_token(event: Event, voter: Voter) -> str:
    ts = int(now().timestamp())
    return f"{voter.pk}.{ts}.{_h(event, 'ballot', str(voter.pk), str(ts))[:32]}"


def check_ballot_token(event: Event, voter: Voter, token: str):
    try:
        vid, ts, sig = (token or "").split(".")
        ts = int(ts)
    except ValueError:
        raise Forbidden("Missing ballot token. Open the ballot page to vote.", code="ballot_token_required")
    if vid != str(voter.pk) or not hmac.compare_digest(sig, _h(event, "ballot", vid, str(ts))[:32]):
        raise Forbidden("Invalid ballot token.", code="ballot_token_invalid")
    issued = now().fromtimestamp(ts, tz=now().tzinfo)
    if now() - issued > BALLOT_TTL:
        raise Forbidden("Ballot expired; reload the page.", code="ballot_token_expired")
    return issued


def own_team_project_ids(event, voter: Voter) -> set:
    if voter.user_id:
        teams = TeamMember.objects.filter(event=event, user_id=voter.user_id).values("team_id")
        return set(Project.objects.filter(event=event, team_id__in=teams).values_list("pk", flat=True))
    if voter.kind == "email":
        ids = set()
        for m in TeamMember.objects.filter(event=event).select_related("user"):
            if _h(event, "email", normalize_email(m.user.email)) == voter.voter_key:
                ids |= set(Project.objects.filter(event=event, team_id=m.team_id).values_list("pk", flat=True))
        return ids
    return set()


def credits_available(event, voter) -> int:
    budget = event.votes_per_voter ** 2
    spent = sum(v.weight ** 2 for v in voter.votes.filter(status="valid"))
    return budget - spent


@transaction.atomic
def cast_vote(request, event: Event, voter: Voter, project: Project, token: str, weight: int = 1,
              category: str = "overall") -> Vote:
    t = now()
    if not event.voting_open(t):
        raise WindowClosed("Voting is not open.", code="voting_closed")
    if project.event_id != event.pk or project.status != "submitted" or project.duplicate_of_id:
        raise Invalid("That project is not on the ballot.")
    issued = check_ballot_token(event, voter, token)
    if project.pk in own_team_project_ids(event, voter):
        raise Forbidden("You cannot vote for your own team.", code="own_team")
    net = ratelimit.client_net(request)
    ok1, _ = ratelimit.hit("vote:" + voter.voter_key[:24], 30, 600)
    ok2, _ = ratelimit.hit("vote-net:" + _h(event, "net", net)[:24], 300, 600)
    if not (ok1 and ok2):
        audit.record("VOTE_REJECTED", "Vote rejected: rate limit", event=event, actor_role="voter",
                     data={"voter": voter.voter_key[:12], "reason": "rate_limited"})
        raise RateLimited("Too many votes too quickly.")
    Voter.objects.select_for_update().filter(pk=voter.pk).first()  # serialise this voter's votes
    quadratic = event.quadratic_voting and event.voting_mode in ("authenticated", "code")
    weight = max(1, int(weight or 1)) if quadratic else 1
    if quadratic:
        if weight ** 2 > credits_available(event, voter):
            raise Conflict("Not enough voice credits for that many votes.", code="credits_exhausted")
    elif voter.votes.filter(status="valid", category=category).count() >= event.votes_per_voter:
        raise Conflict(f"You have used all {event.votes_per_voter} votes.", code="vote_cap_reached")
    projects = list(Project.objects.filter(event=event, status="submitted", duplicate_of__isnull=True))
    order = [p.pk for p in ballot_order(event, voter, projects, category)]
    try:
        with transaction.atomic():
            v = Vote.objects.create(
                event=event, voter=voter, project=project, category=category, weight=weight,
                ballot_position=order.index(project.pk) + 1 if project.pk in order else None,
                net_key=_h(event, "net", net)[:40],
                ua_key=_h(event, "ua", request.META.get("HTTP_USER_AGENT", ""))[:40], ballot_issued_at=issued,
            )
    except IntegrityError:
        raise Conflict("You already voted for this project.", code="duplicate_vote")
    return v


# --------------------------------------------------------------------------- tallies & flags

def tallies(event: Event) -> list[dict]:
    rows = (Vote.objects.filter(event=event).values("project_id")
            .annotate(raw=Sum("weight"), valid=Sum("weight", filter=Q(status="valid")),
                      voided=Count("id", filter=Q(status="void"))))
    info = {p.pk: p for p in Project.objects.filter(event=event).select_related("team")}
    out = [{"project": info[r["project_id"]], "raw": r["raw"] or 0, "valid": r["valid"] or 0, "voided": r["voided"]}
           for r in rows if r["project_id"] in info]
    return sorted(out, key=lambda r: (-r["valid"], r["project"].title))


def tallies_visible_to(actor, event: Event) -> bool:
    if actor.is_organizer(event):
        return True
    return bool(event.voting_closes_at and now() >= event.voting_closes_at and event.published)


def position_bias(event: Event) -> list[tuple[int, int]]:
    c = Counter(Vote.objects.filter(event=event, status="valid", ballot_position__isnull=False)
                .values_list("ballot_position", flat=True))
    n = Project.objects.filter(event=event, status="submitted", duplicate_of__isnull=True).count()
    return [(i, c.get(i, 0)) for i in range(1, n + 1)]


def _flag(event, rule, subject_type, subject_id, summary, evidence, vote_ids, fp, severity="medium"):
    _, created = IntegrityFlag.objects.get_or_create(event=event, fingerprint=fp, defaults={
        "rule": rule, "subject_type": subject_type, "subject_id": str(subject_id), "summary": summary[:300],
        "evidence": evidence, "vote_ids": [str(v) for v in vote_ids], "severity": severity})
    if created:
        audit.record("FLAG_RAISED", f"[{rule}] {summary[:200]}", event=event, actor_role="system",
                     data={"rule": rule, "subject": str(subject_id), "votes": len(vote_ids)})
    return created


def scan_flags(event: Event) -> int:
    """Run every statistical rule over the event's votes. Idempotent (fingerprints)."""
    votes = list(Vote.objects.filter(event=event, status="valid").select_related("voter", "project")
                 .order_by("created_at"))
    if not votes:
        return 0
    new = 0
    titles = {v.project_id: v.project.title for v in votes}
    # F1 velocity: median/MAD z-score on 5-minute buckets per project
    by_p = defaultdict(lambda: defaultdict(list))
    for v in votes:
        by_p[v.project_id][int(v.created_at.timestamp()) // 300].append(v)
    for pid, buckets in by_p.items():
        for b, vs in buckets.items():
            base = [len(buckets.get(b - i, [])) for i in range(1, 13)]
            m = statistics.median(base)
            mad = statistics.median([abs(x - m) for x in base])
            z = (len(vs) - m) / (1.4826 * mad + 1)
            total = sum(len(v2) for p2 in by_p.values() for bb, v2 in p2.items() if bb == b)
            share = len(vs) / total if total else 0
            if len(vs) >= 5 and (z >= 4 or (total >= 10 and share >= 0.6)):
                new += _flag(event, "F1_velocity", "project", pid,
                             f"{len(vs)} votes for “{titles[pid]}” in 5 minutes (burst z={z:.1f}, {share:.0%} of all votes then)",
                             {"bucket_start": b * 300, "count": len(vs), "baseline_median": m, "z": round(z, 2),
                              "share": round(share, 2)}, [v.pk for v in vs], f"F1:{pid}:{b}", "high")
    # F2 fingerprint cluster: >=3 voters, same network + user agent, same project, 30 minutes
    groups = defaultdict(list)
    for v in votes:
        groups[(v.net_key, v.ua_key, v.project_id, int(v.created_at.timestamp()) // 1800)].append(v)
    for (net, ua, pid, w), vs in groups.items():
        voters = {v.voter_id for v in vs}
        if len(voters) >= 3 and net:
            new += _flag(event, "F2_fingerprint_cluster", "project", pid,
                         f"{len(voters)} different voters on one network and browser voted for “{titles[pid]}” within 30 minutes",
                         {"network": net[:10], "agent": ua[:10], "voters": len(voters)}, [v.pk for v in vs],
                         f"F2:{pid}:{net[:12]}:{ua[:12]}:{w}")
    # F3 new-identity burst: >=5 voters first seen within 10 min, voting only for the same project
    only = defaultdict(set)
    first_seen = {}
    for v in votes:
        only[v.voter_id].add(v.project_id)
        first_seen[v.voter_id] = v.voter.first_seen_at
    burst = defaultdict(list)
    for vid, ps in only.items():
        if len(ps) == 1:
            burst[(next(iter(ps)), int(first_seen[vid].timestamp()) // 600)].append(vid)
    for (pid, w), vids in burst.items():
        if len(vids) >= 5:
            vs = [v for v in votes if v.voter_id in set(vids)]
            new += _flag(event, "F3_new_identity_burst", "project", pid,
                         f"{len(vids)} brand-new voters, arriving within 10 minutes, voted only for “{titles[pid]}”",
                         {"voters": len(vids)}, [v.pk for v in vs], f"F3:{pid}:{w}")
    # F4 disposable domains
    disp = disposable_domains()
    for v in votes:
        if v.voter.email_domain and v.voter.email_domain in disp:
            new += _flag(event, "F4_disposable_domain", "voter", v.voter_id,
                         f"Vote from a disposable e-mail domain ({v.voter.email_domain})",
                         {"domain": v.voter.email_domain}, [v.pk], f"F4:{v.voter_id}", "low")
    # F5 rare-domain cluster
    dom = defaultdict(list)
    for v in votes:
        d = v.voter.email_domain
        if d and d not in FREEMAIL and d not in disp:
            dom[(d, v.project_id)].append(v)
    for (d, pid), vs in dom.items():
        if len({v.voter_id for v in vs}) >= 4:
            new += _flag(event, "F5_domain_cluster", "project", pid,
                         f"{len(vs)} voters from @{d} voted for “{titles[pid]}” (coworkers, or one catch-all domain?)",
                         {"domain": d, "voters": len(vs)}, [v.pk for v in vs], f"F5:{pid}:{d}", "low")
    # F6 bot speed
    for v in votes:
        if v.ballot_issued_at and (v.created_at - v.ballot_issued_at).total_seconds() < 2:
            new += _flag(event, "F6_bot_speed", "voter", v.voter_id,
                         f"Vote cast {(v.created_at - v.ballot_issued_at).total_seconds():.1f}s after the ballot opened",
                         {"seconds": round((v.created_at - v.ballot_issued_at).total_seconds(), 2)}, [v.pk],
                         f"F6:{v.pk}", "low")
    return new


@transaction.atomic
def resolve_flags(actor, event: Event, flag_ids: list, action: str, note: str) -> int:
    if action not in ("keep", "void", "dismiss"):
        raise Invalid("Action must be keep, void or dismiss.")
    if len((note or "").strip()) < 5:
        raise Invalid("Write a short reason; it is recorded in the audit log.")
    n = 0
    for f in IntegrityFlag.objects.filter(event=event, pk__in=flag_ids, status="open"):
        if action == "void":
            Vote.objects.filter(event=event, pk__in=f.vote_ids).update(
                status="void", void_reason=f"{f.rule}: {note.strip()[:200]}", voided_by=getattr(actor, "user", actor))
            f.status = IntegrityFlag.Status.VOIDED
        elif action == "keep":
            f.status = IntegrityFlag.Status.KEPT
        else:
            f.status = IntegrityFlag.Status.DISMISSED
        f.resolved_by = getattr(actor, "user", actor)
        f.resolution_note = note.strip()[:300]
        f.resolved_at = now()
        f.save()
        audit.record("FLAG_RESOLVED", f"[{f.rule}] {action}: {note.strip()[:160]} ({len(f.vote_ids)} vote(s))",
                     event=event, actor=actor, actor_role="organizer", target=f,
                     data={"action": action, "votes": f.vote_ids})
        n += 1
    return n


def ballot_state(request, event: Event):
    """What the vote box on a project page should show."""
    if event.voting_mode == "off" or not event.voting_opens_at:
        return None
    t = now()
    v = current_voter(request, event)
    used = v.votes.filter(status="valid").values_list("project_id", flat=True) if v else []
    return {"open": event.voting_open(t), "mode": event.voting_mode, "voter": v, "voted": set(used),
            "remaining": (event.votes_per_voter - len(used)) if v else event.votes_per_voter,
            "closes_at": event.voting_closes_at, "opens_at": event.voting_opens_at}


# --------------------------------------------------------------------------- comments

@transaction.atomic
def add_comment(request, project: Project, body: str) -> Comment:
    if not request.user.is_authenticated:
        raise NotAuthenticated("Sign in to comment.")
    body = (body or "").strip()
    if not (2 <= len(body) <= 2000):
        raise Invalid("Comments must be 2 to 2000 characters.")
    ok, _ = ratelimit.hit("comment:" + str(request.user.pk), 5, 60)
    if not ok:
        raise RateLimited("You are commenting too quickly.")
    c = Comment.objects.create(project=project, author=request.user, body=body, body_html=render_markdown(body))
    audit.record("COMMENT_POSTED", f"{request.user} commented on “{project.title}”", event=project.event,
                 actor=request.user, actor_role="participant", target=project)
    return c


@transaction.atomic
def hide_comment(actor, comment: Comment, reason: str = ""):
    actor.require_organizer(comment.project.event)
    comment.hidden_at = now()
    comment.hidden_by = actor.user
    comment.hidden_reason = (reason or "")[:200]
    comment.save(update_fields=["hidden_at", "hidden_by", "hidden_reason"])
    audit.record("COMMENT_HIDDEN", f"Comment on “{comment.project.title}” hidden", event=comment.project.event,
                 actor=actor, actor_role="organizer", target=comment.project, data={"reason": reason[:200]})


def vote_count() -> int:
    """Platform-wide count for operational metrics (no per-project detail, so no tally leaks)."""
    return Vote.objects.count()
