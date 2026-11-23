#!/usr/bin/env python3
"""Contract tests for the live parser-stream words: consume, peek,
current token, positions, slices, injection, collection helpers,
ct-parser-expected, checkpoints, and sessions."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import ParseError, ParserCheckpoint, ParserSession, Token


class ParserStreamWordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = l2_main.Compiler()
        self.parser = self.compiler.parser
        self.vm = self.parser.compile_time_vm
        self.load("a b c d")

    def load(self, source: str) -> None:
        self.parser.tokens = l2_main.Reader().tokenize(source, default_path="<memory>")
        self.parser.pos = 0
        self.parser._last_token = None
        self.parser._ct_parser_sessions.clear()

    def run_word(self, name: str, *args):
        word = self.compiler.dictionary.lookup(name)
        self.assertIsNotNone(word, name)
        self.vm.stack = list(args)
        word.compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def lexemes(self, tokens) -> list:
        return [tok.lexeme for tok in tokens]

    def state(self) -> tuple:
        return (self.lexemes(self.parser.tokens), self.parser.pos, self.parser._last_token)

    def test_renamed_and_retired_words(self) -> None:
        for name in ("ct-parser-consume", "ct-parser-inject", "ct-parser-current",
                     "ct-parser-get-pos", "ct-parser-slice"):
            self.assertIsNotNone(self.compiler.dictionary.lookup(name), name)
        for name in ("next-token", "peek-token", "inject-tokens", "inject-lexemes",
                     "ct-current-token", "ct-parser-pos", "ct-parser-diff", "ct-parser-mark",
                     "lexer-new", "lexer-pop", "lexer-peek", "lexer-expect",
                     "lexer-collect-brace", "lexer-push-back"):
            self.assertIsNone(self.compiler.dictionary.lookup(name), name)

    def test_consume_updates_current_and_raises_at_eof(self) -> None:
        self.assertEqual(self.run_word("ct-parser-current"), [None])
        tok = self.run_word("ct-parser-consume")[0]
        self.assertEqual(tok.lexeme, "a")
        self.assertIs(self.run_word("ct-parser-current")[0], tok)
        self.parser.pos = 4
        with self.assertRaises(ParseError):
            self.run_word("ct-parser-consume")
        self.assertEqual(self.parser.pos, 4)

    def test_peek(self) -> None:
        self.assertEqual(self.run_word("ct-parser-peek", 0)[0].lexeme, "a")
        self.assertEqual(self.run_word("ct-parser-peek", 3)[0].lexeme, "d")
        self.assertEqual(self.run_word("ct-parser-peek", 4), [None])
        for bad in (-1, True, "0"):
            with self.assertRaises(ParseError, msg=repr(bad)):
                self.run_word("ct-parser-peek", bad)
        self.assertEqual(self.parser.pos, 0)
        self.assertIsNone(self.parser._last_token)

    def test_position_queries(self) -> None:
        self.parser.pos = 1
        self.assertEqual(self.run_word("ct-parser-get-pos"), [1])
        self.assertEqual(self.run_word("ct-parser-remaining"), [3])
        self.assertEqual(self.run_word("ct-parser-eof?"), [0])
        self.assertEqual(self.lexemes(self.run_word("ct-parser-tail")[0]), ["b", "c", "d"])
        self.parser.pos = 4
        self.assertEqual(self.run_word("ct-parser-eof?"), [1])

    def test_set_pos(self) -> None:
        self.assertEqual(self.run_word("ct-parser-set-pos", 2), [0])
        self.assertEqual(self.parser._last_token.lexeme, "b")
        self.assertEqual(self.run_word("ct-parser-set-pos", 0), [2])
        self.assertIsNone(self.parser._last_token)
        self.assertEqual(self.run_word("ct-parser-set-pos", 4), [0])
        for bad in (5, -1, True):
            with self.assertRaises(ParseError, msg=repr(bad)):
                self.run_word("ct-parser-set-pos", bad)
        self.assertEqual(self.parser.pos, 4)

    def test_slice(self) -> None:
        self.assertEqual(self.lexemes(self.run_word("ct-parser-slice", 1, 3)[0]), ["b", "c"])
        self.assertEqual(self.lexemes(self.run_word("ct-parser-slice", 3, 1)[0]), ["b", "c"])
        self.assertEqual(self.run_word("ct-parser-slice", 2, 2), [[]])
        for args in ((0, 5), (-1, 2), ("m0", 1)):
            with self.assertRaises(ParseError, msg=repr(args)):
                self.run_word("ct-parser-slice", *args)
        self.assertEqual(self.parser.pos, 0)

    def test_inject(self) -> None:
        self.parser.pos = 1
        self.parser._last_token = self.parser.tokens[0]
        x, y = (l2_main._derive_token(self.parser.tokens[0], lex) for lex in ("x", "y"))
        self.assertEqual(self.run_word("ct-parser-inject", [x, y]), [])
        self.assertEqual(self.lexemes(self.parser.tokens), ["a", "x", "y", "b", "c", "d"])
        self.assertEqual(self.parser.pos, 1)
        self.assertEqual(self.parser._last_token.lexeme, "a")
        before = self.state()
        for bad in ([x, "z"], "x", [None]):
            with self.assertRaises(ParseError, msg=repr(bad)):
                self.run_word("ct-parser-inject", bad)
            self.assertEqual(self.state(), before)

    def test_collect_until(self) -> None:
        self.load("a b ; c")
        tokens, found = self.run_word("ct-parser-collect-until", ";")
        self.assertEqual((self.lexemes(tokens), found), (["a", "b"], 1))
        self.assertEqual(self.parser.pos, 3)
        self.assertEqual(self.parser._last_token.lexeme, ";")
        tokens, found = self.run_word("ct-parser-collect-until", ";")
        self.assertEqual((self.lexemes(tokens), found), (["c"], 0))
        self.assertEqual(self.parser.pos, 4)
        self.parser.pos = 0
        with self.assertRaises(ParseError):
            self.run_word("ct-parser-collect-until", "")
        self.assertEqual(self.parser.pos, 0)

    def test_collect_balanced(self) -> None:
        self.load("a ( b ) c ) d")
        tokens, found = self.run_word("ct-parser-collect-balanced", "(", ")")
        self.assertEqual((self.lexemes(tokens), found), (["a", "(", "b", ")", "c"], 1))
        self.assertEqual(self.lexemes(self.parser.tokens[self.parser.pos:]), ["d"])
        self.load('a | b | c')
        tokens, found = self.run_word("ct-parser-collect-balanced", "|", "|")
        self.assertEqual((self.lexemes(tokens), found), (["a"], 1))
        self.load("a ( b")
        tokens, found = self.run_word("ct-parser-collect-balanced", "(", ")")
        self.assertEqual((self.lexemes(tokens), found), (["a", "(", "b"], 0))
        self.assertEqual(self.parser.pos, 3)
        self.load("a")
        for args in (("", ")"), ("(", "")):
            with self.assertRaises(ParseError, msg=repr(args)):
                self.run_word("ct-parser-collect-balanced", *args)
            self.assertEqual(self.parser.pos, 0)

    def test_expected(self) -> None:
        tok, matched = self.run_word("ct-parser-expected", "a")
        self.assertEqual((tok.lexeme, matched), ("a", 1))
        tok, matched = self.run_word("ct-parser-expected", ["z", self.parser.tokens[0]])
        self.assertEqual((tok.lexeme, matched), ("a", 1))
        self.assertEqual(self.run_word("ct-parser-expected", "b"), [None, 0])
        self.assertEqual(self.parser.pos, 0)
        self.assertIsNone(self.parser._last_token)
        self.parser.pos = 4
        self.assertEqual(self.run_word("ct-parser-expected", "a"), [None, 0])
        for bad in ([], "", [""], 7, [7]):
            with self.assertRaises(ParseError, msg=repr(bad)):
                self.run_word("ct-parser-expected", bad)

    def test_checkpoint_restore(self) -> None:
        self.run_word("ct-parser-consume")
        cp = self.run_word("ct-parser-checkpoint")[0]
        self.assertIsInstance(cp, ParserCheckpoint)
        with self.assertRaises(AttributeError):
            cp.pos = 0
        self.run_word("ct-parser-consume")
        self.run_word("ct-parser-consume")
        self.assertEqual(self.run_word("ct-parser-restore", cp), [1])
        self.assertEqual(self.parser.pos, 1)
        self.assertEqual(self.parser._last_token.lexeme, "a")
        # A checkpoint from another parser, or one past the current end, is
        # refused without changing state.
        other = l2_main.Compiler().parser
        foreign = ParserCheckpoint(other, 0, None)
        self.parser.pos = 2
        before = self.state()
        self.assertEqual(self.run_word("ct-parser-restore", foreign), [0])
        self.assertEqual(self.run_word("ct-parser-restore", ParserCheckpoint(self.parser, 9, None)), [0])
        self.assertEqual(self.state(), before)
        for bad in (0, {"pos": 0}, None):
            with self.assertRaises(ParseError, msg=repr(bad)):
                self.run_word("ct-parser-restore", bad)

    def test_session_rollback_undoes_injection(self) -> None:
        session = self.run_word("ct-parser-session-begin")[0]
        self.assertIsInstance(session, ParserSession)
        self.run_word("ct-parser-consume")
        self.run_word("ct-parser-inject", [l2_main._derive_token(self.parser.tokens[0], "x")])
        self.assertEqual(self.run_word("ct-parser-session-rollback", session), [1])
        self.assertEqual(self.state(), (["a", "b", "c", "d"], 0, None))
        # A used session is refused.
        self.assertEqual(self.run_word("ct-parser-session-rollback", session), [0])
        self.assertEqual(self.run_word("ct-parser-session-commit", session), [0])

    def test_sessions_are_strict_lifo(self) -> None:
        outer = self.run_word("ct-parser-session-begin")[0]
        self.run_word("ct-parser-consume")
        inner = self.run_word("ct-parser-session-begin")[0]
        self.run_word("ct-parser-consume")
        before = self.state()
        self.assertEqual(self.run_word("ct-parser-session-commit", outer), [0])
        self.assertEqual(self.run_word("ct-parser-session-rollback", outer), [0])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.run_word("ct-parser-session-commit", inner), [1])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.run_word("ct-parser-session-rollback", outer), [1])
        self.assertEqual(self.state(), (["a", "b", "c", "d"], 0, None))
        for bad in (1, None, ParserCheckpoint(self.parser, 0, None)):
            with self.assertRaises(ParseError, msg=repr(bad)):
                self.run_word("ct-parser-session-commit", bad)


class StdlibMigrationTests(unittest.TestCase):
    """L2 libraries that used the retired words, compiled end to end."""

    def compile_ok(self, source: str) -> str:
        with tempfile.TemporaryDirectory(prefix="l2-stream-") as raw:
            path = Path(raw) / "stream.sl"
            path.write_text(textwrap.dedent(source), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact", "--no-daemon",
                 "-I", str(ROOT)],
                cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_fn_splits_operators_but_not_literals(self) -> None:
        out = self.compile_ok("""
            import stdlib/stdlib.sl
            import libs/fn.sl
            word probe
              list-new
              ct-parser-current "a+b*2" token-with-lexeme list-append
              ct-parser-current "\\"x+y\\"" token-with-lexeme list-append
              ct-parser-current "f(a,b);" token-with-lexeme list-append
              fn-split-tokens
              list-new swap
              dup list-empty? 0 == while do
                list-pop-front drop token-lexeme rot swap list-append swap
                dup list-empty? 0 ==
              end
              drop " " string-join puts cr
            end
            compile-time probe
            fn add(int a, int b){
                return a+b*2;
            }
            word main end
        """)
        self.assertIn('a + b * 2 "x+y" f ( a , b ) ;\n', out)

    def test_foreach_tokens_anchor_at_the_foreach_keyword(self) -> None:
        source = textwrap.dedent("""
            import stdlib/stdlib.sl
            import stdlib/arr.sl
            # Print the line of the '2drop' that foreach generates.
            word line_hook
              dup token-lexeme "2drop" string-equal? if token-line puti cr else drop end
              0
            end
            compile-only
            word install "line_hook" ct-parser-set-token-hook end
            compile-time install
            word main
              [ 1 2 ]
              foreach
                drop
              end
            end
        """)
        foreach_line = source.splitlines().index("  foreach") + 1
        out = self.compile_ok(source)
        self.assertIn(f"{foreach_line}\n", out)


if __name__ == "__main__":
    unittest.main()
