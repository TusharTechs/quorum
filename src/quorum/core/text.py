"""Untrusted text in, safe HTML out. Markdown is rendered with raw HTML disabled and the
result is sanitised by nh3 (Rust ammonia) with a small allow-list."""

import nh3
from markdown_it import MarkdownIt

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable("table")

_TAGS = {"p", "br", "strong", "em", "code", "pre", "blockquote", "ul", "ol", "li", "a", "h2", "h3", "h4",
         "table", "thead", "tbody", "tr", "th", "td", "hr"}


def render_markdown(text: str) -> str:
    html = _md.render(text or "")
    return nh3.clean(
        html,
        tags=_TAGS,
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="nofollow noopener ugc",
    )


def clean_line(text: str, limit: int = 200) -> str:
    """Single-line field: strip control characters and collapse whitespace."""
    text = "".join(ch for ch in (text or "") if ch.isprintable())
    return " ".join(text.split())[:limit]
