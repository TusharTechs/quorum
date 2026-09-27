"""Adapter from the DOGFOOD fixture format to engine input (used by the CLI and tests;
the web app builds the same input from the database)."""

from __future__ import annotations

from .prepare import find_duplicates, merge_reviews

DEFAULT_CRITERIA = ["functionality", "quality", "innovation"]


def from_dogfood_fixture(fx: dict, weights: dict | None = None) -> tuple[dict, dict]:
    close = fx.get("event", {}).get("submissions_close")
    crits = sorted({c for s in fx.get("scores", []) for c in (s.get("criteria") or {})}) or DEFAULT_CRITERIA
    order = [c for c in DEFAULT_CRITERIA if c in crits] + [c for c in crits if c not in DEFAULT_CRITERIA]
    weights = weights or {c: 1 for c in order}
    dups = find_duplicates(fx["projects"], close)
    remap = {s: g["canonical"] for g in dups for s in g["superseded"]}
    reviews, retest = merge_reviews(fx["scores"], remap, order)
    projects = [{"id": p["id"], "track": p["track"], "title": p.get("title")}
                for p in fx["projects"] if p["id"] not in remap]
    inp = {
        "method": {},
        "criteria": [{"key": c, "weight": weights[c]} for c in order],
        "projects": sorted(projects, key=lambda p: p["id"]),
        "reviews": [{"judge": r["judge"], "project": r["project"], "criteria": r["criteria"]} for r in reviews],
    }
    return inp, {"duplicates": dups, "retest": retest}
