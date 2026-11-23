#!/usr/bin/env python3
"""L2 compiler entry point.

Run as a program it hands over to l2_client (compile through the background
daemon, --daemon-* management, or a compile in this process).  Imported, it
re-exports l2_main.  Kept minimal: the script Python starts with is compiled
from source on every run, while imported modules are cached.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 12):
    sys.stderr.write("L2 needs Python 3.12 or newer (found %d.%d)\n" % sys.version_info[:2])
    raise SystemExit(1)

if __name__ == "__main__":
    from l2_client import run_main

    run_main()
else:
    from l2_main import *  # noqa: F401,F403
    from l2_main import main as _entry_main
