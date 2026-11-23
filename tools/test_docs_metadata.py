#!/usr/bin/env python3
"""The --docs explorer lists macros and constants from what the compiler
knows about them: parameters, `doc` metadata, declaration source."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import docs
import l2_main

SOURCE = """\
import stdlib.sl

# Adds two values.
macro add2 (a b) `$a $b +` emit ;

macro twice (x) `$x $x` emit ;
word set-doc
  "twice" map-new "metadata" map-new "doc" "Repeats its argument." map-set map-set
  ct-macro-set-by-name drop
end
compile-time set-doc

# The answer.
const ANSWER 42

macro undocumented `1` emit ;
"""


class DocsMetadataTests(unittest.TestCase):
    def entries(self, **options):
        docs.configure_definition_metadata(l2_main._collect_definition_metadata)
        try:
            with tempfile.TemporaryDirectory(prefix="l2-docs-") as tmp:
                path = Path(tmp) / "lib.sl"
                path.write_text(SOURCE, encoding="utf-8")
                found = docs._collect_docs_for_runtime(
                    [path], include_undocumented=options.get("all", False), include_private=False,
                    include_tests=False, include_macros=False, extra_roots=[])
                return {entry.name: entry for entry in found}
        finally:
            docs.configure_definition_metadata(None)

    def test_macros_and_consts_from_the_compiler(self) -> None:
        found = self.entries()
        self.assertEqual((found["add2"].kind, found["add2"].stack_effect, found["add2"].description),
                         ("macro", "macro (a b)", "Adds two values."))
        self.assertEqual(found["twice"].description, "Repeats its argument.")
        self.assertEqual((found["ANSWER"].kind, found["ANSWER"].stack_effect, found["ANSWER"].description),
                         ("const", "-> 42", "The answer."))
        self.assertEqual(found["add2"].line, 4)
        self.assertNotIn("undocumented", found)

    def test_undocumented_with_all(self) -> None:
        self.assertIn("undocumented", self.entries(all=True))


if __name__ == "__main__":
    unittest.main()
