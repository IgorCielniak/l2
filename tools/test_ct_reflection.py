#!/usr/bin/env python3
"""Contract tests for static-assert, the CT loop index
`i`, debug-only `repr`, selected-definition source reflection (CT lookup and
the embedded catalog), string-utf8-length, and libs/fn.sl's
fn-infix>postfix (formerly the `shunt` built-in).  Runtime catalog lookups
are exercised end to end by tests/ct/source_retrieval*.sl."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, ParseError


class ReflectionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-reflection-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str, *, expect_error: bool = False):
        path = self.dir / "r.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib", ROOT])
        error = None
        emission = None
        try:
            emission = compiler.compile_file(path)
        except (ParseError, CompileError) as exc:
            error = exc
        if expect_error:
            self.assertIsNotNone(error)
        else:
            self.assertIsNone(error, error)
        return compiler, emission

    def ct(self, body: str, *, before: str = "", expect_error: bool = False):
        return self.compile(f"""
            import stdlib.sl
            {before}
            word check
            {body}
            end
            compile-time check
            word main 0 end
        """, expect_error=expect_error)

    def diag(self, compiler, code):
        return next(d for d in compiler.parser.diagnostics if d.code == code)

    def call(self, name: str, *args):
        compiler = l2_main.Compiler()
        vm = compiler.parser.compile_time_vm
        vm.stack = list(args)
        compiler.dictionary.lookup(name).compile_time_intrinsic(vm)
        return list(vm.stack)


class StaticAssertTests(ReflectionTestBase):
    def test_passing_conditions_leave_the_stack(self) -> None:
        self.assertEqual(self.call("static-assert", 7, True), [7])
        self.assertEqual(self.call("static-assert", 7, -1), [7])

    def test_failure_is_a_diagnostic_at_the_call_site(self) -> None:
        compiler, _ = self.ct("1 2 + 4 == static-assert", expect_error=True)
        diag = self.diag(compiler, "static-assertion-failed")
        self.assertEqual(diag.category, "ct_execution_error")
        self.assertEqual(diag.primary.start_line, 4)
        for value in (False, 0):
            with self.subTest(value=value):
                with self.assertRaises(ParseError) as caught:
                    self.call("static-assert", value)
                self.assertEqual(caught.exception.code, "static-assertion-failed")

    def test_other_types_are_type_errors(self) -> None:
        for value in ("1", None, 1.0, [1], {}):
            with self.subTest(value=value):
                with self.assertRaises(ParseError) as caught:
                    self.call("static-assert", value)
                self.assertEqual(caught.exception.code, "ct-argument-type")


class LoopIndexTests(ReflectionTestBase):
    def test_index_and_nesting(self) -> None:
        self.ct("""
            list-new 3 for i list-append end
            dup 0 list-get 0 == static-assert
            2 list-get 2 == static-assert
            0 2 for 3 for i + end end 6 == static-assert
        """)

    def test_outside_a_loop(self) -> None:
        compiler, _ = self.ct("i drop", expect_error=True)
        diag = self.diag(compiler, "ct-loop-index-unavailable")
        self.assertEqual(diag.category, "ct_execution_error")


class ReprTests(ReflectionTestBase):
    def test_produces_text_for_any_value(self) -> None:
        compiler = l2_main.Compiler()
        token = l2_main._derive_token(None, "tok")
        for value in (None, 1, 2.5, "s", [1, [2, {"k": "v"}]], {"a": [None]}, token,
                      l2_main.RewriteHandle(1), l2_main.Syntax([token])):
            with self.subTest(value=value):
                vm = compiler.parser.compile_time_vm
                vm.stack = [value]
                compiler.dictionary.lookup("repr").compile_time_intrinsic(vm)
                self.assertEqual(len(vm.stack), 1)
                self.assertIsInstance(vm.stack[0], str)
                self.assertTrue(vm.stack[0])


class SourceTests(ReflectionTestBase):
    def test_ct_lookup_follows_the_selected_candidate(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            import stdlib/meta.sl
            word pick 1 end
            priority 3 word pick 3 end
            priority 3 word pick 33 end
            priority 1 word pick 2 end
            word check
              "pick" meta-runtime-get-source
              over "33" string-contains? static-assert
              swap string-utf8-length == static-assert
              "missing" meta-runtime-get-source 0 == static-assert "" string-equal? static-assert
            end
            compile-time check
            word main 0 end
        """)

    def test_source_less_selected_candidate_does_not_fall_back(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word base 1 end
            word main 0 end
        """)
        original = compiler.dictionary.lookup("base")
        self.assertTrue(original.source_text)
        winner = l2_main.Word(name="base", priority=9)
        winner.definition = original.definition
        compiler.dictionary.register(winner)
        self.assertIs(compiler.dictionary.lookup("base"), winner)
        vm = compiler.parser.compile_time_vm
        vm.stack = ["base"]
        compiler.dictionary.lookup("__meta-runtime-source").compile_time_intrinsic(vm)
        self.assertEqual(vm.stack, ["", 0])
        vm.stack = ["base"]
        compiler.dictionary.lookup("ct-definition-source-by-name").compile_time_intrinsic(vm)
        self.assertEqual(vm.stack, [None, 0])

    def test_catalog_lists_only_selected_definitions(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word dual 1 end
            priority 2 word dual 2 end
            word main dual drop 0 end
        """)
        catalog = dict(compiler.assembler.source_catalog)
        self.assertEqual(catalog["dual"], "word dual 2 end")
        self.assertEqual(list(catalog), sorted(catalog))
        asm = l2_main._render_source_catalog_asm([("dual", "word dual 2 end"), ("é", "x")])
        self.assertIn("l2_source_catalog:\n    dq 2\n", asm)
        self.assertIn(", 2, l2_srccat_src_1, 1", asm)          # 'é' is two UTF-8 bytes

    def test_string_utf8_length(self) -> None:
        self.assertEqual(self.call("string-utf8-length", "aé€😀"), [1 + 2 + 3 + 4])
        with self.assertRaises(ParseError):
            self.call("string-utf8-length", 5)


class InfixTests(ReflectionTestBase):
    def convert(self, *tokens: str) -> str:
        items = " ".join(f'"{token}" list-append' for token in tokens)
        compiler, _ = self.compile(f"""
            import stdlib.sl
            import libs/fn.sl
            word check list-new {items} fn-infix>postfix " " string-join ct-diagnostic-note end
            compile-time check
            word main 0 end
        """)
        notes = [d.message for d in compiler.parser.diagnostics if d.severity == "note"]
        return notes[-1]

    def test_precedence_associativity_and_parentheses(self) -> None:
        self.assertEqual(self.convert("a", "+", "b", "*", "c"), "a b c * +")
        self.assertEqual(self.convert("(", "a", "+", "b", ")", "*", "c"), "a b + c *")
        self.assertEqual(self.convert("a", "-", "b", "-", "c"), "a b - c -")
        self.assertEqual(self.convert("a", "/", "b", "%", "c", "+", "d"), "a b / c % d +")

    def test_no_grammar_validation(self) -> None:
        self.assertEqual(self.convert("+", "+", "a"), "+ a +")

    def test_empty_and_errors(self) -> None:
        self.ct("list-new fn-infix>postfix list-length 0 == static-assert", before="import libs/fn.sl")
        for items, reason in (('"(" list-append "a" list-append', "unmatched '('"),
                              ('"a" list-append ")" list-append', "unmatched ')'"),
                              ('"a" list-append 3 list-append', "")):
            with self.subTest(items=items):
                compiler, _ = self.ct(f"list-new {items} fn-infix>postfix drop", before="import libs/fn.sl",
                                      expect_error=True)
                messages = " ".join(d.message for d in compiler.parser.diagnostics if d.severity == "error")
                self.assertIn(reason, messages)

    def test_shunt_is_not_a_built_in(self) -> None:
        dictionary = l2_main.Compiler().dictionary
        for name in ("shunt", "ct-runtime-get-source", "ct-repr", "static_assert"):
            with self.subTest(name=name):
                self.assertIsNone(dictionary.lookup(name))


if __name__ == "__main__":
    unittest.main()
