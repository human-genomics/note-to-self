#!/usr/bin/env python3
"""Note to Self launcher. Run: python3 notetoself.py [--dir FOLDER] [--port N] [--no-open]"""

import sys

if sys.version_info < (3, 8):
    sys.stderr.write("Note to Self needs Python 3.8 or newer (this is %d.%d).\n"
                     "Get it from https://www.python.org/downloads/\n" % sys.version_info[:2])
    sys.exit(1)

from nts.launch import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
