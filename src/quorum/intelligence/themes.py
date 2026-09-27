"""Feedback themes for a team's scorecard: what the judges said, grouped by rubric criterion.

Extractive only: every line shown is a sentence a judge actually wrote, attributed to the same
pseudonym as in the full feedback below it. Sentences are placed under the criterion they are
closest to in meaning (local embeddings), and sentences that suggest something to do are also
listed as next steps. Nothing is generated or paraphrased.
"""

from __future__ import annotations

import re

from .coach import ACTION, anchors_for

MIN_SENTENCES = 3
FIT = 0.33


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if len(s.strip()) >= 12]


def themes(feedback: list[dict], criteria) -> dict:
    """feedback: [{"judge": pseudonym, "text": ...}] -> {"by_criterion": [...], "next_steps": [...]}"""
    from . import embed

    sents = [(f["judge"], s) for f in feedback for s in _sentences(f["text"])]
    crits = list(criteria)
    out = {"by_criterion": [], "next_steps": [], "method": ""}
    if len(sents) < MIN_SENTENCES or not crits:
        return out
    out["next_steps"] = [{"judge": j, "text": s} for j, s in sents if ACTION.search(s)][:5]
    if not embed.available():
        out["method"] = "suggestions found by wording"
        return out
    sims = embed.match_groups([s for _, s in sents], [anchors_for(c) for c in crits])
    groups = {c.key: [] for c in crits}
    for (j, s), row in zip(sents, sims):
        k = int(row.argmax())
        if float(row[k]) >= FIT:
            groups[crits[k].key].append((float(row[k]), {"judge": j, "text": s}))
    out["by_criterion"] = [{"name": c.name, "quotes": [q for _, q in sorted(groups[c.key], key=lambda t: -t[0])[:3]],
                            "count": len(groups[c.key])} for c in crits if groups[c.key]]
    out["method"] = "each sentence placed under the criterion closest in meaning, on this server"
    return out
