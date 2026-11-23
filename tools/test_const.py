#!/usr/bin/env python3
"""`const NAME value` declarations are recorded cheaply and become
candidates only when first used."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import ParseError


class ConstTests(unittest.TestCase):
    def parse(self, source: str):
        with tempfile.TemporaryDirectory(prefix="l2-const-") as tmp:
            path = Path(tmp) / "c.sl"
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
            compiler = l2_main.Compiler(include_paths=[ROOT, ROOT / "stdlib"])
            compiler.parse_file(path)
            return compiler

    def test_unused_consts_stay_pending(self) -> None:
        compiler = self.parse("""
            const A 1
            const B 2
            word main A drop end
        """)
        dictionary = compiler.dictionary
        self.assertIn("B", dictionary.pending_consts)
        self.assertNotIn("A", dictionary.pending_consts)
        self.assertIsNotNone(dictionary.words.get("A"))
        self.assertTrue(dictionary.has_name("B"))
        self.assertIn("B", dictionary.names())

    def test_stdlib_constants_are_lazy(self) -> None:
        compiler = self.parse("""
            import stdlib.sl
            word main FD_STDOUT drop end
        """)
        pending = compiler.dictionary.pending_consts
        self.assertGreater(len(pending), 800)
        self.assertNotIn("FD_STDOUT", pending)
        self.assertIn("syscall.write.num", pending)

    def test_lookup_materializes_a_macro(self) -> None:
        compiler = self.parse("const A 7\n")
        word = compiler.dictionary.lookup("A")
        self.assertIsNotNone(word)
        self.assertIsNotNone(word.macro_def)
        self.assertEqual([t.lexeme for t in word.macro_def.body_tokens.tokens], ["`", "7", "`", "emit"])
        self.assertIn("const A 7", word.source_text)

    def test_errors(self) -> None:
        for source in ("word f const A 1 end\n", "const\n", "const A\n", "const A `\n", "const 9x 1\n"):
            with self.subTest(source=source):
                with self.assertRaises(ParseError):
                    self.parse(source)


if __name__ == "__main__":
    unittest.main()
