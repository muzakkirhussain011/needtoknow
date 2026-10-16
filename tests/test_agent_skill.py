"""The Agent Skill in plugins/needtoknow must stay correct: AI assistants copy what it shows.

Its frontmatter must follow the Agent Skills specification, the plugin and marketplace manifests
must agree, every Python block must parse, and every name it imports from needtoknow must exist.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "needtoknow"
SKILL = PLUGIN / "skills" / "needtoknow"
_BLOCK = re.compile(r"```python\n(.*?)```", re.S)


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "SKILL.md must start with YAML frontmatter"
    fields = {}
    for line in match.group(1).splitlines():
        if line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _blocks():
    for path in sorted(SKILL.rglob("*.md")):
        for code in _BLOCK.findall(path.read_text(encoding="utf-8")):
            yield path.relative_to(SKILL).as_posix(), code


def test_frontmatter_follows_the_agent_skills_specification():
    fields = _frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    name, description = fields["name"], fields["description"]
    assert name == SKILL.name  # must match the folder
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
    assert 0 < len(description) <= 1024
    assert len((SKILL / "SKILL.md").read_text(encoding="utf-8").splitlines()) < 500


def test_references_exist_and_are_one_level_deep():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\((references/[^)]+)\)", text):
        assert (SKILL / target).is_file(), target
    assert all(p.parent in (SKILL, SKILL / "references") for p in SKILL.rglob("*.md"))


def test_the_marketplace_and_the_plugin_agree():
    marketplace = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    (entry,) = marketplace["plugins"]
    assert entry["name"] == plugin["name"] == "needtoknow"
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()


@pytest.mark.parametrize(("where", "code"), list(_blocks()))
def test_python_blocks_parse_and_import_only_what_exists(where, code):
    tree = ast.parse(code, filename=where)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("needtoknow"):
            try:
                module = importlib.import_module(node.module)
            except ModuleNotFoundError as exc:
                if exc.name and not exc.name.startswith("needtoknow"):
                    continue  # an optional extra that is not installed here
                raise
            for alias in node.names:
                assert hasattr(module, alias.name), f"{where}: {node.module} has no {alias.name}"
