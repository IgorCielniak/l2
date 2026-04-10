#!/usr/bin/env python3
"""Compatibility wrapper for the L2 compiler implementation.

This module re-exports symbols from l2_main so imports like `import main`
continue to work, while script execution uses a small entrypoint that avoids
re-parsing the full compiler source on every invocation.
"""

from l2_main import *  # noqa: F401,F403
from l2_main import main as _entry_main


if __name__ == "__main__":
    _entry_main()
