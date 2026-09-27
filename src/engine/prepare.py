"""Turn raw submissions + reviews into clean model input.

Two data-quality problems every real hackathon has, both present in the DOGFOOD fixture:

* duplicate submissions (the same team submitting the same repo twice), and
* judges who give every project the same score ("flat" judges).

Neither is deleted. Duplicates are merged under a documented rule; flat judges are
kept in the audit trail but get weight 0 for ranking (see calibrate.fit_judge_effects).
"""

from __future__ import annotations

from collections import defaultdict


def _norm_repo(url: str | None) -> str:
    return (url or "").strip().lower().rstrip("/").removesuffix(".git")


def _norm_title(t: str | None) -> str:
    return " ".join((t or "").strip().lower().split())


def find_duplicates(projects: list[dict], submissions_close: str | None = None) -> list[dict]:
    """Group projects that are the same submission: same team AND (same repo OR same title).

    projects: [{"id","team","repo_url","title","submitted_at"}]
    Canonical = latest submission at or before the deadline (the team's final word);
    if none is on time, the latest overall. Returns [{canonical, superseded[], reason}].
    """
    by_team: dict[str, list[dict]] = defaultdict(list)
    for p in projects:
        by_team[p.get("team")].append(p)
    out = []
    for plist in by_team.values():
        if len(plist) < 2:
            continue
        parent = {p["id"]: p["id"] for p in plist}

        def find(x, parent=parent):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a in plist:
            for b in plist:
                if a["id"] < b["id"]:
                    same_repo = _norm_repo(a.get("repo_url")) and _norm_repo(a.get("repo_url")) == _norm_repo(b.get("repo_url"))
                    same_title = _norm_title(a.get("title")) and _norm_title(a.get("title")) == _norm_title(b.get("title"))
                    if same_repo or same_title:
                        parent[find(a["id"])] = find(b["id"])
        comps: dict[str, list[dict]] = defaultdict(list)
        for p in plist:
            comps[find(p["id"])].append(p)
        for comp in comps.values():
            if len(comp) < 2:
                continue
            on_time = [p for p in comp if submissions_close is None or (p.get("submitted_at") or "") <= submissions_close]
            pool = on_time or comp
            canon = max(pool, key=lambda p: (p.get("submitted_at") or "", p["id"]))
            out.append({
                "canonical": canon["id"],
                "superseded": sorted(p["id"] for p in comp if p["id"] != canon["id"]),
                "reason": "same team and same repository or title",
            })
    out.sort(key=lambda g: g["canonical"])
    return out


def merge_reviews(scores: list[dict], remap: dict[str, str], criteria: list[str]) -> tuple[list[dict], list[dict]]:
    """Collapse raw score rows into one observation per (judge, project).

    scores: [{"judge","project","criteria":{c: value}}]
    remap:  {superseded project id -> canonical id}
    If one judge scored both copies of a duplicate, their scores are averaged
    criterion by criterion into ONE observation, so no judge counts twice.
    Returns (reviews, retest_cases) where retest_cases record same-judge double scores
    (a free test-retest noise measurement).
    """
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in scores:
        p = remap.get(s["project"], s["project"])
        crit = {c: float(v) for c, v in (s.get("criteria") or {}).items() if c in criteria and v is not None}
        if crit:
            cells[(s["judge"], p)].append(crit)
    reviews, retest = [], []
    for (j, p), lst in sorted(cells.items()):
        if len(lst) > 1:
            retest.append({"judge": j, "project": p, "scores": lst})
        merged = {}
        for c in criteria:
            vals = [d[c] for d in lst if c in d]
            if vals:
                merged[c] = sum(vals) / len(vals)
        reviews.append({"judge": j, "project": p, "criteria": merged, "n_merged": len(lst)})
    return reviews, retest


def weighted_total(crit: dict[str, float], weights: dict[str, float]) -> float | None:
    """Weighted mean over the criteria present (weights renormalised). None if nothing present."""
    present = [c for c in weights if c in crit and weights[c] > 0]
    if not present:
        return None
    w = sum(weights[c] for c in present)
    return sum(weights[c] * crit[c] for c in present) / w


def detect_flat_judges(reviews: list[dict], min_reviews: int = 3) -> dict[str, dict]:
    """A judge is 'flat' if every criterion score on every review is the same value and
    they did at least `min_reviews` reviews. Their discrimination slope is exactly 0, so
    their scores say nothing about which project is better."""
    vals: dict[str, list[float]] = defaultdict(list)
    count: dict[str, int] = defaultdict(int)
    for r in reviews:
        vals[r["judge"]].extend(r["criteria"].values())
        count[r["judge"]] += 1
    flat = {}
    for j, v in vals.items():
        if count[j] >= min_reviews and len(set(v)) == 1:
            flat[j] = {"value": v[0], "n_reviews": count[j], "n_values": len(v)}
    return flat
