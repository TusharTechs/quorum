"""⌘K search: pages, actions, projects and people, filtered by what the caller may open.

Every result is a link that goes through its own server-side policy when followed, so this
module can only help someone find what they are already allowed to see. Pages carry
plain-language synonyms ("who hasn't started" finds Judging ops) so a first-time organizer
does not need to know our vocabulary.
"""

from __future__ import annotations

import re

from django.db.models import Q

from quorum.events.models import Event, EventRole, Team
from quorum.policy.repos import public_projects

# (title, path suffix, icon, words people actually type)
ORG_PAGES = [
    ("Overview: decisions that need you", "", "layout-dashboard", "home dashboard todo next what should i do decisions"),
    ("Setup: dates, tracks, prizes, rubric", "/setup", "settings", "dates deadline rubric criteria weights tracks prizes questions publish lock method"),
    ("Participants and eligibility", "/participants", "users", "teams members duplicates eligibility submissions withdrawn"),
    ("Judges and conflicts", "/judges", "gavel", "invite judge conflict of interest capacity availability"),
    ("Judge statistics", "/judges/stats", "chart-column", "leniency harsh generous flat judge offset calibration stats"),
    ("Judging ops: progress and reminders", "/ops", "activity", "progress stalled behind who hasn't started reminders nudge rebalance batches burn-down forecast"),
    ("Results and ranking", "/results", "trophy", "ranking podium winners scores calibrated tie ties signal focus round lock publish"),
    ("Comparative judging (pairwise)", "/results/pairwise", "arrow-left-right", "pairwise bradley terry comparisons head to head gavel"),
    ("Feedback to teams", "/feedback", "message-square-text", "feedback comments moderation release quotes scorecards no feedback"),
    ("Community voting and integrity", "/voting", "vote", "votes voting tallies fraud abuse flags sybil codes"),
    ("Audit trail and signed records", "/audit", "shield-check", "audit log history who did what verify chain certificates revoke"),
    ("Data: exports, imports, webhooks", "/data", "database", "csv export import bundle devpost unstop webhooks backup"),
]
ORG_ACTIONS = [
    ("Plan a focus round", "/results#focus", "target", "extra reviews uncertain prize focus"),
    ("Rebalance stalled judges' work", "/ops", "refresh-cw", "rebalance reassign stalled unfinished batches"),
    ("Remind judges who are behind", "/ops", "bell", "nudge remind email reminder behind"),
    ("Verify the audit chain", "/audit", "shield-check", "verify integrity tamper chain"),
    ("Switch on comparative judging", "/results/pairwise", "arrow-left-right", "pairwise mode enable"),
]
PUBLIC_PAGES = [
    ("Events", "/events", "calendar", "hackathons events list"),
    ("Project gallery", "/projects", "layout-grid", "projects gallery submissions browse"),
    ("Verify a signed record", "/verify", "badge-check", "verify certificate protocol signature"),
    ("How judging works", "/methodology", "book-open", "methodology calibration normalization fairness how scores"),
    ("API documentation", "/api/v1/docs", "code", "api rest openapi swagger developers"),
]


def _score(q: str, title: str, words: str = "") -> int:
    if not q:
        return 1
    t, w = title.lower(), words.lower()
    s = 0
    for tok in q.split():
        if re.search(r"\b" + re.escape(tok), t):
            s += 3
        elif tok in t:
            s += 2
        elif re.search(r"\b" + re.escape(tok), w):
            s += 1
        else:
            return 0
    return s


def _rank(q, rows, limit):
    scored = [(_score(q, r["title"], r.pop("_words", "")), i, r) for i, r in enumerate(rows)]
    return [r for s, i, r in sorted((x for x in scored if x[0] > 0), key=lambda x: (-x[0], x[1]))][:limit]


def search(actor, q: str, event_slug: str | None = None) -> dict:
    q = " ".join((q or "").lower().split())[:120]
    ev = Event.objects.filter(slug=event_slug).first() if event_slug else None
    groups = []

    org_events = []
    if actor.user:
        org_events = list(Event.objects.filter(roles__user=actor.user, roles__role="organizer").distinct()) \
            if not actor.is_admin else list(Event.objects.all())
    focus = [ev] if ev and (actor.is_admin or ev in org_events) else org_events[:3]

    pages, actions = [], []
    for e in focus:
        for title, suffix, ic, words in ORG_PAGES:
            pages.append({"title": title, "sub": e.name, "url": f"/o/{e.slug}{suffix}", "icon": ic, "_words": words})
        for title, suffix, ic, words in ORG_ACTIONS:
            actions.append({"title": title, "sub": e.name, "url": f"/o/{e.slug}{suffix}", "icon": ic, "_words": words})
    if actor.user:
        for r in EventRole.objects.filter(user=actor.user, role="judge").select_related("event"):
            pages.append({"title": "My judging batch", "sub": r.event.name, "url": f"/j/{r.event.slug}", "icon": "gavel",
                          "_words": "review score batch queue my assignments judge console"})
        pages.append({"title": "My hackathons", "sub": "teams, submissions, scorecards", "url": "/me", "icon": "user",
                      "_words": "my team submission scorecard feedback certificate"})
    for title, url, ic, words in PUBLIC_PAGES:
        pages.append({"title": title, "sub": "", "url": url, "icon": ic, "_words": words})
    groups.append({"title": "Go to", "items": _rank(q, pages, 7 if q else 6)})
    if focus:
        groups.append({"title": "Do", "items": _rank(q, actions, 4)})

    if q:
        pq = public_projects(ev).filter(Q(title__icontains=q) | Q(tagline__icontains=q) | Q(team__name__icontains=q))
        groups.append({"title": "Projects", "items": [
            {"title": p.title, "sub": f"{p.team.name} · {p.track.name if p.track else ''}", "url": f"/p/{p.pk}", "icon": "rocket"}
            for p in pq.order_by("title")[:6]]})
        people = []
        for e in focus:
            for r in (EventRole.objects.filter(event=e, role="judge")
                      .filter(Q(user__name__icontains=q) | Q(user__email__icontains=q) | Q(ref__icontains=q))
                      .select_related("user")[:5]):
                people.append({"title": r.user.name or r.user.email, "sub": f"judge {r.ref} · {e.name}",
                               "url": f"/o/{e.slug}/judges#{r.ref}", "icon": "gavel"})
            for t in Team.objects.filter(event=e, name__icontains=q)[:4]:
                people.append({"title": t.name, "sub": f"team · {e.name}", "url": f"/o/{e.slug}/participants#{t.ref}",
                               "icon": "users"})
        groups.append({"title": "People and teams", "items": people[:7]})
    return {"query": q, "groups": [g for g in groups if g["items"]]}
