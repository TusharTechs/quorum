"""Organizer console. Every view requires the organizer role of the event in the URL
(checked in `_org`), and every state change goes through a service that audits it."""

from __future__ import annotations

import json

from django.contrib import messages
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import redirect, render

from quorum.api.common import get_event
from quorum.audit import service as audit
from quorum.audit.models import AuditCheckpoint, AuditEvent
from quorum.core.clock import now
from quorum.core.models import SigningKey
from quorum.events import organize
from quorum.events.models import EligibilityItem, Event, EventRole, Project, TeamMember
from quorum.integrations.models import ImportJob, WebhookEndpoint
from quorum.judging import feedback as fb
from quorum.judging import method as methods
from quorum.judging import ops
from quorum.judging import pairwise
from quorum.judging.models import Conflict as JConflict
from quorum.judging.models import Review
from quorum.policy.decorators import policy
from quorum.policy.errors import NotFound, PolicyError
from quorum.results import decide
from quorum.results.models import FeaturedQuote, RankingRun, TiebreakRound
from quorum.results.service import compute_run, latest_run
from quorum.voting import services as voting
from quorum.voting.models import IntegrityFlag


def _org(request, slug) -> Event:
    ev = get_event(slug)
    request.actor.require_organizer(ev)
    return ev


def _err(request, e: PolicyError):
    messages.error(request, e.detail)


TAB_TITLES = {"overview": "Overview", "setup": "Setup", "participants": "Participants", "judges": "Judges",
              "ops": "Judging ops", "results": "Results", "feedback": "Feedback", "voting": "Voting",
              "audit": "Audit and signed records", "data": "Data in and out"}


def _ctx(ev, tab, **kw):
    rail = {"voting": IntegrityFlag.objects.filter(event=ev, status="open").count(),
            "feedback": Review.objects.filter(event=ev, status="submitted", moderation="pending").count()}
    return {"ev": ev, "tab": tab, "tab_title": kw.pop("tab_title", TAB_TITLES.get(tab, "")), "nav": "organize",
            "rail": rail, **kw}


# --------------------------------------------------------------------------- home / create

@policy("authenticated")
def org_home(request):
    if request.actor.is_admin:
        events = Event.objects.all()
    else:
        events = Event.objects.filter(roles__user=request.user, roles__role="organizer")
    events = events.annotate(n_projects=Count("projects", filter=Q(projects__status="submitted"), distinct=True))
    return render(request, "org/home.html", {"events": events, "templates": organize.TEMPLATES, "nav": "organize",
                                             "can_create": request.actor.is_admin or request.user.event_roles.filter(role="organizer").exists()})


@policy("authenticated")
def org_new(request):
    if request.method == "POST":
        try:
            src = request.POST.get("clone_from")
            if src:
                ev = organize.clone_event(request.actor, get_event(src), request.POST.get("name", ""),
                                          int(request.POST.get("shift_days") or 30))
            else:
                ev = organize.create_event(request.actor, request.POST.dict(), request.POST.get("template", "raptors"))
            messages.success(request, f"{ev.name} created. Review the setup, then publish to lock the method.")
            return redirect(f"/o/{ev.slug}/setup")
        except PolicyError as e:
            _err(request, e)
    return redirect("/o")


# --------------------------------------------------------------------------- overview

@policy("authenticated")
def overview(request, slug):
    ev = _org(request, slug)
    progress = ops.judge_progress(ev)
    cov = ops.coverage(ev)
    fcov = ops.feedback_coverage(ev)
    run = latest_run(ev)
    out = run.output if run else {}
    ties = decide.boundary_ties(run, ev.prize_positions) if run and out.get("status") == "ok" else []
    flags = IntegrityFlag.objects.filter(event=ev, status="open").count()
    pending_mod = Review.objects.filter(event=ev, status="submitted", moderation="pending").count()
    stalled = [p for p in progress if p["status"] in ("stalled", "behind")]
    decisions = []
    if cov["below"]:
        decisions.append(("warn", f"{cov['below']} project(s) are below {cov['target']} reviews with nothing pending.",
                          f"/o/{ev.slug}/ops", "Fill coverage gaps"))
    if stalled:
        decisions.append(("warn", f"{len(stalled)} judge(s) are stalled or behind pace.", f"/o/{ev.slug}/ops",
                          "Rebalance or nudge"))
    if out.get("signal", {}).get("verdict") == "no_signal":
        decisions.append(("bad", "The judges' scores show no detectable agreement: the ranking cannot be "
                                 "distinguished from chance.", f"/o/{ev.slug}/results", "See the signal check"))
    for t in ties:
        decisions.append(("tie", f"Prize position {t['boundary']} falls inside a statistical tie "
                                 f"({len(t['projects'])} projects).", f"/o/{ev.slug}/results#ties", "Open a tie-break"))
    if fcov["none"]:
        decisions.append(("warn", f"{len(fcov['none'])} team(s) would receive no written feedback.",
                          f"/o/{ev.slug}/feedback", "See feedback coverage"))
    if flags:
        decisions.append(("warn", f"{flags} voting integrity flag(s) await review.", f"/o/{ev.slug}/voting", "Review votes"))
    if pending_mod:
        decisions.append(("", f"{pending_mod} feedback item(s) await moderation.", f"/o/{ev.slug}/feedback", "Moderate"))
    dup = EligibilityItem.objects.filter(event=ev).count()
    from quorum.web.views_public import _phase_steps

    return render(request, "org/overview.html", _ctx(
        ev, "overview", steps=_phase_steps(ev), progress=progress, cov=cov, fcov=fcov, run=run, out=out,
        decisions=decisions, flags=flags, dup=dup, method=methods.current(ev),
        n_projects=Project.objects.filter(event=ev, status="submitted", duplicate_of__isnull=True).count(),
        n_teams=ev.teams.count(), n_judges=len(progress),
        n_reviews=Review.objects.filter(event=ev, status="submitted").count(),
        audit_head=audit.head(ev),
    ))


# --------------------------------------------------------------------------- setup

@policy("authenticated")
def setup(request, slug):
    ev = _org(request, slug)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "event":
                organize.update_event(request.actor, ev, request.POST.dict())
                messages.success(request, "Event saved.")
            elif action == "rubric":
                reason = request.POST.get("override_reason", "").strip()
                rows = []
                i = 0
                while f"name_{i}" in request.POST:
                    rows.append({"key": request.POST.get(f"key_{i}"), "name": request.POST.get(f"name_{i}"),
                                 "description": request.POST.get(f"desc_{i}"),
                                 "weight_pct": request.POST.get(f"weight_{i}") or 0,
                                 "scale_max": request.POST.get(f"scale_{i}") or 5})
                    i += 1
                organize.save_criteria(request.actor, ev, rows, override_reason=reason or None)
                messages.success(request, "Rubric saved." if not reason else
                                 "Method overridden: new version locked. The results page will say so permanently.")
            elif action == "track":
                organize.add_track(request.actor, ev, request.POST.get("track_name", ""))
            elif action == "prize":
                organize.add_prize(request.actor, ev, request.POST.get("prize_name", ""), request.POST.get("places"),
                                   request.POST.get("kind", "judged"), request.POST.get("value_text", ""),
                                   per_track=request.POST.get("per_track") == "on")
            elif action == "question":
                organize.add_question(request.actor, ev, request.POST.get("label", ""), request.POST.get("kind", "text"),
                                      request.POST.get("required") == "on", request.POST.get("options", ""))
            elif action == "publish":
                organize.open_registration(request.actor, ev)
                messages.success(request, "Published. The judging method is locked and its hash is public.")
            elif action == "phase":
                organize.set_phase(request.actor, ev, request.POST.get("phase"))
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/setup")
    m = methods.current(ev)
    return render(request, "org/setup.html", _ctx(ev, "setup", method=m, locked=bool(m and m.locked_at),
                                                  criteria=ev.criteria.order_by("position"), tracks=ev.tracks.all(),
                                                  prizes=ev.prizes.all(), questions=ev.questions.all(),
                                                  templates=organize.TEMPLATES))


# --------------------------------------------------------------------------- participants

@policy("authenticated")
def participants(request, slug):
    ev = _org(request, slug)
    if request.method == "POST":
        item = EligibilityItem.objects.filter(event=ev, pk=request.POST.get("item")).first()
        if item:
            item.status = request.POST.get("status", "accepted")
            item.resolution_note = request.POST.get("note", "")[:300]
            item.resolved_by = request.user
            item.resolved_at = now()
            item.save()
            audit.record("ELIGIBILITY_RESOLVED", f"Eligibility item ({item.kind}) {item.status}", event=ev,
                         actor=request.actor, actor_role="organizer", target=item.project)
        return redirect(f"/o/{ev.slug}/participants")
    projects = Project.objects.filter(event=ev).select_related("team", "track", "duplicate_of").annotate(
        n_members=Count("team__members", distinct=True)).order_by("ref")
    return render(request, "org/participants.html", _ctx(
        ev, "participants", projects=projects, items=EligibilityItem.objects.filter(event=ev).select_related("project"),
        n_people=TeamMember.objects.filter(event=ev).count()))


# --------------------------------------------------------------------------- judges

@policy("authenticated")
def judges(request, slug):
    ev = _org(request, slug)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "invite":
                organize.invite_judge(request.actor, ev, request.POST.get("email", ""), request.POST.get("name", ""),
                                      request.POST.getlist("tracks"), request.POST.get("capacity") or None)
                messages.success(request, "Invitation sent (see the mail catcher offline).")
            elif action == "availability":
                role = EventRole.objects.get(event=ev, role="judge", pk=request.POST["role"])
                ops.set_availability(ev, request.actor, role, request.POST.get("available") == "1")
            elif action == "suggest_conflicts":
                n = ops.suggest_conflicts(ev)
                messages.success(request, f"{n} possible conflict(s) suggested from shared e-mail domains.")
            elif action == "conflict":
                c = JConflict.objects.get(event=ev, pk=request.POST["conflict"])
                c.status = request.POST.get("status", "confirmed")
                c.save(update_fields=["status"])
                audit.record("CONFLICT_" + c.status.upper(), f"Conflict {c.judge_role.ref} / {c.team.name} {c.status}",
                             event=ev, actor=request.actor, actor_role="organizer")
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/judges")
    run = latest_run(ev)
    stats = {j["judge"]: j for j in (run.output.get("judges") if run else []) or []}
    progress = ops.judge_progress(ev)
    for p in progress:
        p["stat"] = stats.get(p["ref"], {})
    return render(request, "org/judges.html", _ctx(ev, "judges", progress=progress, tracks=ev.tracks.all(),
                                                   conflicts=JConflict.objects.filter(event=ev).select_related("judge_role__user", "team"),
                                                   run=run, k=(run.output.get("k") if run else None)))


# --------------------------------------------------------------------------- ops (Pillar 1)

@policy("authenticated")
def ops_view(request, slug):
    ev = _org(request, slug)
    plan = request.session.get(f"plan_{ev.pk}")
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "plan_fill":
                plan = ops.plan_assignments(ev, seed=int(now().timestamp()) % 100000, kind="rebalance")
                request.session[f"plan_{ev.pk}"] = plan
            elif action == "plan_rebalance":
                ids = request.POST.getlist("role")
                if not ids:
                    ids = [str(p["role"].pk) for p in ops.judge_progress(ev) if p["status"] in ("stalled", "unavailable")]
                plan = ops.plan_rebalance(ev, ids)
                request.session[f"plan_{ev.pk}"] = plan
            elif action == "commit" and plan:
                n = ops.commit_plan(ev, request.actor, plan, source="rebalance" if plan.get("drop_pending_of") else "algorithm",
                                    batch_kind=plan.get("kind", "rebalance"))
                request.session.pop(f"plan_{ev.pk}", None)
                messages.success(request, f"{n} assignment(s) created and batch e-mails queued.")
            elif action == "discard":
                request.session.pop(f"plan_{ev.pk}", None)
            elif action == "nudge":
                ids = request.POST.getlist("role") or None
                n = ops.nudge_now(ev, request.actor, ids, request.POST.get("message", ""))
                messages.success(request, f"Reminder queued for {n} judge(s).")
            elif action == "send_batches":
                n = ops.send_batches(ev, request.actor)
                messages.success(request, f"{n} batch e-mail(s) queued.")
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/ops")
    progress = ops.judge_progress(ev)
    bd = ops.burndown(ev)
    fc = ops.forecast(ev, progress)
    order = {"stalled": 0, "unavailable": 1, "behind": 2, "on_track": 3, "unassigned": 4, "done": 5}
    progress.sort(key=lambda p: (order.get(p["status"], 9), -p["pending"], p["ref"]))
    status_counts = {}
    for p in progress:
        status_counts[p["status"]] = status_counts.get(p["status"], 0) + 1
    from quorum.judging.models import Reminder

    return render(request, "org/ops.html", _ctx(
        ev, "ops", progress=progress, bd=bd, fc=fc, cov=ops.coverage(ev), fcov=ops.feedback_coverage(ev),
        plan=plan, status_counts=status_counts, reminders=Reminder.objects.filter(event=ev),
        window_left=max((ev.judging_closes_at - now()).total_seconds() / 86400, 0)))


# --------------------------------------------------------------------------- results (Pillars 2, 3)

@policy("authenticated")
def results(request, slug):
    ev = _org(request, slug)
    focus_plan = request.session.get(f"focus_{ev.pk}")
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "compute":
                compute_run(ev, request.actor, kind=RankingRun.Kind.PREVIEW, heavy=True)
                messages.success(request, "Ranking recomputed.")
            elif action == "plan_focus":
                budget = request.POST.get("budget")
                focus_plan = decide.plan_focus(ev, int(budget) if budget else None, seed=int(now().timestamp()) % 1000)
                request.session[f"focus_{ev.pk}"] = focus_plan
            elif action == "commit_focus" and focus_plan:
                decide.commit_focus(ev, request.actor, focus_plan)
                request.session.pop(f"focus_{ev.pk}", None)
                messages.success(request, f"Focus round committed: {len(focus_plan['rows'])} targeted review(s) sent.")
            elif action == "discard_focus":
                request.session.pop(f"focus_{ev.pk}", None)
            elif action == "open_tiebreak":
                refs = request.POST.getlist("project")
                tb = decide.open_tiebreak(ev, request.actor, refs, int(request.POST.get("boundary") or 1))
                messages.success(request, f"Tie-break opened with a panel of {tb.judge_roles.count()} judges.")
            elif action == "resolve_tiebreak":
                tb = TiebreakRound.objects.get(event=ev, pk=request.POST["tiebreak"])
                order = [x for x in request.POST.get("order", "").split(",") if x] or None
                decide.resolve_tiebreak(tb, request.actor, order, request.POST.get("note", ""))
                messages.success(request, "Tie-break resolved.")
            elif action == "lock":
                decide.lock_results(ev, request.actor)
                messages.success(request, "Results locked and signed.")
            elif action == "publish":
                decide.publish_results(ev, request.actor)
                messages.success(request, "Results published, feedback released, certificates issued.")
            elif action == "reopen":
                decide.reopen_results(ev, request.actor, request.POST.get("reason", ""))
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/results")
    run = latest_run(ev)
    if run is None or "loo" not in run.output:
        run = compute_run(ev, request.actor, kind=RankingRun.Kind.PREVIEW, heavy=True)
    out = run.output
    titles = {p.ref: p for p in Project.objects.filter(event=ev).select_related("team", "track")}
    ties = decide.boundary_ties(run, ev.prize_positions) if out.get("status") == "ok" else []
    tiebreaks = []
    for tb in ev.tiebreaks.order_by("-created_at"):
        st = decide.tiebreak_state(tb) if tb.status == "open" else tb.result
        tiebreaks.append({"tb": tb, "state": st, "projects": list(tb.projects.all()),
                          "panel": list(tb.judge_roles.select_related("user"))})
    pub = getattr(ev, "publication", None)
    loo = out.get("loo") or {}
    pivotal = sorted([(j, d) for j, d in loo.items() if d.get("prize_set_changes") or d.get("top1_changes")],
                     key=lambda t: -t[1]["max_move"])
    return render(request, "org/results.html", _ctx(
        ev, "results", run=run, out=out, entries=out.get("entries", []), titles=titles, ties=ties,
        tiebreaks=tiebreaks, focus_plan=focus_plan, pub=pub, pivotal=pivotal, n=len(out.get("entries", [])),
        runs=RankingRun.objects.filter(event=ev).order_by("-created_at")[:8], sens=out.get("sensitivity"),
        method=methods.current(ev)))


@policy("authenticated")
def pairwise_view(request, slug):
    ev = _org(request, slug)
    if request.method == "POST":
        try:
            t = pairwise.set_mode(ev, request.actor, pairwise.get_track(ev, request.POST.get("track", "")),
                                  request.POST.get("enabled") == "1")
            messages.success(request, f"Comparative judging {'switched on' if t.pairwise else 'switched off'} "
                                      f"for {t.name}.")
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/results/pairwise")
    boards = {b["track"]["ref"]: b for b in pairwise.event_board(ev)}
    tracks = []
    for t in ev.tracks.all():
        judges = list(EventRole.objects.filter(event=ev, role="judge", judge_tracks__track=t).select_related("user"))
        ready = sum(1 for r in judges if len(pairwise.pool(r, t)) >= pairwise.MIN_POOL)
        tracks.append({"t": t, "board": boards.get(t.ref), "judges": len(judges), "ready": ready})
    return render(request, "org/pairwise.html", _ctx(ev, "results", tracks=tracks, target=pairwise.TARGET,
                                                     min_pool=pairwise.MIN_POOL,
                                                     locked=hasattr(ev, "publication")))


@policy("authenticated")
def explain(request, slug, ref):
    ev = _org(request, slug)
    run = latest_run(ev)
    if not run:
        raise NotFound("No ranking yet.")
    out = run.output
    entry = next((e for e in out.get("entries", []) if e["project"] == ref), None)
    if not entry:
        raise NotFound("That project is not in the ranking.")
    p = Project.objects.select_related("team", "track").get(event=ev, ref=ref)
    names = {r.ref: r.user.name for r in EventRole.objects.filter(event=ev, role="judge").select_related("user")}
    expl = out["explanations"].get(ref)
    reviews = (Review.objects.filter(event=ev, project=p, status="submitted")
               .select_related("judge_role__user").prefetch_related("scores__criterion"))
    judges = {j["judge"]: j for j in out.get("judges", [])}
    rows = []
    for r in reviews:
        rows.append({"r": r, "ref": r.judge_role.ref, "scores": {s.criterion.key: float(s.value) for s in r.scores.all()},
                     "stat": judges.get(r.judge_role.ref, {}), "source": r.source_scores})
    idx = next(i for i, e in enumerate(out["entries"]) if e["project"] == ref)
    neighbours = out["entries"][max(0, idx - 2): idx + 3]
    return render(request, "org/explain.html", _ctx(
        ev, "results", p=p, e=entry, expl=expl, names=names, rows=rows, criteria=ev.criteria.order_by("position"),
        neighbours=neighbours, titles={x.ref: x for x in Project.objects.filter(event=ev)}, run=run,
        n=len(out["entries"]), k=out.get("k"), sigma2=out.get("sigma2")))


@policy("authenticated")
def judge_stats(request, slug):
    ev = _org(request, slug)
    run = latest_run(ev)
    out = run.output if run else {}
    names = {r.ref: r for r in EventRole.objects.filter(event=ev, role="judge").select_related("user")}
    return render(request, "org/judge_stats.html", _ctx(ev, "judges", out=out, judges=out.get("judges", []),
                                                        names=names, run=run))


# --------------------------------------------------------------------------- feedback (Pillar 4)

@policy("authenticated")
def feedback_view(request, slug):
    ev = _org(request, slug)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("approve", "edit", "hide"):
                rv = Review.objects.select_related("project").get(event=ev, pk=request.POST["review"])
                fb.moderate(request.actor, rv, action, request.POST.get("text", ""), request.POST.get("note", ""))
            elif action == "approve_all":
                n = fb.approve_all_pending(request.actor, ev)
                messages.success(request, f"{n} item(s) approved.")
            elif action == "release":
                n = fb.release_feedback(ev, request.actor)
                messages.success(request, f"Feedback released; {n} participant(s) e-mailed a scorecard link.")
            elif action == "feature":
                rv = Review.objects.select_related("project").get(event=ev, pk=request.POST["review"])
                FeaturedQuote.objects.update_or_create(event=ev, project=rv.project, defaults={
                    "review": rv, "text": request.POST.get("text", rv.feedback_for_team)[:400],
                    "attribution": "named" if rv.quotable == "named" else "anonymous"})
                audit.record("QUOTE_FEATURED", f"Judge quote featured for “{rv.project.title}”", event=ev,
                             actor=request.actor, actor_role="organizer", target=rv.project)
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/feedback" + (f"?status={request.GET.get('status')}" if request.GET.get("status") else ""))
    status = request.GET.get("status", "pending")
    qs = Review.objects.filter(event=ev, status="submitted").exclude(feedback_to_team="").select_related(
        "project", "judge_role__user").order_by("project__ref")
    if status != "all":
        qs = qs.filter(moderation=status)
    return render(request, "org/feedback.html", _ctx(
        ev, "feedback", reviews=qs[:200], status=status, fcov=ops.feedback_coverage(ev), rows=fb.coverage_rows(ev),
        counts={s: Review.objects.filter(event=ev, status="submitted", moderation=s).exclude(feedback_to_team="").count()
                for s in ("pending", "approved", "edited", "hidden")},
        quotes=FeaturedQuote.objects.filter(event=ev).select_related("project")))


# --------------------------------------------------------------------------- voting

@policy("authenticated")
def voting_view(request, slug):
    ev = _org(request, slug)
    codes = None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in ("keep", "void", "dismiss"):
                n = voting.resolve_flags(request.actor, ev, request.POST.getlist("flag"), action, request.POST.get("note", ""))
                messages.success(request, f"{n} flag(s) resolved ({action}).")
            elif action == "scan":
                n = voting.scan_flags(ev)
                messages.success(request, f"Scan complete: {n} new flag(s).")
            elif action == "codes":
                codes = voting.issue_codes(request.actor, ev, int(request.POST.get("n") or 20), request.POST.get("batch", ""))
                request.session[f"codes_{ev.pk}"] = codes
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/voting")
    codes = request.session.pop(f"codes_{ev.pk}", None)
    voting.scan_flags(ev)
    return render(request, "org/voting.html", _ctx(
        ev, "voting", tallies=voting.tallies(ev), bias=voting.position_bias(ev), codes=codes,
        flags=IntegrityFlag.objects.filter(event=ev).order_by("status", "-created_at")[:200],
        open_flags=IntegrityFlag.objects.filter(event=ev, status="open").count(),
        n_votes=ev.votes.count(), n_void=ev.votes.filter(status="void").count(), n_voters=ev.voters.count()))


# --------------------------------------------------------------------------- audit

@policy("authenticated")
def audit_view(request, slug):
    ev = _org(request, slug)
    verify = None
    if request.method == "POST" and request.POST.get("action") == "revoke":
        from quorum.audit.certificates import revoke
        from quorum.audit.models import Certificate

        from django.core.exceptions import ValidationError

        try:
            c = Certificate.objects.filter(event=ev, pk=request.POST.get("certificate")).first()
        except ValidationError:
            c = None
        if not c:
            raise NotFound("No such certificate in this event.")
        try:
            revoke(ev, request.actor, c, request.POST.get("reason", ""))
            messages.success(request, f"{c.serial} revoked. The revocation list and the record page show it.")
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/audit#records")
    if request.method == "POST":
        verify = audit.verify_chain(ev)
        audit.record("AUDIT_VERIFIED", f"Audit chain verified: {'intact' if verify['ok'] else 'BROKEN at seq ' + str(verify.get('broken_seq'))}"
                     f" ({verify['checked']} entries)", event=ev, actor=request.actor, actor_role="organizer")
    qs = AuditEvent.objects.filter(event_id=ev.pk).order_by("-seq")
    action = request.GET.get("action", "")
    actor_q = request.GET.get("actor", "")
    q = request.GET.get("q", "")
    if action:
        qs = qs.filter(action__startswith=action)
    if actor_q:
        qs = qs.filter(actor_label__icontains=actor_q)
    if q:
        qs = qs.filter(Q(summary__icontains=q) | Q(target_id__icontains=q))
    actions = (AuditEvent.objects.filter(event_id=ev.pk).values_list("action", flat=True).distinct().order_by("action"))
    return render(request, "org/audit.html", _ctx(
        ev, "audit", entries=qs[:300], total=AuditEvent.objects.filter(event_id=ev.pk).count(), actions=actions,
        action=action, actor_q=actor_q, q=q, verify=verify, head=audit.head(ev),
        checkpoints=AuditCheckpoint.objects.filter(event_id=ev.pk).order_by("-seq")[:10],
        certs=ev.certificates.select_related("user").order_by("kind", "serial"),
        retired=set(SigningKey.objects.filter(retired_at__isnull=False).values_list("key_id", flat=True))))


# --------------------------------------------------------------------------- data (exports, imports, webhooks)

@policy("authenticated")
def data_view(request, slug):
    ev = _org(request, slug)
    from quorum.integrations import importers

    job = None
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "upload" and request.FILES.get("file"):
                job = importers.create_job(request.actor, ev, request.FILES["file"], request.POST.get("source", "generic_csv"))
                return redirect(f"/o/{ev.slug}/data?job={job.pk}")
            elif action == "mapping":
                job = ImportJob.objects.get(event=ev, pk=request.POST["job"])
                mapping = {k[4:]: v for k, v in request.POST.items() if k.startswith("map_") and v}
                importers.dry_run(job, mapping)
                return redirect(f"/o/{ev.slug}/data?job={job.pk}")
            elif action == "commit":
                job = ImportJob.objects.get(event=ev, pk=request.POST["job"])
                importers.commit(request.actor, job)
                messages.success(request, f"Import committed: {job.report.get('summary', '')}")
            elif action == "webhook":
                import secrets

                WebhookEndpoint.objects.create(event=ev, url=request.POST["url"], secret=secrets.token_hex(24),
                                               topics=[t.strip() for t in request.POST.get("topics", "").split(",") if t.strip()],
                                               created_by=request.user)
                audit.record("WEBHOOK_ADDED", f"Webhook added: {request.POST['url']}", event=ev, actor=request.actor,
                             actor_role="organizer")
            elif action == "webhook_delete":
                WebhookEndpoint.objects.filter(event=ev, pk=request.POST["hook"]).update(active=False)
        except PolicyError as e:
            _err(request, e)
        return redirect(f"/o/{ev.slug}/data")
    if request.GET.get("job"):
        job = ImportJob.objects.filter(event=ev, pk=request.GET["job"]).first()
    from quorum.integrations.exports import CSV_EXPORTS

    return render(request, "org/data.html", _ctx(
        ev, "data", exports=sorted(CSV_EXPORTS), job=job, jobs=ImportJob.objects.filter(event=ev).order_by("-created_at")[:10],
        hooks=WebhookEndpoint.objects.filter(event=ev, active=True), sources=ImportJob.Source.choices,
        fields=importers.TARGET_FIELDS))


@policy("authenticated")
def bundle_download(request, slug):
    ev = _org(request, slug)
    from quorum.integrations.bundle import export_bundle

    b = export_bundle(ev)
    audit.record("EXPORT_DOWNLOADED", "Full event bundle exported", event=ev, actor=request.actor, actor_role="organizer",
                 data={"kind": "bundle", "runs": len(b["ranking_runs"])})
    resp = HttpResponse(json.dumps(b, indent=1, sort_keys=True, default=str), content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="{ev.ref}-bundle.json"'
    return resp
