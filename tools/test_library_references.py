#!/usr/bin/env python3
"""Every word called (or referenced by pointer) from a definition or macro
body in stdlib/*.sl and libs/*.sl must exist once that file is imported:
the --check-references lint.  Unknown words are otherwise reported only
when a definition is emitted or run, so a typo in an unused library word
would never fail a build."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main

LIBRARIES = sorted(
    path.relative_to(ROOT).as_posix()
    for directory in ("stdlib", "libs")
    for path in (ROOT / directory).glob("*.sl")
)


def unresolved(library: str):
    """The --check-references lint over one imported library."""
    with tempfile.TemporaryDirectory(prefix="l2-refs-") as tmp:
        path = Path(tmp) / "refs.sl"
        path.write_text(f"import stdlib.sl\nimport {library}\nword main 0 end\n", encoding="utf-8")
        compiler = l2_main.Compiler(include_paths=[ROOT, ROOT / "stdlib"])
        compiler.compile_file(path)
    return sorted(diag.message for diag in compiler.check_references())


class LibraryReferenceTests(unittest.TestCase):
    def test_every_reference_resolves(self) -> None:
        self.assertGreater(len(LIBRARIES), 10)
        for library in LIBRARIES:
            with self.subTest(library=library):
                self.assertEqual(unresolved(library), [])

    def test_cli_reports_unknown_words_in_unused_definitions(self) -> None:
        import subprocess
        with tempfile.TemporaryDirectory(prefix="l2-refs-") as tmp:
            path = Path(tmp) / "bad.sl"
            path.write_text("word unused typo-word end\nmacro m () `x` emit nope ;\nword main 0 end\n",
                            encoding="utf-8")
            plain = subprocess.run([sys.executable, str(ROOT / "main.py"), "--no-daemon", str(path),
                                    "--no-artifact"], capture_output=True, text=True)
            self.assertEqual(plain.returncode, 0, plain.stderr)
            lint = subprocess.run([sys.executable, str(ROOT / "main.py"), "--no-daemon", str(path),
                                   "--no-artifact", "--check-references"], capture_output=True, text=True)
        self.assertEqual(lint.returncode, 1)
        self.assertIn("'unused' refers to unknown word 'typo-word'", lint.stderr)
        self.assertIn("'m' refers to unknown word 'nope'", lint.stderr)


if __name__ == "__main__":
    unittest.main()
