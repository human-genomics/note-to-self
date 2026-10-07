"""Run the end-to-end tests in one or more browsers.

    pip install -r e2e/requirements.txt
    python -m playwright install chromium firefox webkit
    python -m e2e                                   # Chromium
    python -m e2e --browser chromium,firefox,webkit
    python -m e2e --browser chrome                  # the Google Chrome on this computer
    python -m e2e --headed -k settings              # watch it; only matching tests
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv: list = None) -> int:
    p = argparse.ArgumentParser(prog="python -m e2e", description="Run the end-to-end tests.")
    p.add_argument("--browser", default="chromium",
                   help="comma-separated: chromium, firefox, webkit, chrome (default: chromium)")
    p.add_argument("--headed", action="store_true", help="show the browser while testing")
    p.add_argument("-k", dest="pattern", help="only run tests whose names contain this")
    args = p.parse_args(argv)
    failed = []
    for name in [b.strip() for b in args.browser.split(",") if b.strip()]:
        print("\n=== End-to-end tests in %s" % name, flush=True)
        env = dict(os.environ, E2E_BROWSER=name, E2E_HEADED="1" if args.headed else "")
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "e2e", "-t", ROOT, "-v"]
        if args.pattern:
            cmd += ["-k", args.pattern]
        if subprocess.run(cmd, env=env, cwd=ROOT).returncode != 0:
            failed.append(name)
    if failed:
        print("\nEnd-to-end tests failed in: %s (screenshots in e2e/artifacts/)" % ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
