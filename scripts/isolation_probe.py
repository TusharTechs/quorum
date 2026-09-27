#!/usr/bin/env python3
"""Live isolation probe: fire cross-role requests at a running Quorum and report any that
should have been refused but were not. Standard library only; reads .dogfood.toml.

    python3 scripts/isolation_probe.py .dogfood.toml

It reads the OpenAPI document, fills path parameters with real identifiers learned from
the organizer's view, and for every GET operation checks that each lower role gets the
status the policy says it must (probes are GET-only, so running it changes nothing).
"""

import json
import re
import sys
import tomllib
import urllib.error
import urllib.request

ORG_ONLY = re.compile(r"/(ops|judges$|conflicts|rankings|ties|feedback|audit$|audit/verify|integrity-flags|webhooks|exports)")


def get(base, path, header=None):
    r = urllib.request.Request(base + path)
    if header:
        k, _, v = header.partition(":")
        r.add_header(k.strip(), v.strip())
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main():
    cfg = tomllib.load(open(sys.argv[1] if len(sys.argv) > 1 else ".dogfood.toml", "rb"))
    base = cfg["portal"]["base_url"].rstrip("/")
    A = cfg["auth"]
    roles = {"anonymous": None, "participant": A["participant"], "judge_a": A["judge_a"], "judge_b": A["judge_b"]}
    spec = json.loads(get(base, "/api/v1/openapi.json")[1])
    _, projects = get(base, "/api/v1/events/evt_01/projects")
    pids = [p["id"] for p in json.loads(projects)]
    _, judges = get(base, "/api/v1/events/evt_01/judges", A["organizer"])
    jrefs = [j["ref"] for j in json.loads(judges)]
    kinds = ["projects", "teams", "judges", "assignments", "reviews", "scores", "results", "votes", "feedback",
             "comparisons", "audit"]
    probes, failures = 0, []
    for path, item in spec["paths"].items():
        if "get" not in item:
            continue
        rest = path.replace("/api/v1/events/{e}", "")
        org_only = bool(ORG_ONLY.search(rest)) or rest in ("/pairwise", "/certificates")
        for_values = [{}]
        if "{j}" in path:
            for_values = [{"j": j} for j in jrefs]
        if "{kind}" in path:
            for_values = [{"kind": k} for k in kinds]
        if "{pid}" in path:
            for_values = [{"pid": p} for p in pids[:5]]
        for vals in for_values:
            url = path.replace("{e}", "evt_01").replace("{ref}", "prj_02")
            for k, v in vals.items():
                url = url.replace("{" + k + "}", v)
            if "{" in url:
                continue
            for role, h in roles.items():
                status, _ = get(base, url, h)
                probes += 1
                if "/judges/{j}/scores" in path:
                    own = {"judge_a": "jdg_24", "judge_b": "jdg_26"}.get(role)
                    if vals["j"] != own and status not in (401, 403):
                        failures.append((role, url, status))
                elif org_only and status not in (401, 403):
                    failures.append((role, url, status))
    print(f"isolation probe: {probes} cross-role requests, {len(failures)} unexpected successes")
    for f in failures[:30]:
        print("  LEAK?", *f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
