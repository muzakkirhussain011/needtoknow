"""Search engine and AI assistant metadata for the documentation site, without third-party extensions.

- Every page gets a description: its front matter's ``description``, or else its first paragraph.
  The theme writes it as ``<meta name="description">`` and the Open Graph description.
- The front page gets a descriptive ``<title>``.
- ``sitemap.xml`` lists every page, for search consoles, and ``llms.txt`` lists every page with its
  description, in the format proposed at llmstxt.org.

A robots.txt would have no effect here: crawlers read it only at the root of the host, and the site
is served under ``/wardowl/``.
"""

from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Any

from docutils import nodes
from sphinx.application import Sphinx

FRONT_PAGE_TITLE = "wardowl: permission-aware retrieval for RAG in Python"
SUMMARY = (
    "wardowl is an open-source Python library for permission-aware retrieval in RAG: it applies your "
    "permission system inside the vector store's search and checks every hit again, so each user "
    "gets only the chunks they are allowed to read."
)
REPOSITORY = "https://github.com/muzakkirhussain011/wardowl"
LIMIT = 160
SKIP = {"genindex", "search", "py-modindex"}
_descriptions: dict[str, str] = {}


def _first_paragraph(doctree: nodes.document) -> str:
    for paragraph in doctree.findall(nodes.paragraph):
        if isinstance(paragraph.parent, (nodes.Admonition, nodes.field_body, nodes.list_item)):
            continue
        text = re.sub(r"\s+", " ", paragraph.astext()).strip()
        if len(text) >= 40:
            return text
    return ""


def _shorten(text: str) -> str:
    if len(text) <= LIMIT:
        return text
    return text[: LIMIT - 1].rsplit(" ", 1)[0].rstrip(",;:") + "\N{HORIZONTAL ELLIPSIS}"


def _page_context(app: Sphinx, pagename: str, templatename: str, context: dict[str, Any], doctree: Any) -> None:
    if doctree is None or pagename in SKIP:
        return
    meta = dict(context.get("meta") or {})
    description = str(meta.get("description") or "") or _shorten(_first_paragraph(doctree))
    if description:
        meta["description"] = description
        context["meta"] = meta
        _descriptions[pagename] = description
    if pagename == app.config.root_doc:
        context["docstitle"] = FRONT_PAGE_TITLE


def _url(app: Sphinx, pagename: str) -> str:
    base = app.config.html_baseurl.rstrip("/") + "/"
    return base if pagename == app.config.root_doc else f"{base}{pagename}.html"


def _write_files(app: Sphinx, exception: Exception | None) -> None:
    if exception is not None or app.builder.format != "html":
        return
    root = app.config.root_doc
    pages = [root, *sorted(p for p in app.env.found_docs if p != root and p not in SKIP)]
    out = Path(app.outdir)

    urls = "".join(f"  <url><loc>{escape(_url(app, p))}</loc></url>\n" for p in pages)
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}</urlset>\n"
    )
    (out / "sitemap.xml").write_text(sitemap, encoding="utf-8")

    lines = [f"# {app.config.project}", "", f"> {SUMMARY}", "", "## Documentation", ""]
    for p in pages:
        title = app.env.titles[p].astext() if p in app.env.titles else p
        description = _descriptions.get(p)
        lines.append(f"- [{title}]({_url(app, p)})" + (f": {description}" if description else ""))
    lines += ["", "## Source", "", f"- [GitHub repository]({REPOSITORY})", ""]
    (out / "llms.txt").write_text("\n".join(lines), encoding="utf-8")


def setup(app: Sphinx) -> dict[str, Any]:
    app.connect("html-page-context", _page_context)
    app.connect("build-finished", _write_files)
    return {"parallel_read_safe": True, "parallel_write_safe": False}
