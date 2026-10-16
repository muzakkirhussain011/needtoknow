"""Sphinx configuration. Build with: sphinx-build -W -b html docs docs/_build/html"""

import needtoknow

project = "needtoknow"
author = "Syed Muzakkir Hussain"
copyright = "2026, Syed Muzakkir Hussain"
release = needtoknow.__version__
version = release

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx_design",
    "sphinx_copybutton",
]
source_suffix = {".md": "markdown"}
templates_path = ["_templates"]
# The architecture decision records describe the project's history (including the gateway it grew
# out of); they are linked from the contributing page on GitHub rather than built into the site.
exclude_patterns = ["_build", "adr", "_static", "_templates"]

myst_enable_extensions = ["colon_fence", "deflist", "attrs_inline"]
myst_heading_anchors = 3

# Docstrings use `name` for code, as Markdown does.
default_role = "literal"
autodoc_member_order = "bysource"
autodoc_typehints = "description"

# Copy buttons copy the command, not the shell prompt.
copybutton_prompt_text = r"\$ |>>> |\.\.\. "
copybutton_prompt_is_regexp = True

html_theme = "shibuya"
html_title = "needtoknow"
html_baseurl = "https://muzakkirhussain011.github.io/needtoknow/"
html_theme_options = {
    "accent_color": "teal",
    "light_logo": "_static/brand/mark.webp",
    "dark_logo": "_static/brand/mark.webp",
    "github_url": "https://github.com/muzakkirhussain011/needtoknow",
    "og_image_url": "https://muzakkirhussain011.github.io/needtoknow/_static/brand/social-preview.png",
    "nav_links": [
        {"title": "Get started", "url": "quickstart"},
        {
            "title": "Guide",
            "url": "concepts",
            "children": [
                {"title": "Concepts", "url": "concepts", "summary": "Subjects, authorizers, filters, strategies"},
                {"title": "Combining and tenants", "url": "combining", "summary": "Layered rules, multi-tenancy"},
                {"title": "Security model", "url": "security", "summary": "What is guaranteed and what is assumed"},
                {"title": "Operations", "url": "operations", "summary": "Caching, audit, tracing, failure modes"},
                {"title": "Benchmarks", "url": "benchmarks", "summary": "Recall, latency and throughput, measured"},
            ],
        },
        {
            "title": "Integrations",
            "url": "integrations/index",
            "children": [
                {"title": "Overview", "url": "integrations/index", "summary": "Every backend and authorizer"},
                {"title": "PostgreSQL + pgvector", "url": "integrations/pgvector", "summary": "Filter inside SQL"},
                {"title": "Qdrant", "url": "integrations/qdrant", "summary": "Filter as a query_filter"},
                {"title": "Elasticsearch, OpenSearch", "url": "integrations/elasticsearch", "summary": "Filtered kNN"},
                {"title": "OpenFGA", "url": "integrations/openfga", "summary": "Relationship-based access"},
                {"title": "Open Policy Agent", "url": "integrations/opa", "summary": "Policy as code"},
                {"title": "LangChain", "url": "integrations/langchain", "summary": "Retriever and tools"},
                {"title": "LlamaIndex", "url": "integrations/llamaindex", "summary": "Retriever and postprocessor"},
            ],
        },
        {"title": "API", "url": "api"},
    ],
}
# Edit links on every page point at the Markdown source on GitHub.
html_context = {
    "source_type": "github",
    "source_user": "muzakkirhussain011",
    "source_repo": "needtoknow",
    "source_version": "main",
    "source_docs_path": "/docs/",
}
# Only sidebars that work offline: the theme's repository-stats sidebar would make every reader's
# browser call the GitHub API, and the site makes no third-party requests (fonts: see
# _templates/partials/webfonts.html; icons: see _static/ntk-icons.js).
html_sidebars = {"**": ["sidebars/localtoc.html", "sidebars/edit-this-page.html"]}

# Brand assets and the front page's 3D logo (hero3d.js, with three.js vendored in _static/vendor).
html_static_path = ["_static"]
html_css_files = ["brand.css"]
# The theme's script is added at priority 500; the icons must be registered before it starts.
html_js_files = [("ntk-icons.js", {"priority": 400})]
html_favicon = "_static/brand/favicon.svg"
