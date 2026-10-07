#!/usr/bin/env python3
"""Write the Homebrew formula for a release.

    python3 tools/formula.py 0.1.0 --tarball dist/note-to-self-0.1.0.tar.gz
    python3 tools/formula.py 0.1.0       # downloads the release's archive to hash it

The release workflow (.github/workflows/release.yml) builds the source archive with
`git archive`, attaches it to the GitHub release, and runs this with --tarball, so the formula
pins exactly the file that was built. The formula goes in the tap repository
(github.com/human-genomics/homebrew-tap, as Formula/note-to-self.rb), so that

    brew install human-genomics/tap/note-to-self

works. See RELEASING.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import urllib.request

REPO = "human-genomics/note-to-self"
PYTHON = "python@3.14"  # Homebrew's default Python; any 3.8+ works

TEMPLATE = '''class NoteToSelf < Formula
  desc "Chat with yourself in a local app that saves every note as Markdown"
  homepage "https://github.com/{repo}"
  url "{url}"
  sha256 "{sha256}"
  license "MIT"
  head "https://github.com/{repo}.git", branch: "main"

  depends_on "{python}"

  def install
    libexec.install "notetoself.py", "nts", "web"
    (bin/"notetoself").write <<~SH
      #!/bin/sh
      exec "#{{formula_opt_bin("{python}")}}/python{pyver}" "#{{libexec}}/notetoself.py" "$@"
    SH
  end

  def caveats
    <<~EOS
      To open Note to Self like any other app (from Launchpad, Spotlight or the
      Dock), add it to your Applications folder once:
        notetoself --install-app
      Or run `notetoself` in a terminal. Your notes are saved in ~/NoteToSelf.
      Uninstalling doesn't delete them: use Settings > Delete all data first if
      you want them gone.
    EOS
  end

  test do
    port = free_port
    notes = testpath/"notes"
    pid = spawn bin/"notetoself", "--no-open", "--port", port.to_s, "--dir", notes
    begin
      sleep 3
      ping = shell_output("curl -s -H 'X-NoteToSelf: 1' http://127.0.0.1:#{{port}}/api/ping")
      assert_match '"app":"note-to-self"', ping
      assert_path_exists notes/"Note to Self"
      assert_path_exists notes/".notetoself.json"
    ensure
      Process.kill("TERM", pid)
      Process.wait(pid)
    end
  end
end
'''


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(description="Write the Homebrew formula for a release.")
    p.add_argument("version", help="release version, e.g. 0.1.0 (the tag is v0.1.0)")
    p.add_argument("--tarball", help="local tarball to hash instead of downloading the release")
    p.add_argument("--url", help="URL to put in the formula (default: the release's archive)")
    p.add_argument("--out", help="write here instead of stdout")
    args = p.parse_args(argv)
    if not re.match(r"^\d+\.\d+\.\d+$", args.version):
        p.error("version must look like 1.2.3")
    url = args.url or "https://github.com/%s/releases/download/v%s/note-to-self-%s.tar.gz" % (
        REPO, args.version, args.version)
    if args.tarball:
        with open(args.tarball, "rb") as f:
            data = f.read()
    else:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
    formula = TEMPLATE.format(repo=REPO, url=url, sha256=sha256_of(data), python=PYTHON,
                              pyver=PYTHON.split("@")[1])
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(formula)
    else:
        sys.stdout.write(formula)
    return 0


if __name__ == "__main__":
    sys.exit(main())
