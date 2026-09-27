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


def _tokens(css: str, selector: str) -> dict:
    import re

    block = css.split(selector, 1)[1].split("}", 1)[0]
    return {k: v.strip() for k, v in re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block)}


def _contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


# (foreground, background, minimum): body text 4.5:1 (WCAG 1.4.3 AA); icons and UI edges 3:1 (1.4.11)
PAIRS = [("ink", "paper", 4.5), ("ink", "panel", 4.5), ("ink-2", "paper", 4.5), ("ink-2", "sunk", 4.5),
         ("muted", "paper", 4.5), ("muted", "panel", 4.5), ("muted", "sunk", 4.5), ("accent", "panel", 4.5),
         ("accent", "paper", 4.5), ("accent-ink", "accent", 4.5), ("accent-strong", "accent-soft", 4.5),
         ("good", "good-soft", 4.5), ("warn", "warn-soft", 4.5), ("bad", "bad-soft", 4.5), ("tie", "tie-soft", 4.5),
         ("signal", "panel", 3.0), ("field", "panel", 3.0), ("field", "paper", 3.0)]


def test_palette_meets_wcag_contrast_in_both_themes():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[2] / "src/quorum/static/css/app.css").read_text()
    light, dark = _tokens(css, ":root {"), _tokens(css, ':root[data-theme="dark"] {')
    failures = []
    for name, t in (("light", light), ("dark", dark)):
        for fg, bg, need in PAIRS:
            ratio = _contrast(t[fg], t[bg])
            if ratio < need:
                failures.append(f"{name}: --{fg} on --{bg} = {ratio:.2f} (< {need})")
    assert not failures, failures
