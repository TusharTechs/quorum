"""Ask Quorum: questions in plain language, answered only from the event's own data.

A question is matched to one of a fixed set of named *skills* by meaning (local sentence
embeddings over example phrasings; keyword overlap when the model is off). Names in the
question are matched to projects and judges. The skill then runs the same policy-checked
service code the pages use, and the answer says which skill ran and what it read.

This is deliberately not a generative model: an assistant for a judging platform must not
be able to make anything up. It answers, links to the page with the evidence, or says it
does not know.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

MATCH_AT = 0.52          # cosine to the closest example phrasing
KEYWORD_AT = 0.5         # share of a skill's keywords present (fallback)


@dataclass
class Skill:
    name: str
    roles: set
    examples: list
    keywords: str
    run: object = None
    needs: str = ""              # "project" or "judge" when the question must name one
    vectors: object = field(default=None, repr=False)


def _a(text, lines=(), how="", links=()):
    return {"text": text, "lines": list(lines), "how": how, "links": list(links)}


def _pct(x):
    return f"{round(100 * x)}%" if x is not None else "–"


# ------------------------------------------------------------------ organizer skills

def s_stalled(actor, ev, ent):
    from quorum.judging import ops

    prog = ops.judge_progress(ev)
    late = [p for p in prog if p["status"] in ("stalled", "behind")]
    unstarted = [p for p in prog if p["assigned"] and not p["submitted"]]
    if not late and not unstarted:
        return _a(f"All {len(prog)} judges are on pace.", how="judging ops: pace from each judge's submissions and last "
                  "activity", links=[("Judging ops", f"/o/{ev.slug}/ops", "activity")])
    lines = [f"{p['name']} ({p['ref']}): {p['status']}, {p['pending']} pending, idle {round(p['idle_hours'] or 0)} h"
             for p in late[:6]]
    lines += [f"{p['name']} ({p['ref']}): not started, {p['pending']} pending" for p in unstarted if p not in late][:3]
    return _a(f"{len(late)} judge(s) are stalled or behind pace" + (f"; {len(unstarted)} have not submitted anything."
              if unstarted else "."), lines,
              "judging ops: status from pace (reviews per day against days left) and hours since last activity",
              [("Rebalance or nudge", f"/o/{ev.slug}/ops", "refresh-cw")])


def s_coverage(actor, ev, ent):
    from quorum.judging import ops

    cov = ops.coverage(ev)
    short = [r for r in cov["rows"] if r["state"] in ("below", "pending")]
    lines = [f"{r['project'].title}: {r['submitted']} of {r['target']} reviews"
             + (f", {r['pending']} pending" if r["pending"] else ", nothing pending") for r in short[:8]]
    return _a(f"{cov['ok']} of {cov['total']} projects are at the review target of {cov['target']}; "
              f"{cov['below']} are short with nothing pending.", lines,
              "coverage: submitted and pending reviews per project against the method's reviews-per-project",
              [("Fill coverage gaps", f"/o/{ev.slug}/ops", "target")])


def s_ties(actor, ev, ent):
    from quorum.results import decide
    from quorum.results.service import latest_run

    run = latest_run(ev)
    if not run or run.output.get("status") != "ok":
        return _a("There is no ranking yet: not enough reviews.", how="latest ranking run")
    titles = _titles(ev)
    ties = decide.boundary_ties(run, ev.prize_positions)
    if not ties:
        return _a("No prize position falls inside a statistical tie.", how="tie groups of the latest run "
                  "(|difference| < 1.96 × SE)", links=[("Results", f"/o/{ev.slug}/results", "trophy")])
    lines = [f"Position {t['boundary']}: " + ", ".join(titles.get(r, r) for r in t["projects"]) for t in ties]
    head = (f"Yes. Prize position {ties[0]['boundary']} falls inside a statistical tie" if len(ties) == 1 else
            f"Yes. {len(ties)} prize positions fall inside statistical ties")
    return _a(f"{head}: the scores alone cannot order these projects.", lines, f"tie groups of run {run.output_hash[:10]} at the {ev.prize_positions} paid "
              "places; a pre-registered head-to-head round decides them",
              [("Open a tie-break", f"/o/{ev.slug}/results#ties", "scale")])


def s_signal(actor, ev, ent):
    from quorum.results.service import latest_run

    run = latest_run(ev)
    sig = (run.output.get("signal") if run else None) or {}
    if not sig:
        return _a("Agreement has not been computed yet.", how="latest ranking run")
    verdict = {"no_signal": "the judges agree no better than chance, so the ranking cannot be trusted as it stands",
               "weak": "agreement is weak: treat close places as ties", "moderate": "agreement is moderate",
               }.get(sig.get("verdict"), "agreement is good")
    return _a(f"Panel agreement ICC = {sig.get('icc1', 0):.2f} (p = {sig.get('p_value', 0):.2f}): {verdict}.",
              how=f"ICC(1) with a {sig.get('permutations', 2000)}-shuffle permutation test on run "
                  f"{run.output_hash[:10]}", links=[("Signal check", f"/o/{ev.slug}/results", "activity")])


def s_why(actor, ev, ent):
    from quorum.results.service import latest_run

    p = ent.get("project")
    run = latest_run(ev)
    e = next((x for x in (run.output.get("entries") or []) if x["project"] == p.ref), None) if run else None
    if not e:
        return _a(f"{p.title} is not in the ranking yet.", how="latest ranking run")
    lines = [f"Raw average {e['raw']:.2f} → calibrated {e['calibrated']:.2f} (± {e['se']:.2f})",
             f"Plausible places {e['rank_lo']}–{e['rank_hi']}; P(prize) {_pct(e.get('p_prize'))}",
             f"{e['n_reviews']} review(s)" + (" · inside a statistical tie with the next project" if e.get("tied_with_next") else "")]
    expl = (run.output.get("explanations") or {}).get(p.ref) or {}
    names = _judges(ev)
    for m in (expl.get("lines") or [])[:2]:  # already sorted by size of contribution
        if abs(m.get("contribution", 0)) >= 0.005:
            lines.append(f"{names.get(m['judge'], m['judge'])}: {m['contribution']:+.2f} ({m['label']})")
    return _a(f"{p.title} is #{e['rank']} of {len(run.output['entries'])}.", lines,
              f"explanation of run {run.output_hash[:10]}: raw mean plus each judge's calibration adjustment",
              [(f"Why #{e['rank']}", f"/o/{ev.slug}/results/{p.ref}", "scale")])


def s_feedback(actor, ev, ent):
    from quorum.judging import ops

    f = ops.feedback_coverage(ev)
    lines = [f"No feedback: {p.title}" for p in f["none"][:6]] + [f"Short feedback only: {p.title}" for p in f["short"][:4]]
    return _a(f"{f['with_feedback']} of {f['total']} teams have written feedback; {len(f['none'])} would receive none.",
              lines, f"feedback coverage: submitted reviews with at least {f['min_chars']} characters of feedback",
              [("Feedback", f"/o/{ev.slug}/feedback", "message-square-text")])


def s_flat(actor, ev, ent):
    from quorum.results.service import latest_run

    run = latest_run(ev)
    flat = list(((run.output.get("flat_judges") or {}) if run else {}).keys())
    names = _judges(ev)
    if not flat:
        return _a("No judge gave every project the same scores.", how="flat-judge rule (≥3 reviews, zero spread)")
    return _a(f"{len(flat)} judge(s) gave every project the same scores; their weight is zero and their projects get "
              "replacement reviews.", [f"{names.get(j, j)} ({j})" for j in flat],
              "flat-judge rule of the locked method: at least 3 reviews and no spread", [("Judge statistics", f"/o/{ev.slug}/judges/stats", "chart-column")])


def s_forecast(actor, ev, ent):
    from quorum.judging import ops

    prog = ops.judge_progress(ev)
    fc = ops.forecast(ev, prog)
    risky = fc.get("risky_judges", 0)
    return _a(f"{fc['days_left']:.1f} days of judging left; {len(fc['at_risk'])} project(s) risk missing the review "
              f"target and {risky} judge(s) are unlikely to finish at their current pace.",
              [f"{r['project'].title}: {r['submitted']}/{r['target']}, {r['pending']} pending" for r in fc["at_risk"][:6]],
              "forecast: each judge's pace projected to the window close", [("Judging ops", f"/o/{ev.slug}/ops", "activity")])


def s_votes(actor, ev, ent):
    from quorum.voting.models import IntegrityFlag

    flags = list(IntegrityFlag.objects.filter(event=ev, status="open").order_by("rule")[:6])
    if not flags:
        return _a("No open voting integrity flags.", how="integrity flags F1–F6", links=[("Voting", f"/o/{ev.slug}/voting", "vote")])
    return _a(f"{len(flags)} open integrity flag(s) await a keep-or-void decision.",
              [f"{f.get_rule_display() if hasattr(f, 'get_rule_display') else f.rule}: {f.subject}" for f in flags],
              "integrity flags F1–F6 with their evidence", [("Review votes", f"/o/{ev.slug}/voting", "vote")])


def s_next(actor, ev, ent):
    from . import briefing

    d = briefing.decisions(ev)
    if not d:
        return _a("Nothing needs a decision right now.", how="the overview's decision list")
    return _a(f"{len(d)} thing(s) need you. Start with: {d[0][1]}", [x[1] for x in d[1:5]],
              "the overview's decision list, most consequential first", [(d[0][3], d[0][2], "arrow-right")])


def s_leaders(actor, ev, ent):
    from quorum.results.service import latest_run

    run = latest_run(ev)
    if not run or run.output.get("status") != "ok":
        return _a("There is no ranking yet.", how="latest ranking run")
    titles = _titles(ev)
    top = run.output["entries"][:5]
    return _a("Current preview ranking (not published; organizers only):",
              [f"#{e['rank']} {titles.get(e['project'], e['project'])}: {e['calibrated']:.2f} ± {e['se']:.2f}, "
               f"P(prize) {_pct(e.get('p_prize'))}" + (" · tie" if e.get("tied_with_next") else "") for e in top],
              f"run {run.output_hash[:10]}: calibrated scores with 4,000 simulated re-rankings",
              [("Results", f"/o/{ev.slug}/results", "trophy")])


def s_judge(actor, ev, ent):
    from quorum.judging import ops

    j = ent.get("judge")
    p = next((x for x in ops.judge_progress(ev) if x["ref"] == j.ref), None)
    if not p:
        return _a(f"{j.user.display} has no assignments.", how="judging ops")
    return _a(f"{p['name']} ({p['ref']}): {p['submitted']} of {p['assigned']} submitted, {p['pending']} pending, "
              f"status {p['status']}.", [f"Tracks: {', '.join(p['tracks']) or '–'}",
                                        f"Last activity {round(p['idle_hours'] or 0)} h ago"],
              "judging ops: this judge's assignments and activity", [("Judges", f"/o/{ev.slug}/judges", "gavel")])


# ------------------------------------------------------------------ everyone

def s_dates(actor, ev, ent):
    from datetime import UTC

    f = "%a %d %b, %H:%M UTC"
    return _a(f"{ev.name}: submissions close {ev.submissions_close_at.astimezone(UTC).strftime(f)}; judging "
              f"closes {ev.judging_closes_at.astimezone(UTC).strftime(f)}.",
              how="the event's published dates", links=[("Event page", f"/e/{ev.slug}", "calendar")])


def s_my_batch(actor, ev, ent):
    from quorum.judging.models import Assignment

    role = actor.judge_role(ev)
    qs = Assignment.objects.filter(event=ev, judge_role=role).exclude(status__in=["reassigned", "recused"])
    pending = qs.filter(status__in=["pending", "in_progress"]).count()
    done = qs.filter(status="submitted").count()
    return _a(f"You have submitted {done} review(s); {pending} left" + (" (about %d minutes)." % (pending * 8) if pending else "."),
              how="your own assignments", links=[("Your batch", f"/j/{ev.slug}", "gavel")])


def s_method(actor, ev, ent):
    return _a("Scores are calibrated for each judge's lean (measured on projects other judges also saw), reported with "
              "their uncertainty, and ties at prize positions go to a pre-registered head-to-head round.",
              how="the published method", links=[("How judging works", f"/e/{ev.slug}/methodology", "book-open")])


SKILLS = [
    Skill("stalled_judges", {"organizer"}, ["who hasn't started", "who has not started yet", "who hasn't started judging",
          "which judges are stalled", "who is behind",
          "judges not finished", "which judges have not submitted", "who needs a reminder"],
          "judge judges stalled behind started finished remind reminder slow late", s_stalled),
    Skill("coverage", {"organizer"}, ["which projects need more reviews", "projects below the review target",
          "how many projects are fully reviewed", "is every project reviewed"],
          "projects reviews coverage target enough reviewed below short", s_coverage),
    Skill("ties", {"organizer"}, ["is there a tie for first place", "are there ties at the prize positions",
          "is the podium certain", "can we separate the winners"], "tie ties tied podium first place winners certain", s_ties),
    Skill("agreement", {"organizer"}, ["do the judges agree", "can we trust the ranking", "is the ranking reliable",
          "how consistent are the judges"], "agree agreement trust reliable consistent signal icc", s_signal),
    Skill("why_rank", {"organizer"}, ["why is this project ranked where it is", "explain the score of", "why is it placed",
          "how was its score calculated"], "why explain rank ranked placed score calculated", s_why, needs="project"),
    Skill("feedback", {"organizer"}, ["which teams have no feedback", "feedback coverage", "who will get no feedback",
          "are all teams getting feedback"], "feedback teams comments written none", s_feedback),
    Skill("flat_judges", {"organizer"}, ["which judge gave everyone the same score", "flat judges",
          "judges who marked everything the same"], "same score flat everything identical judge", s_flat),
    Skill("forecast", {"organizer"}, ["when will judging finish", "are we on track", "will we finish in time",
          "how many days are left"], "finish track time days left forecast deadline done", s_forecast),
    Skill("votes", {"organizer"}, ["is there vote fraud", "voting integrity flags", "suspicious votes",
          "is anyone cheating the vote"], "vote votes voting fraud cheating suspicious flags sybil", s_votes),
    Skill("next_action", {"organizer"}, ["what should I do next", "what needs my attention", "what is left to do",
          "any problems"], "next todo attention problems needs decide", s_next),
    Skill("leaders", {"organizer"}, ["who is winning", "show the top projects", "current ranking", "who is in first place"],
          "winning top leaders ranking first best", s_leaders),
    Skill("judge_status", {"organizer"}, ["how is this judge doing", "progress of judge", "how far is"],
          "judge progress doing", s_judge, needs="judge"),
    Skill("dates", {"anyone"}, ["when do submissions close", "what is the deadline", "when does judging end",
          "when are results"], "when deadline close closes date submissions end", s_dates),
    Skill("my_batch", {"judge"}, ["how many reviews do I have left", "what is left in my batch", "my progress"],
          "my left batch progress remaining", s_my_batch),
    Skill("method", {"anyone"}, ["how is judging done", "how are scores normalised", "is the judging fair",
          "how are harsh judges handled"], "how judging fair normalised normalized harsh method scores", s_method),
]


def _titles(ev):
    from quorum.events.models import Project

    return dict(Project.objects.filter(event=ev).values_list("ref", "title"))


def _judges(ev):
    from quorum.events.models import EventRole

    return {r.ref: r.user.display for r in EventRole.objects.filter(event=ev, role="judge").select_related("user")}


def _entities(q: str, ev, want: str):
    """Find a project or judge named in the question (exact, then fuzzy)."""
    from quorum.events.models import EventRole, Project

    ql = q.lower()
    if want == "project":
        items = [(p.title.lower(), p) for p in Project.objects.filter(event=ev, status="submitted", duplicate_of__isnull=True)]
    else:
        items = []
        for r in EventRole.objects.filter(event=ev, role="judge").select_related("user"):
            name = (r.user.name or "").lower()
            items += [(name, r), (r.ref.lower(), r)] + ([(name.split()[0], r)] if name else [])
    for k, o in sorted(items, key=lambda t: -len(t[0])):
        if k and len(k) > 2 and re.search(r"\b" + re.escape(k) + r"\b", ql):
            return o, k
    words = re.findall(r"[a-z0-9_]+", ql)
    grams = {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}
    keys = [k for k, _ in items if k]
    for g in sorted(grams, key=len, reverse=True):
        m = difflib.get_close_matches(g, keys, n=1, cutoff=0.86)
        if m and len(g) > 3:
            return next(o for k, o in items if k == m[0]), g
    return None, None


def _roles(actor, ev):
    roles = {"anyone"}
    if ev is not None and actor.user:
        if actor.is_organizer(ev) or actor.is_admin:
            roles.add("organizer")
        if actor.judge_role(ev):
            roles.add("judge")
    return roles


def _vectors():
    from . import embed

    if not embed.available():
        return None
    for s in SKILLS:
        if s.vectors is None:
            s.vectors = embed.encode(s.examples)
    return True


def match(q: str, allowed: set):
    """-> (skill, confidence, method) or (None, best, method)."""
    from . import embed

    cands = [s for s in SKILLS if s.roles & allowed]
    if not cands or len(q.split()) < 2:
        return None, 0.0, "none"
    if _vectors():
        v = embed.encode_one(q)
        best = max(cands, key=lambda s: float((s.vectors @ v).max()))
        score = float((best.vectors @ v).max())
        return (best if score >= MATCH_AT else None), score, "meaning"
    words = set(re.findall(r"[a-z]+", q.lower()))

    def kw(s):
        k = set(s.keywords.split())
        return len(words & k) / max(1, min(len(k), 3))
    best = max(cands, key=kw)
    return (best if kw(best) >= KEYWORD_AT else None), kw(best), "keywords"


def ask(actor, q: str, ev=None) -> dict | None:
    """Answer a question for this actor, or None when no skill fits (search results still show)."""
    q = " ".join((q or "").split())[:300]
    if ev is None:
        return None
    allowed = _roles(actor, ev)
    # find names first and mask them ("why is Small Meadow 15th" -> "why is this project 15th"), so the
    # meaning match sees the question rather than the name; judges' names only for organizers
    found, masked = {}, q
    for kind in ("project", "judge") if "organizer" in allowed else ("project",):
        obj, text = _entities(masked, ev, kind)
        if obj is not None:
            found[kind] = obj
            masked = re.sub(re.escape(text), f"this {kind}", masked, count=1, flags=re.I)
    skill, conf, method = match(masked, allowed)
    if skill is None:
        return None
    ent = {}
    if skill.needs:
        found = found.get(skill.needs)
        if found is None:
            return _a(f"Which {skill.needs}? Name it in the question, e.g. “{skill.examples[0]} Small Meadow”.",
                      how=f"skill {skill.name} needs a {skill.needs}") | {"skill": skill.name, "confidence": round(conf, 2)}
        ent[skill.needs] = found
    ans = skill.run(actor, ev, ent)
    ans["how"] = f"{ans['how']} · skill “{skill.name}”, matched by {method} ({conf:.2f})"
    ans["skill"] = skill.name
    ans["confidence"] = round(conf, 2)
    ans["links"] = [{"title": t, "url": u, "icon": i} for t, u, i in ans["links"]]
    return ans
