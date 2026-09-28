"""Quorum must run with the network off, so no template, stylesheet or script may point a browser
at another host. Comments and input placeholders are never fetched, so they are ignored; any other
http(s) URL must be local (localhost, the example domains used in help text, or an XML namespace)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "quorum"
URL = re.compile(r"https?://[^\s\"'<>)]+")
ALLOWED = re.compile(r"https?://(localhost|127\.0\.0\.1|([\w-]+\.)*example\.(org|com)|www\.w3\.org/)")
NOT_FETCHED = [
    re.compile(r"<!--.*?-->", re.S),          # HTML comments
    re.compile(r"\{#.*?#\}", re.S),            # Django template comments
    re.compile(r"/\*.*?\*/", re.S),            # CSS and JS block comments
    re.compile(r"(?m)(^|[\s;{}(),])//.*$"),   # JS line comments (not the // inside a URL)
    re.compile(r'placeholder="[^"]*"'),        # a hint in an empty input
]


def code_only(text: str) -> str:
    for pattern in NOT_FETCHED:
        text = pattern.sub(" ", text)
    return text


def external_urls(text: str) -> list[str]:
    return [u for u in URL.findall(code_only(text)) if not ALLOWED.match(u)]


def test_no_template_stylesheet_or_script_references_another_host():
    files = [*(ROOT / "templates").rglob("*.html"),
             *(p for ext in ("html", "css", "js") for p in (ROOT / "static").rglob(f"*.{ext}"))]
    assert files
    offenders = [f"{p.relative_to(ROOT)}: {u}" for p in files for u in external_urls(p.read_text(errors="ignore"))]
    assert not offenders, "these would break offline operation:\n" + "\n".join(offenders)


def test_the_guard_still_catches_real_external_loads():
    assert external_urls('<script src="https://cdn.example.net/x.js"></script>')
    assert external_urls("body{background:url(https://fonts.example.net/a.woff2)}")
    assert external_urls("fetch('https://api.example.net/v1')")
    assert not external_urls('<input type="url" placeholder="https://">')
    assert not external_urls("const a = 1; // https://hyperelliptic.org/EFD/g1p")
    assert not external_urls('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
