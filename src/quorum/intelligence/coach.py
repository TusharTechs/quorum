"""Feedback coach: live, local advice while a judge writes feedback. Advice only, never a gate
(the only hard rule stays the event's minimum length, enforced on submit by the review service).

Checks what makes feedback useful to a team: long enough to be read as considered, touches
each rubric criterion, contains a concrete next step, refers to specifics, and stays kind.
Criterion coverage uses sentence embeddings when the local model is available
(quorum.intelligence.embed) and falls back to keyword overlap with the criterion's own words.
"""

from __future__ import annotations

import re

ACTION = re.compile(r"\b(try|consider|add|adding|could|should|would|next|recommend|suggest|improve|instead|maybe|"
                    r"perhaps|if you|worth|missing|document|write|test|include|split|rename|cache|validate)\b", re.I)
HARSH = re.compile(r"\b(stupid|useless|terrible|garbage|trash|lazy|pathetic|awful|worst|dumb|idiotic|joke|"
                   r"waste of time|no effort|horrible)\b", re.I)
SPECIFIC = re.compile(r"(\d|`|\"|“|\b[A-Z][a-z]+[A-Z]\w*|\b\w+\.(py|js|ts|md|json|yml|go|rs)\b|\bREADME\b|\bAPI\b|"
                      r"\bUI\b|\bCLI\b|\btests?\b|\bdemo\b|\bdocs?\b|\binstall\w*\b|\bsetup\b|\berror\w*\b)")
STOP = set("the a an and or of to in for on with is are be it this that as at by from your you our their its "
           "does do how what something someone others not have has had".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOP}


# everyday words people use for the common rubric criteria, so "runs cleanly" counts as functionality
SYNONYMS = {
    "functionality": "works working run runs running demo feature features bug bugs crash edge cases broken install",
    "quality": "code clean structure tests test documentation docs readme maintain maintainable architecture refactor",
    "innovation": "idea novel new original creative clever unique approach unusual fresh steal",
    "impact": "users problem useful impact value helps real people audience",
    "design": "design interface ui ux layout usable usability accessible visual",
    "presentation": "demo video pitch explain explained presentation story clear",
}


def criteria_coverage(text: str, criteria) -> list[dict]:
    """criteria: iterable of objects with key, name, description."""
    crits = list(criteria)
    sims = None
    try:
        from . import embed

        if embed.available() and text.strip():
            sims = embed.coverage(text, [f"{c.name}. {c.description}" for c in crits])
    except Exception:  # the coach must never break the console
        sims = None
    words = _words(text)
    out = []
    for i, c in enumerate(crits):
        vocab = _words(f"{c.name} {c.description} {SYNONYMS.get(c.key, '')}")
        hit = bool(words & vocab)
        how = "keywords"
        if sims is not None:
            hit = hit or sims[i] >= 0.34
            how = "meaning"
        out.append({"key": c.key, "name": c.name, "covered": hit, "how": how})
    return out


def advise(text: str, criteria, min_chars: int = 0) -> dict:
    text = text or ""
    n = len(text.strip())
    checks = [
        {"ok": n >= max(min_chars, 1), "label": f"Long enough ({n}/{min_chars} characters)" if min_chars else f"{n} characters",
         "tip": "A few sentences the team can act on." if n < max(min_chars, 1) else ""},
        {"ok": bool(ACTION.search(text)), "label": "Suggests a next step",
         "tip": "Add one concrete thing to try next, e.g. “Consider adding …”."},
        {"ok": len(SPECIFIC.findall(text)) >= 1, "label": "Points at specifics",
         "tip": "Name a feature, file, step or number so the team knows exactly what you mean."},
        {"ok": not HARSH.search(text), "label": "Kind and direct",
         "tip": "Some wording may read as harsh. Describe the problem, not the people."},
    ]
    cov = criteria_coverage(text, criteria) if n else []
    missing = [c["name"] for c in cov if not c["covered"]]
    score = sum(c["ok"] for c in checks) + (len(cov) - len(missing))
    total = len(checks) + len(cov)
    return {"checks": checks, "coverage": cov, "missing": missing, "score": score, "total": total, "empty": n == 0}
