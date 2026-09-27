#!/usr/bin/env python3
"""Load test for a running Quorum: many people at once, then check that nothing was lost.

Standard library only, like run.py. It drives four kinds of traffic at the same time:

  readers     anonymous visitors on the gallery, project and event pages and the public API
  voters      signed-in voters (each from its own network): open a ballot, cast three votes,
              and try one duplicate (which must be refused)
  judges      judges autosaving drafts, then submitting every review in their batch
  organizers  the ops dashboard, the latest ranking, and recomputing rankings

and then asserts the invariants a judging platform must keep under concurrency: every vote
that got a 201 is counted exactly once, every duplicate is refused, every submitted review is
recorded, the audit hash chain still verifies, and the server never answered 5xx.

    docker compose -f docker-compose.yml -f docker-compose.loadtest.yml up -d --wait
    python3 tools/loadtest.py .dogfood.toml --voters 300 --judges 20 --readers 40 --duration 60

The voters come from distinct networks via X-Forwarded-For; docker-compose.loadtest.yml makes
the app trust the load generator as its reverse proxy, which is how production runs anyway.
"""

from __future__ import annotations

import argparse
import http.client
import json
import random
import ssl
import statistics
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
from collections import defaultdict


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat = defaultdict(list)
        self.codes = defaultdict(lambda: defaultdict(int))

    def add(self, kind, code, ms):
        with self.lock:
            self.lat[kind].append(ms)
            self.codes[kind][code] += 1


class Client:
    """One keep-alive connection per simulated person (like a browser)."""

    insecure = False

    def __init__(self, base, auth=None, xff=None):
        u = urllib.parse.urlsplit(base)
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname, u.port or (443 if self.https else 80)
        self.headers = {"Accept": "application/json"}
        if auth:
            self.headers["Authorization"] = auth
        if xff:
            self.headers["X-Forwarded-For"] = xff
        self.conn = None

    def req(self, method, path, body=None):
        h = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        for attempt in (0, 1):
            try:
                if self.conn is None:
                    if self.https:
                        ctx = ssl._create_unverified_context() if Client.insecure else ssl.create_default_context()
                        self.conn = http.client.HTTPSConnection(self.host, self.port, timeout=60, context=ctx)
                    else:
                        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=60)
                t = time.perf_counter()
                self.conn.request(method, path, body=data, headers=h)
                r = self.conn.getresponse()
                raw = r.read()
                return r.status, raw, (time.perf_counter() - t) * 1000
            except (http.client.HTTPException, ConnectionError, OSError):
                self.conn = None
                if attempt:
                    return 599, b"", 0.0
        return 599, b"", 0.0


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("toml", nargs="?", default=".dogfood.toml")
    ap.add_argument("--voters", type=int, default=300)
    ap.add_argument("--judges", type=int, default=20)
    ap.add_argument("--readers", type=int, default=40)
    ap.add_argument("--voter-threads", type=int, default=60)
    ap.add_argument("--duration", type=int, default=60, help="seconds of reader/organizer traffic")
    ap.add_argument("--setup", default="docker compose exec -T web python manage.py loadtest_setup")
    ap.add_argument("--report", default="")
    ap.add_argument("--base", default="", help="override the portal URL, e.g. https://localhost behind Caddy")
    ap.add_argument("--insecure", action="store_true", help="accept a self-signed/local CA certificate (testing)")
    a = ap.parse_args()

    cfg = tomllib.load(open(a.toml, "rb"))
    base = (a.base or cfg["portal"]["base_url"]).rstrip("/")
    Client.insecure = a.insecure
    org_auth = cfg["auth"]["organizer"].split(":", 1)[1].strip()
    setup = json.loads(subprocess.check_output(f"{a.setup} --voters {a.voters} --judges {a.judges}", shell=True))
    ev, slug = setup["event"], setup["slug"]
    org = Client(base, org_auth)
    s, raw, _ = org.req("GET", f"/api/v1/events/{ev}/votes/tally")
    before = sum(t["raw"] for t in json.loads(raw)) if s == 200 else None
    S = Stats()
    stop = time.time() + a.duration
    outcomes = defaultdict(int)
    olock = threading.Lock()

    def note(k):
        with olock:
            outcomes[k] += 1

    # ---------------------------------------------------------------- readers
    s, raw, _ = Client(base).req("GET", f"/api/v1/events/{ev}/projects")
    pids = [p["id"] for p in json.loads(raw)] if s == 200 else []

    def reader(i):
        c, rng = Client(base, xff=f"198.18.{i}.10"), random.Random(i)
        while time.time() < stop:
            path = rng.choice(["/projects", f"/p/{rng.choice(pids)}", f"/e/{slug}", f"/api/v1/events/{ev}/projects",
                               f"/e/{slug}/projects?q=climate", f"/e/{slug}/methodology"])
            code, _, ms = c.req("GET", path)
            S.add("read", code, ms)

    # ---------------------------------------------------------------- voters
    queue = list(enumerate(setup["voters"]))
    qlock = threading.Lock()

    def voter(_t):
        while True:
            with qlock:
                if not queue:
                    return
                i, token = queue.pop()
            c = Client(base, f"Bearer {token}", xff=f"100.{64 + i // 256}.{i % 256}.7")
            code, raw, ms = c.req("GET", f"/api/v1/events/{ev}/ballot")
            S.add("ballot", code, ms)
            if code != 200:
                note("ballot_failed")
                continue
            b = json.loads(raw)
            choices = [p["ref"] for p in b["projects"] if not p["voted"]][: max(0, min(3, b["votes_left"]))]
            for ref in choices:
                code, _, ms = c.req("POST", f"/api/v1/events/{ev}/votes", {"project": ref, "ballot": b["token"]})
                S.add("vote", code, ms)
                note("vote_ok" if code == 201 else f"vote_{code}")
            if choices:  # a duplicate must be refused, never counted
                code, _, ms = c.req("POST", f"/api/v1/events/{ev}/votes", {"project": choices[0], "ballot": b["token"]})
                S.add("vote-duplicate", code, ms)
                note("dup_refused" if code in (400, 403, 409, 422) else f"dup_{code}")

    # ---------------------------------------------------------------- judges
    crit = setup["criteria"]
    fb = ("The demo runs and the core flow works. Consider adding tests around the parser and a short "
          "README section on setup; the idea itself is original.")

    def judge(j):
        c, rng = Client(base, f"Bearer {j['token']}", xff=f"198.19.{rng_id(j)}.20"), random.Random(j["ref"])
        for aid in j["assignments"]:
            for k in range(2):  # autosave drafts
                code, _, ms = c.req("PUT", f"/api/v1/assignments/{aid}/review",
                                    {"scores": {crit[0]: rng.randint(1, 5)}, "feedback_to_team": fb[: 40 * (k + 1)]})
                S.add("review-draft", code, ms)
            code, _, ms = c.req("PUT", f"/api/v1/assignments/{aid}/review",
                                {"scores": {k: rng.randint(1, 5) for k in crit}, "feedback_to_team": fb, "submit": True})
            S.add("review-submit", code, ms)
            note("review_ok" if code == 200 else f"review_{code}")

    def rng_id(j):
        return int(j["ref"].split("_")[-1]) % 250

    # ---------------------------------------------------------------- organizers
    def organizer(i):
        c = Client(base, org_auth, xff=f"198.20.{i}.30")
        n = 0
        while time.time() < stop:
            for path in (f"/o/{slug}/ops", f"/api/v1/events/{ev}/rankings/latest", f"/o/{slug}"):
                code, _, ms = c.req("GET", path)
                S.add("organizer", code, ms)
            n += 1
            if n % 5 == 0:
                code, _, ms = c.req("POST", f"/api/v1/events/{ev}/rankings", {"heavy": False})
                S.add("recompute", code, ms)

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(a.readers)]
    threads += [threading.Thread(target=voter, args=(i,)) for i in range(a.voter_threads)]
    threads += [threading.Thread(target=judge, args=(j,)) for j in setup["judges"]]
    threads += [threading.Thread(target=organizer, args=(i,)) for i in range(2)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0

    # ---------------------------------------------------------------- invariants
    s, raw, _ = org.req("GET", f"/api/v1/events/{ev}/votes/tally")
    after = sum(t["raw"] for t in json.loads(raw)) if s == 200 else None
    s, raw, _ = org.req("GET", f"/api/v1/events/{ev}/judges")
    judges = {j["ref"]: j for j in json.loads(raw)} if s == 200 else {}
    submitted = sum(judges.get(j["ref"], {}).get("submitted", 0) for j in setup["judges"])
    s, raw, _ = org.req("GET", f"/api/v1/events/{ev}/audit/verify")
    chain = json.loads(raw) if s == 200 else {}
    total = sum(len(v) for v in S.lat.values())
    five = sum(n for k in S.codes for c, n in S.codes[k].items() if c >= 500)
    checks = [
        ("no 5xx responses", five == 0, f"{five}"),
        ("every accepted vote counted exactly once", before is not None and after - before == outcomes["vote_ok"],
         f"{outcomes['vote_ok']} accepted, tally +{(after or 0) - (before or 0)}"),
        ("every duplicate vote refused", outcomes["dup_refused"] == sum(v for k, v in outcomes.items() if k.startswith("dup")),
         f"{outcomes['dup_refused']} refused"),
        ("every submitted review recorded", submitted == outcomes["review_ok"],
         f"{outcomes['review_ok']} submitted, {submitted} recorded"),
        ("audit hash chain intact", bool(chain.get("ok")), f"{chain.get('checked')} entries"),
    ]
    lines = ["# Quorum load test", "",
             f"{len(threads)} concurrent clients ({a.readers} readers, {a.voter_threads} voter workers for "
             f"{a.voters} voters, {len(setup['judges'])} judges, 2 organizers) against `{base}`; "
             f"{total} requests in {wall:.1f} s = **{total / wall:.0f} req/s**.", "",
             "| Traffic | Requests | p50 ms | p95 ms | p99 ms | Status codes |", "|---|---:|---:|---:|---:|---|"]
    for k in ("read", "ballot", "vote", "vote-duplicate", "review-draft", "review-submit", "organizer", "recompute"):
        if S.lat[k]:
            codes = ", ".join(f"{c}×{n}" for c, n in sorted(S.codes[k].items()))
            lines.append(f"| {k} | {len(S.lat[k])} | {statistics.median(S.lat[k]):.0f} | {pct(S.lat[k], 95):.0f} | "
                         f"{pct(S.lat[k], 99):.0f} | {codes} |")
    lines += ["", "| Invariant | Result | Detail |", "|---|---|---|"]
    lines += [f"| {name} | {'PASS' if ok else 'FAIL'} | {d} |" for name, ok, d in checks]
    text = "\n".join(lines) + "\n"
    print(text)
    if a.report:
        open(a.report, "w").write(text)
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
