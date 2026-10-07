#!/usr/bin/env python3
"""Build a signed APT repository (a folder of static files) from .deb packages, so that

    sudo apt install note-to-self

works once the repository is added (see the README). The release workflow publishes it on
GitHub Pages.

    python3 tools/apt_repo.py --out site --key KEY dist/note-to-self_0.1.0_all.deb

KEY is a GPG key id or fingerprint in your keyring (signing needs `gpg`); without --key the
repository is unsigned, which apt only accepts with [trusted=yes] (for tests).

Layout (the usual one, so apt and every mirror tool understand it):

    pool/main/n/note-to-self/note-to-self_0.1.0_all.deb
    dists/stable/main/binary-<arch>/Packages(.gz)
    dists/stable/Release, InRelease, Release.gpg
    note-to-self.asc                the public key users add to apt
    index.html                      how to install, for people who open the URL
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import io
import lzma
import os
import shutil
import subprocess
import sys
import tarfile
import time
from typing import Dict, List, Optional, Tuple

SUITE = "stable"
COMPONENT = "main"
# Packages are Architecture: all; list them for every common machine so apt finds them anywhere.
ARCHES = ["amd64", "arm64", "armhf", "i386", "all"]
FIELD_ORDER = ["Package", "Version", "Architecture", "Maintainer", "Installed-Size", "Depends",
               "Recommends", "Suggests", "Section", "Priority", "Homepage", "Description"]
DEFAULT_URL = "https://human-genomics.github.io/note-to-self"


def read_control(deb: str) -> Dict[str, str]:
    """The control fields of a .deb (an `ar` archive holding control.tar.*), without dpkg."""
    with open(deb, "rb") as f:
        data = f.read()
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("%s isn't a .deb" % deb)
    pos = 8
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        body = data[pos + 60:pos + 60 + size]
        pos += 60 + size + (size % 2)
        if name.startswith("control.tar"):
            if name.endswith(".xz"):
                body = lzma.decompress(body)
            elif name.endswith(".gz"):
                body = gzip.decompress(body)
            elif name != "control.tar":
                raise ValueError("%s: can't read %s" % (deb, name))
            with tarfile.open(fileobj=io.BytesIO(body)) as tar:
                for member in tar.getmembers():
                    if member.name in ("./control", "control"):
                        return parse_fields(tar.extractfile(member).read().decode("utf-8"))
    raise ValueError("%s has no control file" % deb)


def parse_fields(text: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    last = None
    for line in text.splitlines():
        if line[:1] in (" ", "\t") and last:
            fields[last] += "\n" + line
        elif ":" in line:
            last, value = line.split(":", 1)
            fields[last] = value.strip()
    return fields


def _hashes(data: bytes) -> Dict[str, str]:
    return {"MD5sum": hashlib.md5(data).hexdigest(), "SHA1": hashlib.sha1(data).hexdigest(),
            "SHA256": hashlib.sha256(data).hexdigest()}


def packages_entry(fields: Dict[str, str], filename: str, data: bytes) -> str:
    out = []
    for name in FIELD_ORDER + sorted(set(fields) - set(FIELD_ORDER)):
        if name in fields:
            out.append("%s: %s" % (name, fields[name]))
    out.append("Filename: " + filename)
    out.append("Size: %d" % len(data))
    for name, value in _hashes(data).items():
        out.append("%s: %s" % (name, value))
    return "\n".join(out) + "\n"


def release_file(indexes: List[Tuple[str, bytes]], date: float) -> str:
    lines = [
        "Origin: Note to Self",
        "Label: Note to Self",
        "Suite: " + SUITE,
        "Codename: " + SUITE,
        "Date: " + time.strftime("%a, %d %b %Y %H:%M:%S UTC", time.gmtime(date)),
        "Architectures: " + " ".join(ARCHES),
        "Components: " + COMPONENT,
        "Description: Note to Self, a chat with yourself saved as Markdown",
    ]
    for title, algo in (("MD5Sum", hashlib.md5), ("SHA1", hashlib.sha1), ("SHA256", hashlib.sha256)):
        lines.append(title + ":")
        for path, data in indexes:
            lines.append(" %s %d %s" % (algo(data).hexdigest(), len(data), path))
    return "\n".join(lines) + "\n"


INDEX_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Note to Self for Ubuntu and Debian</title>
<style>body{{max-width:720px;margin:48px auto;padding:0 16px;font:16px/1.5 system-ui,sans-serif;color:#222}}
pre{{background:#f4f4f6;padding:14px 16px;border-radius:8px;overflow-x:auto}}code{{font-size:14px}}</style>
<h1>Note to Self for Ubuntu and Debian</h1>
<p>This is the APT repository for <a href="https://github.com/human-genomics/note-to-self">Note to Self</a>.
Add it once, then install and update with <code>apt</code>:</p>
<pre><code>{commands}</code></pre>
<p>Packages here: {packages}.</p>
</html>
"""


def install_commands(url: str) -> str:
    return "\n".join([
        "sudo install -m 0755 -d /etc/apt/keyrings",
        "sudo wget -qO /etc/apt/keyrings/note-to-self.asc %s/note-to-self.asc" % url,
        'echo "deb [signed-by=/etc/apt/keyrings/note-to-self.asc] %s stable main" \\' % url,
        "  | sudo tee /etc/apt/sources.list.d/note-to-self.list",
        "sudo apt update && sudo apt install note-to-self",
    ])


def _gpg(args: List[str], key: str) -> None:
    subprocess.run(["gpg", "--batch", "--yes", "--local-user", key] + args, check=True,
                   stdout=subprocess.DEVNULL)


def build(debs: List[str], out: str, key: Optional[str] = None, url: str = DEFAULT_URL,
          date: Optional[float] = None) -> str:
    if os.path.exists(out):
        shutil.rmtree(out)
    entries = []
    names = []
    for deb in sorted(debs):
        fields = read_control(deb)
        pkg = fields["Package"]
        filename = "pool/%s/%s/%s/%s_%s_%s.deb" % (COMPONENT, pkg[0], pkg, pkg, fields["Version"],
                                                    fields["Architecture"])
        os.makedirs(os.path.join(out, os.path.dirname(filename)), exist_ok=True)
        shutil.copyfile(deb, os.path.join(out, filename))
        with open(deb, "rb") as f:
            entries.append(packages_entry(fields, filename, f.read()))
        names.append("%s %s" % (pkg, fields["Version"]))
    packages = "\n".join(entries).encode("utf-8")
    indexes = []
    for arch in ARCHES:
        rel = "%s/binary-%s/Packages" % (COMPONENT, arch)
        packed = gzip.compress(packages, 9, mtime=0)
        for path, data in ((rel, packages), (rel + ".gz", packed)):
            full = os.path.join(out, "dists", SUITE, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
            indexes.append((path, data))
    dist = os.path.join(out, "dists", SUITE)
    with open(os.path.join(dist, "Release"), "w", encoding="utf-8", newline="\n") as f:
        f.write(release_file(indexes, time.time() if date is None else date))
    if key:
        _gpg(["--clearsign", "--output", os.path.join(dist, "InRelease"), os.path.join(dist, "Release")], key)
        _gpg(["--armor", "--detach-sign", "--output", os.path.join(dist, "Release.gpg"),
              os.path.join(dist, "Release")], key)
        with open(os.path.join(out, "note-to-self.asc"), "wb") as f:
            f.write(subprocess.run(["gpg", "--batch", "--armor", "--export", key], check=True,
                                   stdout=subprocess.PIPE).stdout)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(INDEX_HTML.format(commands=html.escape(install_commands(url)),
                                  packages=html.escape(", ".join(names))))
    open(os.path.join(out, ".nojekyll"), "w").close()  # serve the files as they are
    return out


def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(description="Build the APT repository from .deb files.")
    p.add_argument("debs", nargs="+", help=".deb files to include")
    p.add_argument("--out", required=True, help="folder to write the repository to")
    p.add_argument("--key", help="GPG key id or fingerprint to sign with")
    p.add_argument("--url", default=DEFAULT_URL, help="where it will be served (for index.html)")
    args = p.parse_args(argv)
    build(args.debs, args.out, args.key, args.url.rstrip("/"))
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
