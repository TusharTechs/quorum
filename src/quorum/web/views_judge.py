"""Judge pages: inbox, review console (keyboard-first, autosave), recusal, tie-break
comparisons, and the signed evaluation protocol."""

from __future__ import annotations

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render

from quorum.api.common import get_event
from quorum.audit.certificates import issue_judge_protocol
from quorum.audit.models import Certificate
from quorum.core.clock import now
from quorum.events.models import EventRole, Project
from quorum.judging import ops
from quorum.judging import pairwise
from quorum.judging import services as judging
from quorum.judging.models import Assignment, Review
from quorum.policy.decorators import policy
from quorum.policy.errors import Forbidden, NotFound, PolicyError
from quorum.policy.repos import judge_assignment
from quorum.results import decide
from quorum.results.models import TiebreakRound


@policy("authenticated")
def judge_home(request):
    roles = EventRole.objects.filter(user=request.user, role="judge").select_related("event")
    if roles.count() == 1:
        return redirect(f"/j/{roles.first().event.slug}")
    return render(request, "judge/home.html", {"roles": roles, "nav": "judge"})


def _queue(role, event):
    items = list(Assignment.objects.filter(judge_role=role, event=event).exclude(status="reassigned")
                 .select_related("project", "project__track", "batch").order_by("status", "assigned_at"))
    reviews = {r.assignment_id: r for r in Review.objects.filter(judge_role=role, event=event)}
    for a in items:
        a.review_obj = reviews.get(a.pk)
    pending = [a for a in items if a.status in ("pending", "in_progress")]
    done = [a for a in items if a.status == "submitted"]
    recused = [a for a in items if a.status == "recused"]
    return pending, done, recused


@policy("authenticated")
def inbox(request, slug):
    ev = get_event(slug)
    role = request.actor.require_judge(ev)
    pending, done, recused = _queue(role, ev)
    tiebreaks = TiebreakRound.objects.filter(event=ev, judge_roles=role, status="open")
    tb_rows = []
    for tb in tiebreaks:
        pair, mine, total = decide.tiebreak_next_pair(tb, role)
        tb_rows.append({"tb": tb, "done": mine, "total": total, "next": pair})
    protocol = Certificate.objects.filter(event=ev, user=request.user, kind="judge_protocol").first()
    total = len(pending) + len(done)
    return render(request, "judge/inbox.html", {
        "ev": ev, "role": role, "pending": pending, "done": done, "recused": recused, "total": total,
        "tiebreaks": tb_rows, "comparative": pairwise.judge_tracks(ev, role), "min_pool": pairwise.MIN_POOL, "protocol": protocol, "tracks": [jt.track for jt in role.judge_tracks.select_related("track")],
        "window_open": ev.judging_opens_at <= now() < ev.judging_closes_at, "nav": "judge",
        "est_minutes": len(pending) * 8,
    })


@policy("authenticated")
def review(request, slug, aid):
    ev = get_event(slug)
    request.actor.require_judge(ev)
    a = judge_assignment(request.actor, aid)
    if a.event_id != ev.pk:
        raise Forbidden("This review is not assigned to you.")
    if request.method == "POST":
        action = request.POST.get("action", "draft")
        scores = {c.key: request.POST.get(f"c_{c.key}") for c in ev.criteria.all()}
        try:
            judging.save_review(request.actor, a, scores=scores, feedback=request.POST.get("feedback"),
                                note=request.POST.get("note"), quotable=request.POST.get("quotable"),
                                active_seconds=int(request.POST.get("active_seconds") or 0),
                                submit=action == "submit")
        except PolicyError as e:
            if action == "draft":
                return JsonResponse({"ok": False, "error": e.code, "detail": e.detail}, status=e.status)
            messages.error(request, e.detail)
            return redirect(f"/j/{ev.slug}/review/{a.pk}")
        if action == "draft":
            return JsonResponse({"ok": True})
        messages.success(request, f"Review of “{a.project.title}” submitted.")
        nxt = (Assignment.objects.filter(judge_role=a.judge_role, event=ev, status__in=["pending", "in_progress"])
               .order_by("assigned_at").first())
        return redirect(f"/j/{ev.slug}/review/{nxt.pk}" if nxt else f"/j/{ev.slug}")
    try:
        rv = judging.open_review(request.actor, a)
    except PolicyError as e:
        messages.error(request, e.detail)
        rv = Review.objects.filter(assignment=a).first()
    current = {s.criterion.key: int(s.value) if s.value == int(s.value) else float(s.value)
               for s in rv.scores.select_related("criterion")} if rv else {}
    pending, done, _ = _queue(a.judge_role, ev)
    order = [x.pk for x in pending + done]
    idx = order.index(a.pk) if a.pk in order else 0
    nxt = next((x for x in pending if x.pk != a.pk), None)
    p = a.project
    criteria = []
    for c in ev.criteria.order_by("position"):
        anchors = {int(k): v for k, v in (c.anchors or {}).items()}
        criteria.append({"c": c, "value": current.get(c.key), "levels": [
            {"v": v, "anchor": anchors.get(v, "")} for v in range(c.scale_min, c.scale_max + 1)]})
    return render(request, "judge/review.html", {
        "ev": ev, "a": a, "p": p, "rv": rv, "criteria": criteria, "images": p.images.all(),
        "answers": p.answers.select_related("question"), "position": idx + 1, "count": len(order),
        "next": nxt, "nav": "judge", "window_open": ev.judging_opens_at <= now() < ev.judging_closes_at,
    })


@policy("authenticated")
def recuse(request, slug, aid):
    ev = get_event(slug)
    request.actor.require_judge(ev)
    a = judge_assignment(request.actor, aid)
    if request.method == "POST":
        try:
            ops.recuse(request.actor, a, request.POST.get("note", ""))
            messages.success(request, "Recorded. The project has been re-queued to another judge.")
        except PolicyError as e:
            messages.error(request, e.detail)
    return redirect(f"/j/{ev.slug}")


@policy("authenticated")
def tiebreak(request, slug, tid):
    ev = get_event(slug)
    role = request.actor.require_judge(ev)
    tb = TiebreakRound.objects.filter(pk=tid, event=ev, judge_roles=role).first()
    if not tb:
        raise Forbidden("You are not on this tie-break panel.")
    if request.method == "POST":
        pa = Project.objects.filter(event=ev, ref=request.POST.get("a")).first()
        pb = Project.objects.filter(event=ev, ref=request.POST.get("b")).first()
        if not pa or not pb:
            raise NotFound("Unknown projects.")
        try:
            judging.record_comparison(request.actor, ev, role, pa, pb, request.POST.get("outcome"), tiebreak=tb,
                                      reason=request.POST.get("reason", ""))
        except PolicyError as e:
            messages.error(request, e.detail)
        return redirect(f"/j/{ev.slug}/tiebreak/{tb.pk}")
    pair, mine, total = decide.tiebreak_next_pair(tb, role)
    pa = pb = None
    if pair:
        pa = Project.objects.select_related("track", "team").get(event=ev, ref=pair[0])
        pb = Project.objects.select_related("track", "team").get(event=ev, ref=pair[1])
    return render(request, "judge/tiebreak.html", {"ev": ev, "tb": tb, "a": pa, "b": pb, "done": mine,
                                                   "total": total, "projects": tb.projects.all(), "nav": "judge"})


@policy("authenticated")
def compare(request, slug, track):
    ev = get_event(slug)
    role = request.actor.require_judge(ev)
    t = pairwise.get_track(ev, track)
    if not role.judge_tracks.filter(track=t).exists():
        raise Forbidden("You do not judge this track.")
    if request.method == "POST":
        pa = Project.objects.filter(event=ev, ref=request.POST.get("a")).select_related("track").first()
        pb = Project.objects.filter(event=ev, ref=request.POST.get("b")).select_related("track").first()
        if not pa or not pb:
            raise NotFound("Unknown projects.")
        try:
            judging.record_comparison(request.actor, ev, role, pa, pb, request.POST.get("outcome"),
                                      reason=request.POST.get("reason", ""),
                                      active_seconds=int(request.POST.get("active_seconds") or 0))
        except PolicyError as e:
            messages.error(request, e.detail)
        return redirect(f"/j/{ev.slug}/compare/{t.ref}")
    st = pairwise.next_for(role, t)
    pa = pb = None
    if st["pair"] and t.pairwise:
        pa = Project.objects.select_related("track", "team").get(event=ev, ref=st["pair"][0])
        pb = Project.objects.select_related("track", "team").get(event=ev, ref=st["pair"][1])
    return render(request, "judge/compare.html", {"ev": ev, "t": t, "st": st, "a": pa, "b": pb, "nav": "judge",
                                                  "window_open": ev.judging_opens_at <= now() < ev.judging_closes_at})


@policy("authenticated")
def protocol(request, slug):
    ev = get_event(slug)
    role = request.actor.require_judge(ev)
    cert = Certificate.objects.filter(event=ev, user=request.user, kind="judge_protocol").first()
    if request.method == "POST" and not cert:
        pending = Assignment.objects.filter(judge_role=role, status__in=["pending", "in_progress"]).count()
        if pending:
            messages.error(request, f"Finish your {pending} open review(s) first: the protocol documents completed work.")
        else:
            cert = issue_judge_protocol(ev, role)
    if cert:
        return redirect(f"/certificates/{cert.pk}")
    return redirect(f"/j/{ev.slug}")
