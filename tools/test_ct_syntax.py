#!/usr/bin/env python3
"""Contract tests for quotations and Syntax values:
backtick quotations, escapes, splices, unterminated quotations, the Syntax
API, `emit` outside macros, runtime-use errors, `gensym`, and the
expansion-depth limit words."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, ParseError, Syntax, Token


class SyntaxTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-syntax-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str, *, expect_error: bool = False):
        path = self.dir / "s.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"])
        error = None
        try:
            compiler.compile_file(path)
        except (ParseError, CompileError) as exc:
            error = exc
        if expect_error:
            self.assertIsNotNone(error)
        else:
            self.assertIsNone(error, error)
        return compiler, error

    def error_codes(self, compiler):
        return [d.code for d in compiler.parser.diagnostics if d.severity == "error"]

    def run_word(self, compiler, name: str, *args):
        word = compiler.dictionary.lookup(name)
        vm = compiler.parser.compile_time_vm
        vm.stack = list(args)
        word.compile_time_intrinsic(vm)
        return list(vm.stack)

    def quote(self, text: str) -> Syntax:
        """Evaluate one quotation written in a compile-time word."""
        compiler, _ = self.compile(f"""
            import stdlib.sl
            word q {text} end
            word main 0 end
        """)
        word = compiler.dictionary.lookup("q")
        vm = compiler.parser.compile_time_vm
        vm.stack = []
        vm.invoke(word)
        (value,) = vm.stack
        return value


class QuotationTests(SyntaxTestBase):
    def test_quotation_is_an_immutable_token_sequence(self) -> None:
        value = self.quote("`1 2 +`")
        self.assertIsInstance(value, Syntax)
        self.assertEqual(value.lexemes(), ("1", "2", "+"))
        with self.assertRaises(AttributeError):
            value.tokens = ()
        self.assertEqual(self.quote("``").lexemes(), ())

    def test_literal_tokens_keep_their_source_origin(self) -> None:
        value = self.quote("`foo bar`")
        origin = value.tokens[0].origin
        self.assertEqual(Path(origin.path).name, "s.sl")
        self.assertEqual((origin.start_line, origin.start_column), (2, 9))

    def test_ordinary_tokenization_strings_and_comments(self) -> None:
        value = self.quote('`"a b" x # comment\n y`')
        self.assertEqual(value.lexemes(), ('"a b"', "x", "y"))

    def test_escapes(self) -> None:
        value = self.quote("`\\$name \\` \\\\ `")
        self.assertEqual(value.lexemes(), ("$name", "`", "\\"))

    def test_invalid_escape(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word q `\\n` end
            word main 0 end
        """, expect_error=True)
        self.assertIn("syntax-escape-invalid", self.error_codes(compiler))

    def test_unterminated_quotation(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word q `1 2
        """, expect_error=True)
        diag = next(d for d in compiler.parser.diagnostics if d.code == "syntax-unterminated-quotation")
        self.assertEqual((diag.category, diag.phase), ("lexer_error", "quotation"))
        self.assertEqual((diag.primary.start_line, diag.primary.start_column), (2, 8))
        self.assertEqual(diag.primary.end_line, 2)

    def test_unbound_splice_is_an_error_when_used(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word q `a $missing b` drop end
            compile-time q
            word main 0 end
        """, expect_error=True)
        self.assertIn("syntax-binding-unbound", self.error_codes(compiler))

    def test_bound_splices_insert_tokens_and_syntax(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word main 0 end
        """)
        parser = compiler.parser
        tokens = parser.reader.tokenize_text("` a $x $y b `", "<t>")
        parser.tokens, parser.pos = tokens, 1
        parser._binding_scopes.append({"x", "y"})
        template = parser._quotation_template(0, len(tokens) - 1)
        parser._binding_scopes.pop()
        vm = parser.compile_time_vm
        one = Token("1", 1, 0, 0, 1)
        vm.binding_env = {"x": one, "y": Syntax([Token("2", 1, 0, 0, 1), Token("+", 1, 0, 0, 1)])}
        try:
            value = vm._evaluate_quotation(template)
        finally:
            vm.binding_env = None
        self.assertEqual(value.lexemes(), ("a", "1", "2", "+", "b"))
        self.assertIs(value.tokens[1], one)
        vm.binding_env = {"x": 5, "y": Syntax([])}
        try:
            with self.assertRaises(ParseError) as caught:
                vm._evaluate_quotation(template)
        finally:
            vm.binding_env = None
        self.assertEqual(caught.exception.code, "syntax-splice-invalid")

    def test_quotation_in_runtime_code_is_rejected(self) -> None:
        compiler, error = self.compile("""
            import stdlib.sl
            word main `1` drop 0 end
        """, expect_error=True)
        self.assertEqual(error.diagnostic.code, "syntax-quotation-runtime")
        self.assertEqual(error.diagnostic.category, "emission_error")

    def test_compile_only_words_may_hold_quotations(self) -> None:
        self.compile("""
            import stdlib.sl
            word helper `1` drop end compile-only
            word main 0 end
        """)


class SyntaxApiTests(SyntaxTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.compiler, _ = self.compile("word main 0 end\n")
        self.tok = [Token(lex, 1, i, i, i + 1) for i, lex in enumerate(["a", "b", "c"])]
        self.syn = Syntax(self.tok)

    def code(self, name: str, *args) -> str:
        with self.assertRaises(ParseError) as caught:
            self.run_word(self.compiler, name, *args)
        return caught.exception.code

    def test_length_get_and_negative_index(self) -> None:
        self.assertEqual(self.run_word(self.compiler, "syntax-length", self.syn), [3])
        self.assertIs(self.run_word(self.compiler, "syntax-get", self.syn, -1)[0], self.tok[2])
        self.assertEqual(self.code("syntax-get", self.syn, 3), "ct-index-out-of-range")
        self.assertEqual(self.code("syntax-get", self.syn, -4), "ct-index-out-of-range")

    def test_slice_and_concat(self) -> None:
        (part,) = self.run_word(self.compiler, "syntax-slice", self.syn, 1, 99)
        self.assertEqual(part.lexemes(), ("b", "c"))
        (joined,) = self.run_word(self.compiler, "syntax-concat", part, self.syn)
        self.assertEqual(joined.lexemes(), ("b", "c", "a", "b", "c"))
        self.assertEqual(self.syn.lexemes(), ("a", "b", "c"))

    def test_list_round_trip_keeps_tokens(self) -> None:
        (items,) = self.run_word(self.compiler, "syntax>list", self.syn)
        self.assertEqual(items, self.tok)
        items.clear()
        self.assertEqual(len(self.syn.tokens), 3)
        (again,) = self.run_word(self.compiler, "list>syntax", list(self.tok))
        self.assertTrue(all(a is b for a, b in zip(again.tokens, self.tok)))
        self.assertEqual(self.code("list>syntax", [self.tok[0], "x"]), "ct-argument-type")

    def test_type_errors(self) -> None:
        self.assertEqual(self.code("syntax-length", [self.tok[0]]), "ct-argument-type")
        self.assertEqual(self.code("emit", [self.tok[0]]), "ct-argument-type")
        self.assertEqual(self.code("emit", self.tok[0]), "ct-argument-type")

    def test_equality_compares_lexemes(self) -> None:
        other = Syntax([Token("a", 9, 9, 9, 9), Token("b", 9, 9, 9, 9), Token("c", 9, 9, 9, 9)])
        self.assertTrue(l2_main._ct_values_equal(self.syn, other))
        self.assertFalse(l2_main._ct_values_equal(self.syn, Syntax(self.tok[:2])))
        self.assertEqual(l2_main._ct_type_name(self.syn), "Syntax")

    def test_json_projection(self) -> None:
        projected = l2_main._event_json_value(self.syn, set())
        self.assertEqual(projected["kind"], "Syntax")
        self.assertEqual([t["lexeme"] for t in projected["tokens"]], ["a", "b", "c"])


class EmitTests(SyntaxTestBase):
    def test_emitted_definition_is_processed_in_source_order(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word define_answer `word generated_answer 42 end` emit end
            compile-time define_answer
            word check generated_answer 42 == static-assert end
            compile-time check
            word main generated_answer drop 0 end
        """)

    def test_emit_inserts_at_the_current_position(self) -> None:
        self.compile("""
            import stdlib.sl
            word push_two `2` emit end immediate compile-only
            word check 40 push_two + 42 == static-assert end
            compile-time check
            word main 0 end
        """)

    def test_emit_without_an_input_stream(self) -> None:
        compiler, _ = self.compile("word main 0 end\n")
        with self.assertRaises(ParseError) as caught:
            self.run_word(compiler, "emit", Syntax([]))
        self.assertEqual(caught.exception.code, "emit-no-input")


class GensymAndLimitTests(SyntaxTestBase):
    def test_gensym(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word lambda_1 0 end
            word main 0 end
        """)
        first = self.run_word(compiler, "gensym", "lambda")[0]
        self.assertEqual(first, "lambda_2")  # lambda_1 is a known name
        self.assertEqual(self.run_word(compiler, "gensym", "a-b!")[0], "a_b_3")
        self.assertEqual(self.run_word(compiler, "gensym", "__")[0], "g_4")
        self.assertEqual(self.run_word(compiler, "gensym", "9x")[0], "g_9x_5")
        with self.assertRaises(ParseError):
            self.run_word(compiler, "gensym", 5)
        self.assertIsNone(compiler.dictionary.lookup("ct-gensym"))

    def test_expansion_depth_limit(self) -> None:
        compiler, _ = self.compile("word main 0 end\n")
        self.assertEqual(self.run_word(compiler, "ct-expansion-get-depth-limit"), [256])
        self.assertEqual(self.run_word(compiler, "ct-expansion-set-depth-limit", 12), [])
        self.assertEqual(self.run_word(compiler, "ct-expansion-get-depth-limit"), [12])
        for bad in (0, -3, True, "4"):
            with self.assertRaises(ParseError) as caught:
                self.run_word(compiler, "ct-expansion-set-depth-limit", bad)
            self.assertEqual(caught.exception.code, "ct-argument-invalid")
        for name in ("ct-get-macro-expansion-limit", "ct-set-macro-expansion-limit"):
            self.assertIsNone(compiler.dictionary.lookup(name))


if __name__ == "__main__":
    unittest.main()
