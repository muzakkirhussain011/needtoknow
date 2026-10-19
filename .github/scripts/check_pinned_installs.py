"""Fail when a workflow, Dockerfile or shell script runs `pip install` without pinning by hash.

The rule is OpenSSF Scorecard's (Pinned-Dependencies): an install is pinned when it passes
`--require-hashes`, when it is an editable install of local source with `--no-deps`, or when it
installs only `.whl` files. Run from the repository root; it prints each unpinned install and exits 1.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

# A pip install at the start of a command: after `run:`, `RUN` or `sudo`, and through any path.
PIP = re.compile(
    r"""^\s*(?:-\s*)?(?:run:\s*)?(?:RUN\s+)?(?:sudo\s+)?["']?[^\s"']*?"""
    r"""(?:\bpip3?|\bpython3?(?:\.\d+)?["']?\s+-m\s+pip)["']?\s+install\b(?P<args>.*)$"""
)
SEPARATORS = re.compile(r"&&|\|\||;|\|")


def files() -> list[Path]:
    found = set(Path(".github/workflows").glob("*.yml"))
    for path in Path().rglob("*"):
        if any(part in {".git", "node_modules", ".venv"} for part in path.parts) or not path.is_file():
            continue
        if path.name.startswith("Dockerfile") or path.suffix == ".sh":
            found.add(path)
    return sorted(found)


def commands(text: str) -> list[tuple[int, str]]:
    """The pip installs in a file, with backslash continuations joined, each with its first line."""
    joined: list[tuple[int, str]] = []
    start, buffer = 0, ""
    for number, line in enumerate(text.splitlines(), 1):
        if not buffer:
            start = number
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        joined.append((start, buffer + stripped))
        buffer = ""
    found = []
    for number, line in joined:
        if line.lstrip().startswith("#"):
            continue
        for part in SEPARATORS.split(line):
            match = PIP.match(part)
            if match:
                found.append((number, match.group("args")))
    return found


def pinned(args: str) -> bool:
    try:
        words = shlex.split(args, comments=True)
    except ValueError:  # quoting this script cannot follow: check it by hand
        return False
    editable = no_deps = False
    packages: list[str] = []
    for word in words:
        if word == "--require-hashes":
            return True
        if word == "--no-deps":
            no_deps = True
        elif word in ("-e", "--editable"):
            editable = True
        elif not word.startswith("-"):
            packages.append(word)
    if editable:
        return no_deps and all(p == "." or p.startswith(("./", "/")) for p in packages)
    return bool(packages) and all(p.endswith(".whl") for p in packages)


def main() -> int:
    unpinned = [
        f"{path}:{number}: pip install{args}"
        for path in files()
        for number, args in commands(path.read_text(encoding="utf-8"))
        if not pinned(args)
    ]
    for line in unpinned:
        print(f"unpinned: {line}")
    return 1 if unpinned else 0


if __name__ == "__main__":
    sys.exit(main())
