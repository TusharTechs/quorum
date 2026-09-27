"""Accessibility basics on every page a person uses: each form control has a programmatic
label, images have alt text, buttons and links have an accessible name, ids are unique,
the document has a language and exactly one h1. Static checks, not a substitute for a
screen-reader pass, but they keep the easy regressions out."""

from html.parser import HTMLParser

import pytest


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.issues, self.for_ids, self.ids, self.controls = [], set(), set(), []
        self.lang, self.h1, self.in_label = False, 0, 0
        self.btn = self.link = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            if a["id"] in self.ids:
                self.issues.append(f"duplicate id {a['id']}")
            self.ids.add(a["id"])
        if tag == "html":
            self.lang = bool(a.get("lang"))
        elif tag == "h1":
            self.h1 += 1
        elif tag == "label":
            self.in_label += 1
            if a.get("for"):
                self.for_ids.add(a["for"])
        elif tag == "img" and "alt" not in a:
            self.issues.append(f"img without alt {a.get('src')}")
        elif tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button", "reset"):
            named = a.get("aria-label") or a.get("aria-labelledby") or a.get("title") or self.in_label
            self.controls.append((tag, a.get("name"), a.get("id"), bool(named)))
        elif tag == "button":
            self.btn = [a.get("aria-label") or a.get("title") or ""]
        elif tag == "a":
            self.link = [a.get("aria-label") or a.get("title") or "", a.get("href")]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "img" and self.link is not None:
            self.link[0] += dict(attrs).get("alt") or ""

    def handle_endtag(self, tag):
        if tag == "label":
            self.in_label = max(0, self.in_label - 1)
        elif tag == "button" and self.btn is not None:
            if not self.btn[0].strip():
                self.issues.append("button without an accessible name")
            self.btn = None
        elif tag == "a" and self.link is not None:
            if not self.link[0].strip():
                self.issues.append(f"link without an accessible name {self.link[1]}")
            self.link = None

    def handle_data(self, data):
        if self.btn is not None:
            self.btn[0] += data
        if self.link is not None:
            self.link[0] += data

    def report(self):
        out = list(self.issues)
        out += [f"unlabelled {t} name={n}" for t, n, i, named in self.controls if not named and i not in self.for_ids]
        if not self.lang:
            out.append("html without lang")
        if self.h1 != 1:
            out.append(f"{self.h1} h1 elements")
        return out


ORG = ["", "/setup", "/participants", "/judges", "/judges/stats", "/ops", "/results", "/results/prj_02",
       "/results/pairwise", "/feedback", "/voting", "/audit", "/data"]


@pytest.mark.django_db
def test_pages_meet_accessibility_basics(client_as):
    from quorum.events.models import Project
    from quorum.judging.models import Assignment

    pid = Project.objects.filter(event__ref="evt_01").first().pk
    aid = Assignment.objects.filter(event__ref="evt_01", judge_role__ref="jdg_24").first().pk
    pages = [(p, None) for p in ("/", "/events", "/projects", f"/p/{pid}", "/e/sample-hack-2026",
                                 "/e/sample-hack-2026/projects", "/e/sample-hack-2026/methodology", "/methodology",
                                 "/verify", "/login", "/signup", "/embed/events/sample-hack-2026/gallery",
                                 "/e/quorum-live-demo")]
    pages += [(p, "participant") for p in ("/e/sample-hack-2026/vote", "/me", "/me/e/sample-hack-2026",
                                           "/me/e/quorum-live-demo/submit", "/settings/tokens")]
    pages += [(p, "judge_a") for p in ("/j/sample-hack-2026", f"/j/sample-hack-2026/review/{aid}")]
    pages += [(f"/o/sample-hack-2026{t}", "organizer") for t in ORG] + [("/o", "organizer")]
    problems = {}
    for path, role in pages:
        r = client_as(role).get(path)
        assert r.status_code == 200, f"{path} as {role}: {r.status_code}"
        s = Scan()
        s.feed(r.content.decode())
        if s.report():
            problems[path] = s.report()
    assert not problems, problems
