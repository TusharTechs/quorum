"""Meaning-based search, similar projects and possible duplicates over submissions.

Boilerplate-aware: a sentence that appears in many submissions of the same event (a template
line such as the fixture's "One line of what it does.") says nothing about any one project,
so it is dropped before embedding. Otherwise every project would look like every other.

Search is hybrid: cosine similarity of meaning plus a keyword bonus, so an exact name still
wins. Duplicates are only ever *suggested* to an organizer, with the score; a human decides.
"""

from __future__ import annotations

import re
from collections import Counter

from .embed import available, content_hash, encode, encode_one
from .models import Embedding

BOILERPLATE_SHARE = 0.3
SEARCH_FLOOR = 0.30
DUPLICATE_AT = 0.90


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]


def boilerplate(projects) -> set[str]:
    """Sentences shared by at least 30% of the given submissions (and at least 3 of them)."""
    projects = list(projects)
    if len(projects) < 5:
        return set()
    c = Counter()
    for p in projects:
        c.update({s.lower() for s in _sentences(f"{p.tagline}\n{p.description_md}")})
    need = max(3, int(len(projects) * BOILERPLATE_SHARE))
    return {s for s, n in c.items() if n >= need}


def project_text(p, drop: set[str] = frozenset()) -> str:
    body = " ".join(s for s in _sentences(f"{p.tagline}\n{p.description_md}") if s.lower() not in drop)
    tags = ", ".join(p.tech_tags or [])
    track = p.track.name if getattr(p, "track", None) else ""
    return " · ".join(x for x in (p.title, body, tags, track) if x)[:2000]


def vectors(projects, drop=None) -> dict:
    """{project.pk: vector}, computing and caching only what changed."""
    import numpy as np

    projects = list(projects)
    if not projects or not available():
        return {}
    drop = boilerplate(projects) if drop is None else drop
    texts = {p.pk: project_text(p, drop) for p in projects}
    hashes = {pk: content_hash(t) for pk, t in texts.items()}
    have = {e.object_id: e for e in Embedding.objects.filter(kind="project", object_id__in=[str(k) for k in texts])}
    out, missing = {}, []
    for pk, h in hashes.items():
        e = have.get(str(pk))
        if e is not None and e.content_hash == h:
            out[pk] = np.frombuffer(bytes(e.vector), dtype="float32")
        else:
            missing.append(pk)
    if missing:
        V = encode([texts[pk] for pk in missing])
        for pk, v in zip(missing, V):
            Embedding.objects.update_or_create(kind="project", object_id=str(pk),
                                               defaults={"content_hash": hashes[pk], "vector": v.tobytes()})
            out[pk] = v
    return out


def _keyword(q: str, p) -> float:
    toks = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]
    if not toks:
        return 0.0
    hay = f"{p.title} {p.tagline} {' '.join(p.tech_tags or [])} {p.team.name} {p.track.name if p.track else ''}".lower()
    return sum(t in hay for t in toks) / len(toks)


def search(projects, q: str, limit: int = 200) -> list[tuple]:
    """[(project, score, how)] best first; how is "meaning", "words" or "both"."""
    projects = list(projects)
    V = vectors(projects)
    qv = encode_one(q) if V else None
    rows = []
    for p in projects:
        kw = _keyword(q, p)
        sim = float(V[p.pk] @ qv) if qv is not None and p.pk in V else 0.0
        if sim < SEARCH_FLOOR and kw == 0:
            continue
        how = "both" if (sim >= SEARCH_FLOOR and kw) else ("meaning" if sim >= SEARCH_FLOOR else "words")
        rows.append((p, 0.7 * sim + 0.3 * kw + (0.5 if q.lower() in p.title.lower() else 0.0), how))
    rows.sort(key=lambda r: -r[1])
    return rows[:limit]


def similar(project, pool, n: int = 3) -> list[tuple]:
    pool = [p for p in pool if p.pk != project.pk]
    V = vectors(pool + [project])
    if project.pk not in V:
        return []
    v = V[project.pk]
    scored = sorted(((p, float(V[p.pk] @ v)) for p in pool if p.pk in V), key=lambda t: -t[1])
    return [t for t in scored[:n] if t[1] > 0.35]


def possible_duplicates(projects, threshold: float = DUPLICATE_AT) -> list[dict]:
    """Pairs whose non-boilerplate text means nearly the same, or that share a repository URL."""
    projects = [p for p in projects]
    V = vectors(projects)
    out = []
    for i, a in enumerate(projects):
        for b in projects[i + 1:]:
            same_repo = bool(a.repo_url) and a.repo_url.rstrip("/").lower() == (b.repo_url or "").rstrip("/").lower()
            sim = float(V[a.pk] @ V[b.pk]) if a.pk in V and b.pk in V else None
            if same_repo or (sim is not None and sim >= threshold):
                out.append({"a": a, "b": b, "similarity": sim, "same_repo": same_repo})
    out.sort(key=lambda d: -(d["similarity"] or 0))
    return out
