#!/usr/bin/env python3
"""Regenerate the README screenshots from a running demo stack (docs/img/*.png).

Uses Playwright with the locally installed Chrome, signs in through the demo buttons like a
visitor would, and captures the pages the README shows. Dev tool only:

    pip install playwright pillow
    python3 tools/screenshots.py --base http://localhost:8080
"""

from __future__ import annotations

import argparse
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs/img"


def signed_in(browser, base, role, **kw):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1, **kw)
    page = ctx.new_page()
    page.goto(base + "/")
    page.click(f"text=Continue as {role}")
    page.wait_for_load_state("networkidle")
    return ctx, page


def shot(page, name, full=False, clip_h=None):
    OUT.mkdir(parents=True, exist_ok=True)
    kw = {"path": str(OUT / f"{name}.png"), "full_page": full}
    if clip_h:
        kw.update(full_page=True, clip={"x": 0, "y": 0, "width": 1440, "height": clip_h})
    page.screenshot(**kw)
    print("wrote", kw["path"])


def main():
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8080")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto(base + "/")
        page.wait_for_load_state("networkidle")
        shot(page, "landing")
        page.goto(base + "/e/sample-hack-2026/projects?q=climate")
        page.wait_for_load_state("networkidle")
        shot(page, "gallery")
        ctx.close()

        ctx, page = signed_in(b, base, "organizer")
        page.goto(base + "/o/sample-hack-2026")
        page.wait_for_load_state("networkidle")
        shot(page, "organizer-overview")
        page.goto(base + "/o/sample-hack-2026/results")
        page.wait_for_load_state("networkidle")
        shot(page, "results-certainty", clip_h=1180)
        page.goto(base + "/o/sample-hack-2026")
        page.wait_for_load_state("networkidle")
        page.keyboard.press("Control+k")
        page.fill("#palette-q", "is there a tie for first place?")
        page.wait_for_selector(".palette .answer", timeout=15000)
        page.wait_for_timeout(400)
        shot(page, "ask-quorum")
        ctx.close()

        ctx, page = signed_in(b, base, "organizer", color_scheme="dark")
        page.goto(base + "/o/sample-hack-2026/ops")
        page.wait_for_load_state("networkidle")
        shot(page, "ops-dark")
        ctx.close()

        ctx, page = signed_in(b, base, "judge")
        page.goto(base + page.locator("a[href*='/review/']").first.get_attribute("href"))
        page.wait_for_load_state("networkidle")
        page.fill("#feedback", "The demo runs cleanly and the install worked first time. Consider adding tests for "
                                "the parser and a README section on setup.")
        page.dispatch_event("#feedback", "input")
        page.wait_for_selector(".coach-card .coach-list", timeout=15000)
        page.wait_for_timeout(500)
        page.evaluate("document.querySelector('.crit').scrollIntoView({block: 'start'}); window.scrollBy(0, -90)")
        page.wait_for_timeout(300)
        shot(page, "judge-console")
        ctx.close()
        b.close()


if __name__ == "__main__":
    main()
