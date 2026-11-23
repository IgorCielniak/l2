#!/usr/bin/env python3
"""Seeded fuzz tests (deterministic, a few seconds):

* the tokenizer: Reader.tokenize (flattened compiler source) and
  Reader.tokenize_text (ct-tokenizer-run) agree on lexemes and offsets, and
  every origin covers exactly its lexeme;
* rewrites: random rule sets always end, with output or a rewrite
  diagnostic (loop detection or the step budget), never a Python error;
* quotation splicing: random quotations with `$( )` unquotes and pasting
  give the expected tokens or a syntax diagnostic.

Set L2_FUZZ_ITERATIONS to run longer.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, CompileTimeError, ParseError

ITERATIONS = int(os.environ.get("L2_FUZZ_ITERATIONS", "300"))
EXPECTED_ERRORS = (ParseError, CompileError, CompileTimeError)

TOKEN_PIECES = [
    "a", "b1", "foo", "-3", "42", "0x1F", "'c'", "'\\n'", '"str"', '"a b"', '"\\""',
    "`", "\\`", "\\\\", "$x", "$(", ")", "(", "{", "}", "[", "]", ";", ",", "#c\n",
    ":asm", "f", "mov", "rax,", "; it's\n", "é", "→", "\t", " ", " ", " ", "\n", "\r\n",
]


def random_text(rng: random.Random) -> str:
    return "".join(rng.choice(TOKEN_PIECES) for _ in range(rng.randint(1, 25)))


class TokenizerFuzz(unittest.TestCase):
    def test_entry_points_agree(self) -> None:
        rng = random.Random(1234)
        reader = l2_main.Reader()
        for _ in range(ITERATIONS * 5):
            text = random_text(rng)
            try:
                flat = reader.tokenize(text, None, "<fuzz>")
            except ParseError as flat_error:
                with self.assertRaises(ParseError, msg=repr(text)) as caught:
                    reader.tokenize_text(text, "<fuzz>")
                self.assertEqual(caught.exception.code, flat_error.code, repr(text))
                continue
            exact = reader.tokenize_text(text, "<fuzz>")
            self.assertEqual([t.lexeme for t in flat], [t.lexeme for t in exact], repr(text))
            for token in exact:
                origin = token.origin
                self.assertEqual(text[origin.start:origin.end], token.lexeme, repr(text))
            for left, right in zip(flat, exact):
                self.assertEqual((left.start, left.end), (right.start, right.end), repr(text))


class CompileFuzzBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-fuzz-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def parse(self, source: str):
        path = self.dir / "f.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        compiler = l2_main.Compiler(include_paths=[ROOT, ROOT / "stdlib"])
        compiler.parse_file(path)
        return compiler


class RewriteFuzz(CompileFuzzBase):
    WORDS = ["p", "q", "r", "s"]

    def lexeme_list(self, rng: random.Random, low: int) -> str:
        items = [rng.choice(self.WORDS) for _ in range(rng.randint(low, 3))]
        return "list-new " + " ".join(f'"{item}" list-append' for item in items)

    def test_random_rule_sets_terminate(self) -> None:
        rng = random.Random(99)
        outcomes = {"ok": 0, "diagnostic": 0}
        for index in range(max(20, ITERATIONS // 10)):
            rules = []
            for number in range(rng.randint(1, 4)):
                stage = rng.choice(["reader", "grammar"])
                rules.append(f'"r{number}" {self.lexeme_list(rng, 1)} {self.lexeme_list(rng, 0)} '
                             f"ct-rewrite-add-{stage}-named drop")
            body = " ".join(rng.choice(self.WORDS) for _ in range(rng.randint(1, 8)))
            source = f"""
                import stdlib.sl
                word p end word q end word r end word s end
                word setup {" ".join(rules)} end
                compile-time setup
                word main {body} end
            """
            with self.subTest(index=index, source=source):
                try:
                    self.parse(source)
                    outcomes["ok"] += 1
                except EXPECTED_ERRORS as exc:
                    code = exc.diagnostic.code if exc.diagnostic is not None else ""
                    self.assertTrue(code.startswith("rewrite-") or code == "compilation-failed",
                                    f"unexpected code {code}: {exc}")
                    outcomes["diagnostic"] += 1
        self.assertGreater(outcomes["ok"], 0)


class QuotationFuzz(CompileFuzzBase):
    def test_random_unquotes(self) -> None:
        rng = random.Random(7)
        pieces = [
            ("a", ["a"]), ("b c", ["b", "c"]),
            ("$( 1 2 + )", ["3"]), ('$( "w" )', ["w"]), ("$( `x y` )", ["x", "y"]),
            ('pre$( "m" )', ["prem"]), ('$( "m" )-s', ["m-s"]),
            ('$( list-new "l1" list-append "l2" list-append )', ["l1", "l2"]),
        ]
        bad = ["$( 1 2 )", "$( map-new )", '$( "two words" )', 'p$( `x y` )']
        for index in range(max(20, ITERATIONS // 5)):
            chosen = [rng.choice(pieces) for _ in range(rng.randint(1, 5))]
            broken = rng.random() < 0.25
            parts = [text for text, _ in chosen]
            if broken:
                parts.insert(rng.randint(0, len(parts)), rng.choice(bad))
            expected = [lexeme for _, lexemes in chosen for lexeme in lexemes]
            checks = " ".join(
                f'dup {i} syntax-get token-lexeme "{lexeme}" string-equal? static-assert'
                for i, lexeme in enumerate(expected))
            source = f"""
                import stdlib.sl
                word check
                  `{" ".join(parts)}`
                  dup syntax-length {len(expected)} == static-assert
                  {checks}
                  drop
                end
                compile-time check
                word main 0 end
            """
            with self.subTest(index=index, source=source):
                if broken:
                    with self.assertRaises(EXPECTED_ERRORS) as caught:
                        self.parse(source)
                    code = caught.exception.diagnostic.code if caught.exception.diagnostic else ""
                    self.assertIn(code, ("syntax-splice-invalid", "compilation-failed"), str(caught.exception))
                else:
                    self.parse(source)


if __name__ == "__main__":
    unittest.main()
