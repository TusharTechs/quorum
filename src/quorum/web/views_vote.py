"""Public ballot, public results, certificates and the verify page."""

from __future__ import annotations

import json

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render

from quorum.api.common import get_event
from quorum.audit import certificates
from quorum.audit.models import AuditCheckpoint, Certificate
from quorum.core.clock import now
from quorum.events.models import Project
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound, PolicyError
from quorum.policy.repos import public_projects
from quorum.results.models import FeaturedQuote
from quorum.voting import services as voting


@policy("public")
def ballot(request, slug):
    ev = get_event(slug)
    if ev.voting_mode == "off":
        raise NotFound("This event has no community vote.")
    voter = voting.current_voter(request, ev)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "email":
                voting.start_email_verification(request, ev, request.POST.get("email", ""))
                messages.success(request, "Check your inbox for a link to confirm your vote. "
                                          "(Offline demo: the mail catcher at localhost:8025.)")
                return redirect(f"/e/{ev.slug}/vote")
            if action == "code":
                v = voting.redeem_code(request, ev, request.POST.get("code", ""))
                resp = redirect(f"/e/{ev.slug}/vote")
                resp.set_signed_cookie(voting.cookie_name(ev), str(v.pk), salt="quorum-voter", max_age=60 * 60 * 24 * 30,
                                       httponly=True, samesite="Lax")
                return resp
            if action == "open":
                v = voting.ensure_open_voter(request, ev)
                resp = redirect(f"/e/{ev.slug}/vote")
                resp.set_signed_cookie(voting.cookie_name(ev), str(v.pk), salt="quorum-voter", max_age=60 * 60 * 24 * 30,
                                       httponly=True, samesite="Lax")
                return resp
            if action == "vote":
                if ev.voting_mode == "authenticated":
                    voter = voting.ensure_user_voter(request, ev)
                if not voter:
                    raise Forbidden("Verify your identity before voting.", code="voter_unverified")
                p = Project.objects.filter(event=ev, pk=request.POST.get("project")).first()
                if not p:
                    raise NotFound("Unknown project.")
                voting.cast_vote(request, ev, voter, p, request.POST.get("ballot", ""),
                                 int(request.POST.get("weight") or 1))
                if request.headers.get("HX-Request"):
                    return JsonResponse({"ok": True})
                messages.success(request, f"Vote recorded for “{p.title}”.")
        except PolicyError as e:
            if request.headers.get("HX-Request"):
                return JsonResponse({"ok": False, "error": e.code, "detail": e.detail}, status=e.status)
            messages.error(request, e.detail)
        return redirect(f"/e/{ev.slug}/vote")
    if ev.voting_mode == "authenticated" and request.user.is_authenticated:
        voter = voting.ensure_user_voter(request, ev)
    projects = list(public_projects(ev))
    ordered = voting.ballot_order(ev, voter, projects) if voter else sorted(projects, key=lambda p: p.title)
    voted = set(voter.votes.filter(status="valid").values_list("project_id", flat=True)) if voter else set()
    remaining = (ev.votes_per_voter - len(voted)) if voter else ev.votes_per_voter
    credits = voting.credits_available(ev, voter) if (voter and ev.quadratic_voting) else None
    show_tally = voting.tallies_visible_to(request.actor, ev)
    return render(request, "public/ballot.html", {
        "ev": ev, "voter": voter, "projects": ordered, "voted": voted, "remaining": remaining, "credits": credits,
        "token": voting.ballot_token(ev, voter) if voter else "", "open": ev.voting_open(now()),
        "tallies": voting.tallies(ev) if show_tally else None, "nav": "events", "now": now(),
    })


@policy("public")
def public_results(request, slug):
    ev = get_event(slug)
    pub = getattr(ev, "publication", None)
    if not (ev.published and pub) and not request.actor.is_organizer(ev):
        raise Forbidden("Results are hidden until the organizers publish them.", code="results_not_published")
    if not pub:
        return redirect(f"/o/{ev.slug}/results")
    out = pub.run.output
    entries = {e["project"]: e for e in out.get("entries", [])}
    projects = {p.ref: p for p in Project.objects.filter(event=ev).select_related("team", "track")}
    order = pub.final_order or [e["project"] for e in out.get("entries", [])]
    rows = [{"p": projects[r], "e": entries[r], "place": i + 1} for i, r in enumerate(order) if r in projects and r in entries]
    quotes = {q.project.ref: q for q in FeaturedQuote.objects.filter(event=ev).select_related("project")}
    track_winners = {}
    for r in rows:
        t = r["p"].track.name if r["p"].track else "—"
        track_winners.setdefault(t, r)
    community = voting.tallies(ev)[:3] if voting.tallies_visible_to(request.actor, ev) else []
    cp = AuditCheckpoint.objects.filter(event_id=ev.pk).order_by("-seq").first()
    tb_refs = {r for t in (pub.tiebreak_results or []) for r in (t.get("order") or [])}
    titles = {ref: p.title for ref, p in projects.items()}
    return render(request, "public/results.html", {
        "ev": ev, "pub": pub, "rows": rows, "podium": rows[: ev.prize_positions], "quotes": quotes,
        "track_winners": track_winners, "signal": out.get("signal", {}), "community": community, "checkpoint": cp,
        "method": ev.methods.order_by("-version").first(), "n": len(rows), "nav": "events",
        "tiebreaks": pub.tiebreak_results or [], "tb_refs": tb_refs, "titles": titles,
    })


@policy("public")
def certificate(request, cid):
    c = Certificate.objects.select_related("event", "user").filter(pk=cid).first()
    if not c:
        raise NotFound("No such certificate.")
    data = json.loads(c.payload)
    return render(request, "public/certificate.html", {"c": c, "data": data, "nav": "verify",
                                                       "bundle": json.dumps({"payload": c.payload, "signature": c.signature,
                                                                             "key_id": c.key_id})})


@policy("public")
def verify_page(request):
    result = None
    if request.method == "POST":
        try:
            blob = json.loads(request.POST.get("record", ""))
            result = certificates.check(blob["payload"], blob["signature"], blob["key_id"])
            result["data"] = json.loads(blob["payload"])
        except (ValueError, KeyError, TypeError):
            result = {"valid": False, "error": "Paste the JSON record exactly as issued."}
    return render(request, "public/verify.html", {"result": result, "nav": "verify"})
