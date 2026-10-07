"""Release tooling: the .deb layout, the Homebrew formula, and the release notes."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock  # noqa: F401  (unittest.mock below)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import apt_repo  # noqa: E402
import build_deb  # noqa: E402
import formula  # noqa: E402
import release_notes  # noqa: E402

from nts import __version__  # noqa: E402


class TestDeb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nts-deb-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tree = build_deb.build_tree(os.path.join(self.tmp, "pkg"))

    def read(self, *parts):
        with open(os.path.join(self.tree, *parts), encoding="utf-8") as f:
            return f.read()

    def test_layout(self):
        lib = os.path.join(self.tree, "usr", "lib", "note-to-self")
        for rel in ("notetoself.py", "nts/server.py", "nts/store.py", "web/index.html",
                    "web/js/main.js", "web/fonts/OFL.txt"):
            self.assertTrue(os.path.isfile(os.path.join(lib, rel)), rel)
        shipped = sorted(os.path.relpath(os.path.join(d, n), os.path.join(lib, "web"))
                         for d, _, files in os.walk(os.path.join(lib, "web")) for n in files)
        expected = sorted(os.path.relpath(os.path.join(d, n), os.path.join(ROOT, "web"))
                          for d, _, files in os.walk(os.path.join(ROOT, "web")) for n in files
                          if not n.startswith("."))
        self.assertEqual(shipped, expected)
        for d, dirs, files in os.walk(self.tree):
            self.assertNotIn("__pycache__", dirs)
            self.assertFalse([f for f in files if f.endswith(".pyc")])

    def test_command_launcher_and_icon(self):
        self.assertIn('exec python3 /usr/lib/note-to-self/notetoself.py "$@"',
                      self.read("usr", "bin", "notetoself"))
        desktop = self.read("usr", "share", "applications", "note-to-self.desktop")
        for line in ("Exec=notetoself --background", "Icon=note-to-self", "Terminal=false", "Name=Note to Self"):
            self.assertIn(line, desktop.splitlines())
        self.assertTrue(os.path.isfile(os.path.join(
            self.tree, "usr", "share", "icons", "hicolor", "scalable", "apps", "note-to-self.svg")))
        with gzip.open(os.path.join(self.tree, "usr", "share", "man", "man1", "notetoself.1.gz"), "rt") as f:
            man = f.read()
        self.assertIn("Note to Self %s" % __version__, man)
        self.assertNotIn("@", man.replace("@users", ""))  # placeholders filled in
        with gzip.open(os.path.join(self.tree, "usr", "share", "doc", "note-to-self", "changelog.gz"), "rt") as f:
            changelog = f.read()
        self.assertTrue(changelog.startswith("note-to-self (%s) unstable; urgency=medium" % __version__))
        self.assertTrue(all(len(line) <= 80 for line in changelog.splitlines() if line.startswith("  ")))
        if os.name != "nt":
            mode = os.stat(os.path.join(self.tree, "usr", "bin", "notetoself")).st_mode & 0o777
            self.assertEqual(mode, 0o755)
            self.assertEqual(os.stat(os.path.join(self.tree, "usr", "share", "applications",
                                                  "note-to-self.desktop")).st_mode & 0o777, 0o644)

    def test_control(self):
        control = dict(line.split(": ", 1) for line in self.read("DEBIAN", "control").splitlines()
                       if line and not line.startswith(" "))
        self.assertEqual(control["Package"], "note-to-self")
        self.assertEqual(control["Version"], __version__)
        self.assertEqual(control["Architecture"], "all")
        self.assertEqual(control["Depends"], "python3 (>= 3.8)")
        self.assertGreater(int(control["Installed-Size"]), 100)
        self.assertIn("License: MIT", self.read("usr", "share", "doc", "note-to-self", "copyright"))

    @unittest.skipUnless(shutil.which("dpkg-deb"), "needs dpkg-deb")
    def test_build(self):
        out = os.path.join(self.tmp, "dist")
        with redirect_stdout(io.StringIO()):
            build_deb.main(["--out", out])
        deb = os.path.join(out, "note-to-self_%s_all.deb" % __version__)
        fields = subprocess.run(["dpkg-deb", "--field", deb, "Package", "Version"], check=True,
                                stdout=subprocess.PIPE).stdout.decode()
        self.assertIn("Version: " + __version__, fields)
        contents = subprocess.run(["dpkg-deb", "--contents", deb], check=True,
                                  stdout=subprocess.PIPE).stdout.decode()
        self.assertIn("./usr/bin/notetoself", contents)
        self.assertIn("root/root", contents)


class TestFormula(unittest.TestCase):
    def test_formula_pins_the_tarball(self):
        tmp = tempfile.mkdtemp(prefix="nts-formula-")
        self.addCleanup(shutil.rmtree, tmp, True)
        tarball = os.path.join(tmp, "note-to-self-1.2.3.tar.gz")
        with open(tarball, "wb") as f:
            f.write(b"not really a tarball")
        out = os.path.join(tmp, "Formula", "note-to-self.rb")
        formula.main(["1.2.3", "--tarball", tarball, "--out", out])
        with open(out, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("class NoteToSelf < Formula", text)
        self.assertIn('sha256 "%s"' % hashlib.sha256(b"not really a tarball").hexdigest(), text)
        self.assertIn('url "https://github.com/human-genomics/note-to-self/releases/download/'
                      'v1.2.3/note-to-self-1.2.3.tar.gz"', text)
        self.assertIn('depends_on "python@3.14"', text)
        self.assertIn('formula_opt_bin("python@3.14")}/python3.14', text)
        self.assertNotIn("{", text.replace("#{", "").replace("}", ""))  # no unfilled placeholders

    def test_bad_version(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                formula.main(["1.2"])


def make_deb(path, control):
    """A minimal .deb (ar archive), like dpkg-deb makes, without needing dpkg."""
    def member(name, data):
        header = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, 0, 0, 0, "100644", len(data))
        return header.encode("ascii") + data + (b"\n" if len(data) % 2 else b"")
    ctl = io.BytesIO()
    with tarfile.open(fileobj=ctl, mode="w:gz") as tar:
        raw = control.encode("utf-8")
        info = tarfile.TarInfo("./control")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    payload = io.BytesIO()
    tarfile.open(fileobj=payload, mode="w:gz").close()
    with open(path, "wb") as f:
        f.write(b"!<arch>\n" + member("debian-binary", b"2.0\n") + member("control.tar.gz", ctl.getvalue())
                + member("data.tar.gz", payload.getvalue()))


CONTROL = """Package: note-to-self
Version: 1.2.3
Architecture: all
Maintainer: Someone <someone@example.com>
Depends: python3 (>= 3.8)
Description: chat with yourself
 Longer text,
 over two lines.
"""


class TestAptRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nts-apt-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.deb = os.path.join(self.tmp, "note-to-self_1.2.3_all.deb")
        make_deb(self.deb, CONTROL)

    def read(self, *parts, mode="r"):
        with open(os.path.join(self.tmp, "site", *parts), mode) as f:
            return f.read()

    def test_reads_the_control_file(self):
        fields = apt_repo.read_control(self.deb)
        self.assertEqual((fields["Package"], fields["Version"]), ("note-to-self", "1.2.3"))
        self.assertEqual(fields["Description"], "chat with yourself\n Longer text,\n over two lines.")

    def test_repository_layout_and_checksums(self):
        apt_repo.build([self.deb], os.path.join(self.tmp, "site"), url="https://example.com/apt", date=0)
        pool = "pool/main/n/note-to-self/note-to-self_1.2.3_all.deb"
        with open(self.deb, "rb") as f:
            deb = f.read()
        self.assertEqual(self.read(*pool.split("/"), mode="rb"), deb)
        packages = self.read("dists", "stable", "main", "binary-amd64", "Packages")
        self.assertIn("Filename: %s\n" % pool, packages)
        self.assertIn("SHA256: %s\n" % hashlib.sha256(deb).hexdigest(), packages)
        self.assertIn(" over two lines.\n", packages)
        release = self.read("dists", "stable", "Release")
        self.assertIn("Suite: stable", release)
        self.assertIn("Date: Thu, 01 Jan 1970 00:00:00 UTC", release)
        for arch in apt_repo.ARCHES:
            for name in ("Packages", "Packages.gz"):
                data = self.read("dists", "stable", "main", "binary-" + arch, name, mode="rb")
                line = " %s %d main/binary-%s/%s" % (hashlib.sha256(data).hexdigest(), len(data), arch, name)
                self.assertIn(line, release)
        self.assertEqual(gzip.decompress(self.read("dists", "stable", "main", "binary-all", "Packages.gz", mode="rb")).decode(),
                         packages)
        index = self.read("index.html")
        self.assertIn("deb [signed-by=/etc/apt/keyrings/note-to-self.asc] https://example.com/apt stable main", index)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "site", "dists", "stable", "InRelease")))  # unsigned

    @unittest.skipUnless(shutil.which("gpg"), "needs gpg")
    def test_signed(self):
        home = os.path.join(self.tmp, "gnupg")
        os.makedirs(home, mode=0o700)
        env = dict(os.environ, GNUPGHOME=home)
        self.addCleanup(subprocess.run, ["gpgconf", "--homedir", home, "--kill", "all"], capture_output=True)
        subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "Test <test@example.com>",
                        "ed25519", "sign", "never"], env=env, check=True, capture_output=True)
        with unittest.mock.patch.dict(os.environ, {"GNUPGHOME": home}):
            apt_repo.build([self.deb], os.path.join(self.tmp, "site"), key="test@example.com")
        dist = os.path.join(self.tmp, "site", "dists", "stable")
        subprocess.run(["gpg", "--verify", os.path.join(dist, "InRelease")], env=env, check=True, capture_output=True)
        subprocess.run(["gpg", "--verify", os.path.join(dist, "Release.gpg"), os.path.join(dist, "Release")],
                       env=env, check=True, capture_output=True)
        self.assertIn("BEGIN PGP PUBLIC KEY BLOCK", self.read("note-to-self.asc"))


class TestTools(unittest.TestCase):
    def test_every_tool_imports(self):
        # A moved function shouldn't break a script nobody runs until release day.
        import importlib
        for name in ("apt_repo", "build_deb", "formula", "release_notes", "seed", "smoke"):
            self.assertTrue(callable(importlib.import_module(name).main), name)


class TestReleaseNotes(unittest.TestCase):
    CHANGELOG = "# Changelog\n\n## 1.1.0 (unreleased)\n\n- next\n\n## 1.0.0 (2026-10-08)\n\n- first\n- second\n\n## 0.9.0 (2026-09-01)\n\n- old\n"

    def test_section(self):
        self.assertEqual(release_notes.section(self.CHANGELOG, "1.0.0"), "- first\n- second\n")
        self.assertEqual(release_notes.section(self.CHANGELOG, "0.9.0"), "- old\n")

    def test_refuses_unfinished_releases(self):
        with self.assertRaisesRegex(ValueError, "unreleased"):
            release_notes.section(self.CHANGELOG, "1.1.0")
        with self.assertRaisesRegex(ValueError, "no section"):
            release_notes.section(self.CHANGELOG, "2.0.0")

    def test_version_must_match(self):
        with self.assertRaises(SystemExit):
            release_notes.main(["99.0.0"])


if __name__ == "__main__":
    unittest.main()
