#!/usr/bin/env python3
"""Contract tests for the default tokenizer: fixed lexical rules,
the compiler-scoped configuration map, ct-tokenizer-run, and the raw-source
cursor."""

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
from l2_main import ParseError, Reader, SourceCursor, SourceOrigin, Token

DEFAULT_SEPARATORS = ["(", ")", "{", "}", ";", ",", "[", "]"]


def lexemes(source: str, reader: Reader = None) -> list:
    return [tok.lexeme for tok in (reader or Reader()).tokenize(source)]


class CompilerWordMixin:
    def setUp(self) -> None:
        self.compiler = l2_main.Compiler()
        self.vm = self.compiler.parser.compile_time_vm

    def run_word(self, name: str, *args):
        word = self.compiler.dictionary.lookup(name)
        self.assertIsNotNone(word, name)
        self.vm.stack = list(args)
        word.compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def assertCode(self, code: str, name: str, *args) -> None:
        with self.assertRaises(ParseError) as caught:
            self.run_word(name, *args)
        self.assertEqual(caught.exception.code, code)


class LexicalRuleTests(unittest.TestCase):
    def test_default_separators_split_words(self) -> None:
        self.assertEqual(lexemes("f(a,b)[c]{d};"),
                         ["f", "(", "a", ",", "b", ")", "[", "c", "]", "{", "d", "}", ";"])

    def test_semicolon_is_a_separator_before_letters(self) -> None:
        self.assertEqual(lexemes(";end"), [";", "end"])

    def test_operators_stay_in_words(self) -> None:
        self.assertEqual(lexemes("a+b c-d e*f/g%h"), ["a+b", "c-d", "e*f/g%h"])

    def test_backticks_are_always_own_tokens(self) -> None:
        self.assertEqual(lexemes("foo`bar`"), ["foo", "`", "bar", "`"])
        self.assertEqual(lexemes("``"), ["`", "`"])

    def test_escaped_backtick_is_one_token(self) -> None:
        self.assertEqual(lexemes("a\\`b"), ["a", "\\`", "b"])
        self.assertEqual(lexemes("\\$name \\\\ a\\b"), ["\\$name", "\\\\", "a\\b"])

    def test_escaped_backslash_before_closing_backtick(self) -> None:
        # `\\` right before a backtick is an escaped backslash, so the
        # backtick still closes the quotation.
        self.assertEqual(lexemes("`a \\\\`"), ["`", "a", "\\\\", "`"])
        self.assertEqual(lexemes("x\\\\`"), ["x", "\\\\", "`"])
        self.assertEqual(lexemes("a\\\\b"), ["a\\\\b"])

    def test_raw_asm_bodies_are_one_token(self) -> None:
        self.assertEqual(lexemes(":asm f { ; it's } a comment\n ret }"),
                         [":asm", "f", "{", "; it's } a comment\n ret ", "}"])
        self.assertEqual(lexemes(":asm f { }"), [":asm", "f", "{", "}"])
        # Inside a quotation the body stays tokenized ($name splices work).
        self.assertEqual(lexemes("`:asm f { mov rax, $x }`"),
                         ["`", ":asm", "f", "{", "mov", "rax", ",", "$x", "}", "`"])

    def test_backticks_inside_literals_and_comments_are_text(self) -> None:
        self.assertEqual(lexemes('"a`b" \'`\' # x`y\nz'), ['"a`b"', "'`'", "z"])

    def test_fixed_whitespace_set(self) -> None:
        # U+2003 and U+3000 are White_Space; U+001C and U+200B are not.
        self.assertEqual(lexemes("a b　c"), ["a", "b", "c"])
        self.assertEqual(lexemes("a\u001cb a​b"), ["a\u001cb", "a​b"])

    def test_comment_ends_at_any_line_break(self) -> None:
        self.assertEqual(lexemes("a # c\rb # d\r\nc # e\nd"), ["a", "b", "c", "d"])

    def test_words_end_at_literal_and_comment_openers(self) -> None:
        self.assertEqual(lexemes('a"s"b#c\n'), ["a", '"s"', "b"])
        self.assertEqual(lexemes("a'x'b"), ["a", "'x'", "b"])

    def test_character_literals(self) -> None:
        for text in ("'a'", "'\\n'", "'\\''", "'\\x41'", "'é'", "'\n'", "'\r'"):
            self.assertEqual(lexemes(text), [text], repr(text))

    def test_malformed_character_literals(self) -> None:
        for text in ("''", "'ab'", "'\\x4'", "'\r\n'"):
            with self.assertRaises(ParseError, msg=repr(text)) as caught:
                lexemes(text)
            self.assertEqual(caught.exception.code, "lexer-invalid-char-literal")

    def test_unterminated_literals_span_to_eof(self) -> None:
        cases = (('x "abc', "lexer-unterminated-string"), ("x 'abc", "lexer-unterminated-char"),
                 ('x "a\\"', "lexer-unterminated-string"))
        for text, code in cases:
            with self.assertRaises(ParseError, msg=text) as caught:
                Reader().tokenize_text(text, "/t.sl")
            self.assertEqual(caught.exception.code, code)
            primary = caught.exception.diagnostic.primary
            self.assertEqual((primary.path, primary.start_line, primary.start_column, primary.end), ("/t.sl", 1, 3, len(text)))

    def test_literals_may_span_lines(self) -> None:
        self.assertEqual(lexemes('"a\nb" c'), ['"a\nb"', "c"])


class ConfigurationTests(CompilerWordMixin, unittest.TestCase):
    def test_default_config(self) -> None:
        self.assertEqual(self.run_word("ct-tokenizer-get-config"), [{"separators": DEFAULT_SEPARATORS}])

    def test_snapshot_is_detached(self) -> None:
        config = self.run_word("ct-tokenizer-get-config")[0]
        config["separators"].append("+")
        config["extra"] = 1
        self.assertEqual(self.run_word("ct-tokenizer-get-config"), [{"separators": DEFAULT_SEPARATORS}])

    def test_set_replaces_active_set(self) -> None:
        self.run_word("ct-tokenizer-set-config", {"separators": ["=>", "=", "+", "+"]})
        self.assertEqual(self.run_word("ct-tokenizer-get-config"), [{"separators": ["=>", "=", "+"]}])
        tokens = self.run_word("ct-tokenizer-run", "foo=>bar a==b c+d (x)", "<t>")[0]
        self.assertEqual([t.lexeme for t in tokens],
                         ["foo", "=>", "bar", "a", "=", "=", "b", "c", "+", "d", "(x)"])

    def test_longest_separator_wins(self) -> None:
        self.run_word("ct-tokenizer-set-config", {"separators": ["-", "->", "->>"]})
        tokens = self.run_word("ct-tokenizer-run", "a->>b->c-d", "<t>")[0]
        self.assertEqual([t.lexeme for t in tokens], ["a", "->>", "b", "->", "c", "-", "d"])

    def test_multi_character_separator_with_backslash(self) -> None:
        self.run_word("ct-tokenizer-set-config", {"separators": ["\\\\"]})
        tokens = self.run_word("ct-tokenizer-run", "a\\\\b c\\`", "<t>")[0]
        self.assertEqual([t.lexeme for t in tokens], ["a", "\\\\", "b", "c", "\\`"])

    def test_invalid_configs_leave_config_unchanged(self) -> None:
        bad = (
            "not a map",
            {},
            {"separators": ["("], "other": 1},
            {"separators": "("},
            {"separators": [""]},
            {"separators": [1]},
            {"separators": ["a b"]},
            {"separators": ["　"]},
            {"separators": ['"']},
            {"separators": ["'"]},
            {"separators": ["`"]},
            {"separators": ["#"]},
        )
        for config in bad:
            self.assertCode("tokenizer-config-invalid", "ct-tokenizer-set-config", config)
            self.assertEqual(self.run_word("ct-tokenizer-get-config"),
                             [{"separators": DEFAULT_SEPARATORS}], repr(config))

    def test_empty_separator_list_is_valid(self) -> None:
        self.run_word("ct-tokenizer-set-config", {"separators": []})
        tokens = self.run_word("ct-tokenizer-run", "f(a,b)", "<t>")[0]
        self.assertEqual([t.lexeme for t in tokens], ["f(a,b)"])

    def test_config_is_per_compiler(self) -> None:
        self.run_word("ct-tokenizer-set-config", {"separators": ["+"]})
        other = l2_main.Compiler()
        self.assertEqual(other.parser.reader.get_config(), {"separators": DEFAULT_SEPARATORS})

    def test_retired_words(self) -> None:
        for name in ("add-token", "add-token-chars"):
            self.assertIsNone(self.compiler.dictionary.lookup(name), name)


class TokenizerRunTests(CompilerWordMixin, unittest.TestCase):
    def test_origins_use_original_text_and_line_breaks(self) -> None:
        text = "a\r\nbc\rd\n\t'x' été"
        tokens = self.run_word("ct-tokenizer-run", text, "/src/m.sl")[0]
        self.assertTrue(all(isinstance(t, Token) for t in tokens))
        got = [(t.lexeme, t.origin.path, t.origin.start, t.origin.end,
                t.origin.start_line, t.origin.start_column, t.origin.end_line, t.origin.end_column)
               for t in tokens]
        self.assertEqual(got, [
            ("a", "/src/m.sl", 0, 1, 1, 1, 1, 2),
            ("bc", "/src/m.sl", 3, 5, 2, 1, 2, 3),
            ("d", "/src/m.sl", 6, 7, 3, 1, 3, 2),
            ("'x'", "/src/m.sl", 9, 12, 4, 2, 4, 5),
            ("été", "/src/m.sl", 13, 16, 4, 6, 4, 9),
        ])
        self.assertTrue(all(t.expansion_chain == () for t in tokens))

    def test_multiline_literal_end_coordinate(self) -> None:
        tok = self.run_word("ct-tokenizer-run", '"a\nb"', "<t>")[0][0]
        self.assertEqual((tok.origin.end_line, tok.origin.end_column), (2, 3))

    def test_requires_strings(self) -> None:
        with self.assertRaises(ParseError):
            self.run_word("ct-tokenizer-run", "a", 1)
        with self.assertRaises(ParseError):
            self.run_word("ct-tokenizer-run", 1, "<t>")

    def test_does_not_touch_live_stream(self) -> None:
        parser = self.compiler.parser
        parser.tokens = Reader().tokenize("x y", default_path="<memory>")
        parser.pos = 0
        self.run_word("ct-tokenizer-run", "a b c", "<t>")
        self.assertEqual([t.lexeme for t in parser.tokens], ["x", "y"])
        self.assertEqual(parser.pos, 0)


class SourceCursorTests(CompilerWordMixin, unittest.TestCase):
    def new(self, text: str = "ab\r\ncé", path: str = "rel/p.sl") -> SourceCursor:
        cursor = self.run_word("source-cursor-new", text, path)[0]
        self.assertIsInstance(cursor, SourceCursor)
        return cursor

    def test_read_peek_eof(self) -> None:
        cursor = self.new("aé")
        self.assertEqual(self.run_word("source-cursor-peek", cursor), [ord("a")])
        self.assertEqual(self.run_word("source-cursor-pos", cursor), [0])
        self.assertEqual(self.run_word("source-cursor-read", cursor), [cursor, ord("a")])
        self.assertEqual(self.run_word("source-cursor-eof?", cursor), [0])
        self.assertEqual(self.run_word("source-cursor-read", cursor), [cursor, 0xE9])
        self.assertEqual(self.run_word("source-cursor-eof?", cursor), [1])
        self.assertEqual(self.run_word("source-cursor-pos", cursor), [2])
        for name in ("source-cursor-peek", "source-cursor-read"):
            self.assertCode("source-cursor-eof", name, cursor)
            self.assertEqual(cursor.pos, 2)

    def test_crlf_reads_two_code_points(self) -> None:
        cursor = self.new("\r\n")
        self.assertEqual(self.run_word("source-cursor-read", cursor)[1], 13)
        self.assertEqual(self.run_word("source-cursor-read", cursor)[1], 10)

    def test_identity(self) -> None:
        first = self.new("abc")
        second = self.new("abc")
        self.run_word("source-cursor-read", first)
        self.assertEqual(self.run_word("source-cursor-pos", first), [1])
        self.assertEqual(self.run_word("source-cursor-pos", second), [0])

    def test_set_pos(self) -> None:
        cursor = self.new("abc")
        self.assertEqual(self.run_word("source-cursor-set-pos", cursor, 3), [cursor])
        self.assertEqual(cursor.pos, 3)
        self.run_word("source-cursor-set-pos", cursor, 1)
        for bad in (-1, 4, True, "1", None):
            self.assertCode("source-cursor-invalid-argument", "source-cursor-set-pos", cursor, bad)
            self.assertEqual(cursor.pos, 1, repr(bad))

    def test_slice(self) -> None:
        cursor = self.new("hello")
        cursor.pos = 2
        self.assertEqual(self.run_word("source-cursor-slice", cursor, 1, 4), ["ell"])
        self.assertEqual(self.run_word("source-cursor-slice", cursor, 5, 5), [""])
        self.assertEqual(cursor.pos, 2)
        for start, end in ((3, 2), (-1, 2), (0, 6), (True, 2)):
            self.assertCode("source-cursor-invalid-argument", "source-cursor-slice", cursor, start, end)

    def test_span(self) -> None:
        cursor = self.new()  # "ab\r\ncé"
        origin = self.run_word("source-cursor-span", cursor, 1, 5)[0]
        self.assertIsInstance(origin, SourceOrigin)
        self.assertEqual((origin.path, origin.start, origin.end, origin.start_line, origin.start_column,
                          origin.end_line, origin.end_column), ("rel/p.sl", 1, 5, 1, 2, 2, 2))
        # The position after the CR is already on the next line.
        after_cr = self.run_word("source-cursor-span", cursor, 3, 3)[0]
        self.assertEqual((after_cr.start_line, after_cr.start_column), (2, 1))
        after_lf = self.run_word("source-cursor-span", cursor, 4, 4)[0]
        self.assertEqual((after_lf.start_line, after_lf.start_column), (2, 1))

    def test_lone_cr_line_break(self) -> None:
        cursor = self.new("a\rb")
        origin = self.run_word("source-cursor-span", cursor, 2, 3)[0]
        self.assertEqual((origin.start_line, origin.start_column), (2, 1))

    def test_constructor_validation(self) -> None:
        for args in ((1, "p"), ("t", 1), ("t", None), ("\ud800", "p")):
            self.assertCode("source-cursor-invalid-argument", "source-cursor-new", *args)

    def test_non_cursor_handles(self) -> None:
        for name, args in (("source-cursor-peek", ("x",)), ("source-cursor-read", (1,)),
                           ("source-cursor-eof?", (None,)), ("source-cursor-pos", ([],)),
                           ("source-cursor-set-pos", ("x", 0)), ("source-cursor-slice", ("x", 0, 0)),
                           ("source-cursor-span", ("x", 0, 0))):
            self.assertCode("source-cursor-invalid-argument", name, *args)

    def test_empty_source(self) -> None:
        cursor = self.new("")
        self.assertEqual(self.run_word("source-cursor-eof?", cursor), [1])
        origin = self.run_word("source-cursor-span", cursor, 0, 0)[0]
        self.assertEqual((origin.start_line, origin.start_column, origin.end_line, origin.end_column),
                         (1, 1, 1, 1))


class EndToEndTests(unittest.TestCase):
    def compile_output(self, source: str) -> str:
        with tempfile.TemporaryDirectory(prefix="l2-tokenizer-") as raw:
            path = Path(raw) / "tok.sl"
            path.write_text(textwrap.dedent(source), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact", "--no-daemon"],
                cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_cursor_scanner_and_tokenizer_from_l2(self) -> None:
        out = self.compile_output("""
        import stdlib.sl
        # Count code points with a source cursor, then tokenize with an
        # added separator and print each lexeme with its line.
        word probe
          "x+y\\nz" "<probe>" source-cursor-new
          0 swap
          while dup source-cursor-eof? 0 == do
            source-cursor-read drop swap 1 + swap
          end
          drop puti cr
          ct-tokenizer-get-config dup "separators" map-get drop
          "+" list-append "separators" swap map-set ct-tokenizer-set-config
          "x+y\\nz" "<probe>" ct-tokenizer-run
          dup list-length for
            dup i list-get dup token-lexeme puts token-line puti cr
          end
          drop
        end
        compile-time probe
        word main 0 end
        """)
        self.assertIn("5\nx\n1\n+\n1\ny\n1\nz\n2\n", out)

    def test_backtick_tokens_reach_the_parser(self) -> None:
        out = self.compile_output("""
        import stdlib.sl
        word show
          ct-parser-consume token-lexeme puts
          ct-parser-consume token-lexeme puts
          ct-parser-consume token-lexeme puts
        end
        immediate
        compile-only
        word main show a`\\` 0 end
        """)
        self.assertIn("a\n`\n\\`\n", out)


if __name__ == "__main__":
    unittest.main()
