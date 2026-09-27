"""Judge assignment as measurement design.

Judge leniency is only measurable through projects that several judges share: if two
judges never review a common project, nobody can tell whether one is harsh or simply
drew weaker projects. Classic "table judging" (disjoint panels) makes calibration
impossible. Simulation on the fixture's design: calibration gains +0.018 Kendall tau on
a connected design vs +0.003 on disjoint panels.

So assignment here does four jobs at once: coverage (every project reaches k reviews),
balance (no judge overloaded), integrity (tracks and conflicts of interest respected),
and connectivity (the judge graph stays one component so offsets are identifiable).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict


def assign(projects, judges, k=3, capacity=None, conflicts=(), seed=0, existing=None):
    """Greedy overlap-maximising assignment.

    projects : [{"id","track"}]
    judges   : [{"id","tracks": [...] | None (any track)}]
    k        : target reviews per project (existing assignments count towards it)
    capacity : int or {judge: int} total load cap (existing load counts); default
               ceil(k*|P|/|J|) + 1
    conflicts: set of (judge, project) pairs that are forbidden
    existing : {project: [judge, ...]} already assigned (kept, never moved)

    Slots are filled in rounds (every project gets its next judge before any project gets
    two more), hardest project first (fewest eligible judges). For each slot the eligible
    judge with spare capacity maximising
        3 * new co-review edges + 2 * joins two judge-graph components - 2 * load/capacity
    is chosen. Returns {"new": [(judge, project)], "assignment": {p: [j]}, "unfilled",
    "components", "load", "judge_edges"}.
    """
    rng = random.Random(seed)
    conflicts = set(conflicts)
    J = [j["id"] for j in judges]
    tracks_of = {j["id"]: (set(j["tracks"]) if j.get("tracks") else None) for j in judges}
    if capacity is None:
        capacity = math.ceil(k * len(projects) / max(1, len(J))) + 1
    cap = capacity if isinstance(capacity, dict) else {j: capacity for j in J}
    existing = {p: list(v) for p, v in (existing or {}).items()}
    load = defaultdict(int)
    edges = set()
    parent = {j: j for j in J}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    out = {p["id"]: list(existing.get(p["id"], [])) for p in projects}
    for js in out.values():
        for a in js:
            load[a] += 1
        for i in range(len(js)):
            for m in range(i + 1, len(js)):
                a, b = js[i], js[m]
                edges.add((min(a, b), max(a, b)))
                parent[find(a)] = find(b)

    def eligible(j, p):
        t = tracks_of[j]
        return (t is None or p["track"] in t) and (j, p["id"]) not in conflicts

    elig = {p["id"]: [j for j in J if eligible(j, p)] for p in projects}
    order = sorted(projects, key=lambda p: (len(elig[p["id"]]), rng.random()))
    new, unfilled = [], []
    for rnd in range(k):
        for p in order:
            pid = p["id"]
            if len(out[pid]) > rnd:
                continue
            cands = [j for j in elig[pid] if j not in out[pid] and load[j] < cap.get(j, 0)]
            if not cands:
                unfilled.append({"project": pid, "slot": rnd + 1})
                continue

            def score(j):
                new_edges = sum(1 for o in out[pid] if (min(j, o), max(j, o)) not in edges)
                joins = sum(1 for o in out[pid] if find(o) != find(j))
                return 3 * new_edges + 2 * joins - 2 * load[j] / max(cap.get(j, 1), 1) + 1e-6 * rng.random()

            j = max(cands, key=score)
            for o in out[pid]:
                edges.add((min(j, o), max(j, o)))
                parent[find(j)] = find(o)
            out[pid].append(j)
            load[j] += 1
            new.append((j, pid))
    active = {j for js in out.values() for j in js}
    comps = len({find(j) for j in active})
    return {"new": new, "assignment": out, "unfilled": unfilled, "components": comps,
            "load": dict(load), "judge_edges": len(edges)}


def assign_disjoint_panels(projects, judges, k=3, seed=0):
    """Baseline for the proof: panels of k judges review disjoint blocks of projects, so the
    judge graph has one component per panel and cross-panel leniency is unidentifiable."""
    rng = random.Random(seed)
    J = [j["id"] for j in judges]
    rng.shuffle(J)
    panels = [J[i:i + k] for i in range(0, len(J) - len(J) % k, k)]
    P = [p["id"] for p in projects]
    rng.shuffle(P)
    return {"assignment": {p: list(panels[i % len(panels)]) for i, p in enumerate(P)},
            "components": len(panels)}


def split_batches(pairs, batch_size):
    """Group a judge's (judge, project) pairs into batches of at most batch_size."""
    by_j = defaultdict(list)
    for j, p in pairs:
        by_j[j].append(p)
    out = {}
    for j, ps in by_j.items():
        out[j] = [ps[i:i + batch_size] for i in range(0, len(ps), batch_size)]
    return out


def design_diagnostics(pairs, project_track=None):
    """Graph / identifiability report for a set of (judge, project) pairs (assigned or
    reviewed): components, shared judge pairs, reviews per project, and the information
    behind each judge's offset, n_j - sum_{p in j} 1/n_p (the Schur-complement diagonal)."""
    by_j = defaultdict(set)
    by_p = defaultdict(set)
    for j, p in pairs:
        by_j[j].add(p)
        by_p[p].add(j)
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for j, p in pairs:
        parent[find("j:" + j)] = find("p:" + p)
    roots = {find(n) for n in list(parent)}
    info = {j: len(ps) - sum(1.0 / len(by_p[p]) for p in ps) for j, ps in by_j.items()}
    js = sorted(by_j)
    shared = 0
    for a in range(len(js)):
        for b in range(a + 1, len(js)):
            if by_j[js[a]] & by_j[js[b]]:
                shared += 1
    track_components = {}
    if project_track:
        for t in sorted(set(project_track.values())):
            sub = [(j, p) for j, p in pairs if project_track.get(p) == t]
            par = {}

            def f2(x):
                par.setdefault(x, x)
                while par[x] != x:
                    par[x] = par[par[x]]
                    x = par[x]
                return x

            for j, p in sub:
                par[f2("j:" + j)] = f2("p:" + p)
            track_components[t] = len({f2(n) for n in list(par)}) if sub else 0
    return {
        "components": len(roots),
        "track_components": track_components,
        "judge_pairs_sharing": shared,
        "judge_pairs_total": len(js) * (len(js) - 1) // 2,
        "offset_information": info,
        "reviews_per_project": {p: len(v) for p, v in by_p.items()},
        "reviews_per_judge": {j: len(v) for j, v in by_j.items()},
    }
