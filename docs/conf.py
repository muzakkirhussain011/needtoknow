"""Sphinx configuration. Build with: sphinx-build -W -b html docs docs/_build/html"""

import needtoknow

project = "needtoknow"
author = "Syed Muzakkir Hussain"
copyright = "2026, Syed Muzakkir Hussain"
release = needtoknow.__version__
version = release

extensions = ["myst_parser", "sphinx.ext.autodoc", "sphinx.ext.viewcode"]
source_suffix = {".md": "markdown"}
# The architecture decision records describe the project's history (including the gateway it grew
# out of); they are linked from the contributing page on GitHub rather than built into the site.
exclude_patterns = ["_build", "adr"]

myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

# Docstrings use `name` for code, as Markdown does.
default_role = "literal"
autodoc_member_order = "bysource"
autodoc_typehints = "description"

html_theme = "furo"
html_title = "needtoknow"
html_theme_options = {
    "source_repository": "https://github.com/muzakkirhussain011/needtoknow",
    "source_branch": "main",
    "source_directory": "docs/",
}
