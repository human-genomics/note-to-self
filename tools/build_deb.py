#!/usr/bin/env python3
"""Build the Debian/Ubuntu package, so Linux users can install with apt:

    python3 tools/build_deb.py                  # writes dist/note-to-self_<version>_all.deb
    sudo apt install ./dist/note-to-self_<version>_all.deb

Needs dpkg-deb (Debian, Ubuntu). The package installs the app in /usr/lib/note-to-self, the
`notetoself` command, and a launcher with its icon in your applications. The build is
reproducible: file times come from SOURCE_DATE_EPOCH (or the last commit).
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nts import __version__  # noqa: E402
from nts.desktop import desktop_entry  # noqa: E402

PACKAGE = "note-to-self"
HOMEPAGE = "https://github.com/human-genomics/note-to-self"
MAINTAINER = os.environ.get("DEB_MAINTAINER",
                            "human-genomics <157525599+human-genomics@users.noreply.github.com>")

CONTROL = """\
Package: {package}
Version: {version}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.8)
Recommends: xdg-utils
Suggests: chromium | google-chrome-stable | firefox
Installed-Size: {size}
Maintainer: {maintainer}
Homepage: {homepage}
Description: chat with yourself, saved as Markdown on your computer
 Note to Self looks and works like the "Note to Self" chat in Signal Desktop,
 but it runs entirely on your computer: every message you send is saved as a
 plain Markdown note in ~/NoteToSelf. Open it from your applications or run
 notetoself.
"""

WRAPPER = """\
#!/bin/sh
exec python3 /usr/lib/note-to-self/notetoself.py "$@"
"""

COPYRIGHT = """\
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: note-to-self
Source: {homepage}

Files: *
License: MIT

Files: usr/lib/note-to-self/web/fonts/*
Copyright: The Inter Project Authors (https://github.com/rsms/inter)
License: OFL-1.1
 The full license text is in /usr/lib/note-to-self/web/fonts/OFL.txt.

License: MIT
{mit}
"""

CHANGELOG = """\
{package} ({version}) unstable; urgency=medium

  * Note to Self {version}. For what's new, see CHANGELOG.md in
    {homepage}

 -- {maintainer}  {date}
"""


def source_epoch() -> int:
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ct"], cwd=ROOT, check=True,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return int(out.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        return int(time.time())  # not in a git checkout


def _copy(src: str, dst: str, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    os.chmod(dst, mode)


def _write(dst: str, data: bytes, mode: int = 0o644) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(data)
    os.chmod(dst, mode)


def build_tree(dest: str, version: str = __version__) -> str:
    """Lay out the package's files (and DEBIAN/control) under `dest`."""
    lib = os.path.join(dest, "usr", "lib", PACKAGE)
    _copy(os.path.join(ROOT, "notetoself.py"), os.path.join(lib, "notetoself.py"))
    for name in sorted(os.listdir(os.path.join(ROOT, "nts"))):
        if name.endswith(".py"):
            _copy(os.path.join(ROOT, "nts", name), os.path.join(lib, "nts", name))
    web = os.path.join(ROOT, "web")
    for d, dirs, files in os.walk(web):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for name in sorted(files):
            if not name.startswith("."):
                src = os.path.join(d, name)
                _copy(src, os.path.join(lib, "web", os.path.relpath(src, web)))
    _write(os.path.join(dest, "usr", "bin", "notetoself"), WRAPPER.encode(), 0o755)
    _write(os.path.join(dest, "usr", "share", "applications", "note-to-self.desktop"),
           desktop_entry(["notetoself", "--background"]).encode())
    _copy(os.path.join(web, "icons", "app.svg"),
          os.path.join(dest, "usr", "share", "icons", "hicolor", "scalable", "apps", "note-to-self.svg"))
    doc = os.path.join(dest, "usr", "share", "doc", PACKAGE)
    with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as f:
        mit = "\n".join((" " + line) if line.strip() else " ." for line in f.read().strip().splitlines())
    _write(os.path.join(doc, "copyright"), COPYRIGHT.format(homepage=HOMEPAGE, mit=mit).encode())
    epoch = source_epoch()
    date = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(epoch))
    changelog = CHANGELOG.format(package=PACKAGE, version=version, homepage=HOMEPAGE,
                                 maintainer=MAINTAINER, date=date)
    _write(os.path.join(doc, "changelog.gz"), gzip.compress(changelog.encode(), 9, mtime=0))
    with open(os.path.join(ROOT, "packaging", "linux", "notetoself.1"), encoding="utf-8") as f:
        man = f.read().replace("@VERSION@", version).replace(
            "@DATE@", time.strftime("%B %Y", time.gmtime(epoch)))
    _write(os.path.join(dest, "usr", "share", "man", "man1", "notetoself.1.gz"),
           gzip.compress(man.encode(), 9, mtime=0))

    size = 0
    for d, _dirs, files in os.walk(dest):
        size += sum(os.path.getsize(os.path.join(d, n)) for n in files)
    control = CONTROL.format(package=PACKAGE, version=version, size=(size + 1023) // 1024,
                             maintainer=MAINTAINER, homepage=HOMEPAGE)
    _write(os.path.join(dest, "DEBIAN", "control"), control.encode())

    for d, dirs, files in os.walk(dest):
        os.chmod(d, 0o755)
        for name in files:
            os.utime(os.path.join(d, name), (epoch, epoch))
    for d, _dirs, _files in os.walk(dest, topdown=False):
        os.utime(d, (epoch, epoch))
    return dest


def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(description="Build the .deb package.")
    p.add_argument("--out", default=os.path.join(ROOT, "dist"), help="output folder (default: dist)")
    p.add_argument("--version", default=__version__, help=argparse.SUPPRESS)  # for testing upgrades
    args = p.parse_args(argv)
    if not shutil.which("dpkg-deb"):
        sys.exit("dpkg-deb isn't installed (it comes with Debian and Ubuntu).")
    os.makedirs(args.out, exist_ok=True)
    out = os.path.join(args.out, "%s_%s_all.deb" % (PACKAGE, args.version))
    with tempfile.TemporaryDirectory() as tmp:
        tree = build_tree(os.path.join(tmp, PACKAGE), args.version)
        env = dict(os.environ, SOURCE_DATE_EPOCH=str(source_epoch()))
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", tree, out],
                       check=True, env=env, stdout=subprocess.DEVNULL)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
