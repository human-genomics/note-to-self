#!/usr/bin/env python3
"""Print a version's section of CHANGELOG.md, for the GitHub release notes:

    python3 tools/release_notes.py 0.1.0

Fails if that section is missing or still says "(unreleased)", or if nts/__init__.py has a
different version, so a half-prepared release can't go out.
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nts import __version__  # noqa: E402


def section(changelog: str, version: str) -> str:
    m = re.search(r"^## %s \(([^)\n]*)\)[ \t]*$" % re.escape(version), changelog, re.M)
    if not m:
        raise ValueError("CHANGELOG.md has no section for %s." % version)
    if m.group(1).strip().lower() == "unreleased":
        raise ValueError("CHANGELOG.md still says %s is unreleased; put the release date in "
                         "its heading." % version)
    rest = changelog[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    body = (rest[:nxt.start()] if nxt else rest).strip()
    if not body:
        raise ValueError("CHANGELOG.md's section for %s is empty." % version)
    return body + "\n"


def main(argv: list = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.exit("usage: release_notes.py VERSION")
    version = args[0].lstrip("v")
    if version != __version__:
        sys.exit("nts/__init__.py says %s, not %s." % (__version__, version))
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
        text = f.read()
    try:
        sys.stdout.write(section(text, version))
    except ValueError as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
