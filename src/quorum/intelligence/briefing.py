"""The organizer's briefing: the decisions only a human can make right now. Shared by the
overview page and Ask Quorum, so both always say the same thing."""

from __future__ import annotations


def state(ev) -> dict:
    from quorum.judging import feedback, ops
    from quorum.results import decide
    from quorum.results.service import latest_run
    from quorum.voting.models import IntegrityFlag

    progress = ops.judge_progress(ev)
    cov = ops.coverage(ev)
    fcov = ops.feedback_coverage(ev)
    run = latest_run(ev)
    out = run.output if run else {}
    ties = decide.boundary_ties(run, ev.prize_positions) if run and out.get("status") == "ok" else []
    return {"progress": progress, "cov": cov, "fcov": fcov, "run": run, "out": out, "ties": ties,
            "flags": IntegrityFlag.objects.filter(event=ev, status="open").count(),
            "pending_mod": feedback.pending_moderation(ev),
            "stalled": [p for p in progress if p["status"] in ("stalled", "behind")]}


def decisions(ev, st: dict | None = None) -> list[tuple]:
    """[(kind, text, url, call to action)], most consequential first."""
    st = st or state(ev)
    cov, fcov, out = st["cov"], st["fcov"], st["out"]
    d = []
    if cov["below"]:
        d.append(("warn", f"{cov['below']} project(s) are below {cov['target']} reviews with nothing pending.",
                  f"/o/{ev.slug}/ops", "Fill coverage gaps"))
    if st["stalled"]:
        d.append(("warn", f"{len(st['stalled'])} judge(s) are stalled or behind pace.", f"/o/{ev.slug}/ops",
                  "Rebalance or nudge"))
    if out.get("signal", {}).get("verdict") == "no_signal":
        d.append(("bad", "The judges' scores show no detectable agreement: the ranking cannot be "
                         "distinguished from chance.", f"/o/{ev.slug}/results", "See the signal check"))
    for t in st["ties"]:
        d.append(("tie", f"Prize position {t['boundary']} falls inside a statistical tie ({len(t['projects'])} projects).",
                  f"/o/{ev.slug}/results#ties", "Open a tie-break"))
    if fcov["none"]:
        d.append(("warn", f"{len(fcov['none'])} team(s) would receive no written feedback.", f"/o/{ev.slug}/feedback",
                  "See feedback coverage"))
    if st["flags"]:
        d.append(("warn", f"{st['flags']} voting integrity flag(s) await review.", f"/o/{ev.slug}/voting", "Review votes"))
    if st["pending_mod"]:
        d.append(("", f"{st['pending_mod']} feedback item(s) await moderation.", f"/o/{ev.slug}/feedback", "Moderate"))
    return d
