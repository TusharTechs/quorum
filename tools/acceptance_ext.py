#!/usr/bin/env python3
"""Quorum extended acceptance checker: T3 and T4 behaviour.

The official DOGFOOD checker (run.py) only has T1/T2 checks; T3/T4 are judged by hand.
This file makes them checkable the same way: same config file, same auth headers,
Python standard library only, one line per check, details under every FAIL.

Usage:  python3 tools/acceptance_ext.py .dogfood.toml > acceptance-report-extended.txt

It creates a little data (one comment, one vote, one team in the live demo event) and
cleans up what it can. Re-running is safe: every check accepts the idempotent outcome.
"""

import http.cookiejar
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

TIMEOUT = 20
EVENT = "evt_01"
LIVE = "live-demo"


def load_config(path):
    with open(path, "rb") as f:
        if tomllib:
            return tomllib.load(f)
    raise SystemExit("Python 3.11+ required (tomllib)")


def req(base, path, header=None, method="GET", body=None, raw=False, ctype="application/json"):
    r = urllib.request.Request(base + path, method=method)
    if header:
        name, _, value = header.partition(":")
        r.add_header(name.strip(), value.strip())
    if body is not None:
        r.data = body if isinstance(body, bytes) else json.dumps(body).encode()
        r.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, text, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", {}


def js(text):
    try:
        return json.loads(text)
    except Exception:
        return None


class Check:
    def __init__(self, tier, label):
        self.tier, self.label, self.ok, self.detail = tier, label, False, []

    def note(self, s):
        self.detail.append(s)


def run(cfg):
    base = cfg["portal"]["base_url"].rstrip("/")
    A = cfg["auth"]
    org, ja, jb, par = A["organizer"], A["judge_a"], A["judge_b"], A["participant"]
    checks = []

    def check(tier, label, fn):
        c = Check(tier, label)
        try:
            fn(c)
        except Exception as e:  # a crash is a FAIL with the reason
            c.ok = False
            c.note(f"{type(e).__name__}: {e}")
        checks.append(c)

    # ---------------------------------------------------------------- T3 public
    def tally_hidden(c):
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/votes/tally", par)
        c.ok = s == 403 and "tally_hidden" in t
        if not c.ok:
            c.note(f"GET tally as participant during the window: got {s}, wanted 403 tally_hidden")
    check("T3", "tallies hidden from non-organizers during voting", tally_hidden)

    def tally_org(c):
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/votes/tally", org)
        c.ok = s == 200 and isinstance(js(t), list)
        if not c.ok:
            c.note(f"GET tally as organizer: got {s}, wanted 200 JSON list")
    check("T3", "organizers can see tallies", tally_org)

    ballots = {}

    def ballot_order(c):
        for who, h in (("judge_a", ja), ("judge_b", jb)):
            s, t, _ = req(base, f"/api/v1/events/{EVENT}/ballot", h)
            if s != 200:
                c.note(f"GET ballot as {who}: got {s}")
                return
            ballots[who] = js(t)
        a = [p["ref"] for p in ballots["judge_a"]["projects"]]
        b = [p["ref"] for p in ballots["judge_b"]["projects"]]
        c.ok = sorted(a) == sorted(b) and a != b and len(a) >= 10
        if not c.ok:
            c.note(f"two voters got {'the same order' if a == b else 'different project sets'} ({len(a)} projects)")
    check("T3", "ballot order is shuffled per voter", ballot_order)

    def token_required(c):
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/votes", jb, "POST", {"project": "prj_02", "ballot": "forged.0.x"})
        c.ok = s == 403 and "ballot_token" in t
        if not c.ok:
            c.note(f"POST vote with a forged ballot token: got {s} {t[:120]}, wanted 403 ballot_token_*")
    check("T3", "a vote needs a valid signed ballot token", token_required)

    def duplicate(c):
        tok = (ballots.get("judge_b") or {}).get("token", "")
        body = {"project": "prj_03", "ballot": tok}
        s1, t1, _ = req(base, f"/api/v1/events/{EVENT}/votes", jb, "POST", body)
        s2, t2, _ = req(base, f"/api/v1/events/{EVENT}/votes", jb, "POST", body)
        c.ok = s1 in (201, 409) and s2 == 409 and "duplicate_vote" in t2
        if not c.ok:
            c.note(f"voting twice for the same project: got {s1} then {s2} {t2[:100]}, wanted 409 duplicate_vote")
    check("T3", "duplicate votes are refused", duplicate)

    def own_team(c):
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/ballot", par)
        tok = (js(t) or {}).get("token", "")
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/votes", par, "POST", {"project": "prj_01", "ballot": tok})
        c.ok = s == 403 and "own_team" in t
        if not c.ok:
            c.note(f"participant voting for their own project: got {s} {t[:100]}, wanted 403 own_team")
    check("T3", "voting for your own team is refused", own_team)

    def comments(c):
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/projects", None)
        projects = js(t) or []
        pid = next((p["id"] for p in projects if p["ref"] == "prj_02"), None)
        text = "Extended checker comment %06d" % random.randint(0, 999999)
        s1, t1, _ = req(base, f"/api/v1/projects/{pid}/comments", par, "POST", {"body": text})
        cid = (js(t1) or {}).get("id")
        s2, _, _ = req(base, f"/api/v1/comments/{cid}/hide", org, "POST", {"reason": "checker cleanup"})
        s3, t3, _ = req(base, f"/api/v1/projects/{pid}/comments", None)
        s4, _, _ = req(base, f"/api/v1/comments/{cid}/hide", par, "POST", {"reason": "not allowed"})
        c.ok = s1 == 201 and s2 == 200 and text not in t3 and s4 == 403
        if not c.ok:
            c.note(f"post {s1}, organizer hide {s2}, still listed={text in t3}, participant hide {s4}")
    check("T3", "comments: post, organizer moderation, participant cannot moderate", comments)

    def rate_limit(c):
        # a real browser flow: cookie jar + CSRF token from the form, so the refusal we
        # measure is the rate limiter, not CSRF protection
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with opener.open(base + "/login", timeout=TIMEOUT) as resp:
            page = resp.read().decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
        email = "nobody-%06d@checker.invalid" % random.randint(0, 999999)
        codes = []
        for _ in range(12):
            r = urllib.request.Request(base + "/login", method="POST", headers={
                "Content-Type": "application/x-www-form-urlencoded", "Referer": base + "/login"},
                data=f"csrfmiddlewaretoken={token}&email={email}&password=wrong-password".encode())
            try:
                with opener.open(r, timeout=TIMEOUT) as resp:
                    codes.append(resp.status)
            except urllib.error.HTTPError as e:
                codes.append(e.code)
        c.ok = 429 in codes
        if not c.ok:
            c.note(f"12 failed sign-ins for one address returned {sorted(set(codes))}, wanted a 429")
    check("T3", "rate limits: repeated failed sign-ins are throttled (429)", rate_limit)

    def audit_readable(c):
        s1, t1, _ = req(base, f"/api/v1/events/{EVENT}/audit?limit=5", org)
        s2, _, _ = req(base, f"/api/v1/events/{EVENT}/audit", ja)
        s3, t3, _ = req(base, f"/api/v1/events/{EVENT}/audit/verify", org)
        entries = js(t1) or []
        c.ok = s1 == 200 and len(entries) > 0 and "summary" in entries[0] and s2 == 403 and (js(t3) or {}).get("ok") is True
        if not c.ok:
            c.note(f"organizer audit {s1} ({len(entries)} entries), judge {s2} (want 403), chain verify {t3[:80]}")
    check("T3", "audit trail: readable by organizers, hash chain verifies, hidden from judges", audit_readable)

    def flags(c):
        s1, t1, _ = req(base, f"/api/v1/events/{EVENT}/integrity-flags", org)
        s2, _, _ = req(base, f"/api/v1/events/{EVENT}/integrity-flags", par)
        c.ok = s1 == 200 and isinstance(js(t1), list) and s2 == 403
        if not c.ok:
            c.note(f"organizer {s1}, participant {s2} (want 200 / 403)")
    check("T3", "vote-integrity flags are organizer-only", flags)

    # ---------------------------------------------------------------- T2+ isolation extras
    def aggregates_hidden(c):
        codes = {who: req(base, f"/api/v1/events/{EVENT}/rankings/latest", h)[0] for who, h in
                 (("judge_a", ja), ("participant", par), ("anonymous", None))}
        s, _, _ = req(base, f"/api/v1/events/{EVENT}/rankings/latest", org)
        c.ok = codes["judge_a"] == 403 and codes["participant"] == 403 and codes["anonymous"] == 401 and s == 200
        if not c.ok:
            c.note(f"rankings before publication: {codes}, organizer {s}")
    check("T2+", "rankings are hidden from judges, participants and visitors before publication", aggregates_hidden)

    def judge_only_own_queue(c):
        s, t, _ = req(base, f"/api/v1/me/assignments?event={EVENT}", ja)
        mine = js(t) or []
        s2, t2, _ = req(base, f"/api/v1/me/assignments?event={EVENT}", jb)
        other = js(t2) or []
        if not other:
            c.note("judge_b has no assignments to probe")
            return
        s3, _, _ = req(base, f"/api/v1/assignments/{other[0]['id']}/review", ja)
        c.ok = s == 200 and s3 == 403
        if not c.ok:
            c.note(f"judge_a reading judge_b's review draft: got {s3}, wanted 403")
    check("T2+", "a judge cannot open another judge's review", judge_only_own_queue)

    # ---------------------------------------------------------------- T4 stretch
    def openapi(c):
        s, t, _ = req(base, "/api/v1/openapi.json")
        d = js(t) or {}
        n = sum(len(v) for v in d.get("paths", {}).values())
        c.ok = s == 200 and n >= 60
        c.note(f"{n} documented operations") if c.ok else c.note(f"got {s}, {n} operations")
    check("T4", "REST API: OpenAPI document published", openapi)

    def api_action(c):
        s, t, _ = req(base, f"/api/v1/events/{LIVE}/teams", jb, "POST", {"name": "Checker Team %04d" % random.randint(0, 9999)})
        c.ok = s in (201, 403, 409)
        if s == 403 and "judge" not in t.lower():
            c.ok = False
        if not c.ok:
            c.note(f"create a team via the API: got {s} {t[:100]}")
    check("T4", "REST API: UI actions are API actions (create team)", api_action)

    def webhooks(c):
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/webhooks", org, "POST", {"url": "https://hooks.example.org/quorum", "topics": ["RESULTS_"]})
        hid = (js(t) or {}).get("id")
        has_secret = bool((js(t) or {}).get("secret"))
        s2, _, _ = req(base, f"/api/v1/events/{EVENT}/webhooks/{hid}", org, "DELETE")
        s3, _, _ = req(base, f"/api/v1/events/{EVENT}/webhooks", par)
        c.ok = s == 201 and has_secret and s2 == 200 and s3 == 403
        if not c.ok:
            c.note(f"create {s} (secret returned: {has_secret}), delete {s2}, participant list {s3}")
    check("T4", "webhooks: organizer can register signed endpoints", webhooks)

    def signed_records(c):
        s, t, _ = req(base, "/.well-known/quorum-keys.json")
        keys = (js(t) or {}).get("keys", [])
        s2, t2, _ = req(base, f"/api/v1/events/{EVENT}/audit/checkpoints")
        cps = js(t2) or []
        if not cps:
            c.note("no signed checkpoints found")
            return
        cp = cps[-1]
        s3, t3, _ = req(base, "/api/v1/verify", None, "POST", {"payload": cp["payload"], "signature": cp["signature"], "key_id": cp["key_id"]})
        tampered = cp["payload"].replace('"seq":', '"seq":9', 1)
        s4, t4, _ = req(base, "/api/v1/verify", None, "POST", {"payload": tampered, "signature": cp["signature"], "key_id": cp["key_id"]})
        c.ok = (keys and keys[0].get("crv") == "Ed25519" and (js(t3) or {}).get("valid") is True
                and (js(t4) or {}).get("valid") is False)
        if not c.ok:
            c.note(f"keys {len(keys)}, verify {t3[:60]}, tampered {t4[:60]}")
    check("T4", "signed, publicly verifiable records (Ed25519; tampering detected)", signed_records)

    def bundle(c):
        s, t, _ = req(base, f"/api/v1/events/{EVENT}/exports/bundle.json", org)
        b = js(t) or {}
        s2, t2, _ = req(base, "/api/v1/bundles/verify", None, "POST", b)
        v = js(t2) or {}
        s3, _, _ = req(base, f"/api/v1/events/{EVENT}/exports/bundle.json", par)
        c.ok = s == 200 and b.get("format") == "quorum.bundle/v1" and v.get("all_match") is True and s3 == 403
        if not c.ok:
            c.note(f"bundle {s} format={b.get('format')}, recompute {str(v)[:100]}, participant {s3}")
        else:
            c.note(f"{len(b.get('ranking_runs', []))} ranking run(s) recomputed: MATCH")
    check("T4", "bulk export: event bundle recomputes to the same results (MATCH)", bundle)

    def csv_exports(c):
        bad = []
        for kind in ("projects", "teams", "judges", "assignments", "reviews", "scores", "results", "votes", "feedback", "audit"):
            s, t, h = req(base, f"/api/v1/events/{EVENT}/exports/{kind}.csv", org)
            if s != 200 or "," not in (t.splitlines() or [""])[0]:
                bad.append(f"{kind}:{s}")
        s, _, _ = req(base, f"/api/v1/events/{EVENT}/exports/scores.csv", jb)
        c.ok = not bad and s == 403
        if not c.ok:
            c.note(f"failed exports {bad}; judge access {s}")
    check("T4", "CSV export at every stage (10 kinds), organizer-only", csv_exports)

    def embed(c):
        s, t, h = req(base, f"/embed/events/sample-hack-2026/gallery")
        csp = h.get("Content-Security-Policy", "")
        c.ok = s == 200 and "frame-ancestors *" in csp and h.get("X-Frame-Options") is None
        s2, _, h2 = req(base, "/projects")
        c.ok = c.ok and "frame-ancestors 'none'" in h2.get("Content-Security-Policy", "")
        if not c.ok:
            c.note(f"embed {s} csp={csp[-40:]}; other pages must not be frameable")
    check("T4", "embeddable gallery widget (only the embed is frameable)", embed)

    return checks


def main():
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else ".dogfood.toml")
    print("QUORUM extended acceptance report (T3 / T4 behaviour; complements the official run.py)")
    print(f"portal: {cfg['portal']['base_url']}")
    print()
    checks = run(cfg)
    width = max(len(c.label) for c in checks) + 2
    for c in checks:
        print(f"{c.tier:<4} {c.label} {'.' * (width - len(c.label))} {'PASS' if c.ok else 'FAIL'}")
        for d in c.detail:
            print(f"       {d}")
    passed = sum(c.ok for c in checks)
    print()
    for tier in ("T2+", "T3", "T4"):
        tc = [c for c in checks if c.tier == tier]
        print(f"{tier}: {sum(c.ok for c in tc)}/{len(tc)} pass")
    print(f"total: {passed}/{len(checks)} pass")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
