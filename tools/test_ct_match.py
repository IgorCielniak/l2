#!/usr/bin/env python3
"""Contract tests for `match` over the token stream:
pattern elements and validation, typed constraints, repeated bindings,
first-match order, non-consuming tests, consumption on selection, the
`[_]` default and pass-through, quotation input values, bindings in clause
bodies and quotations, nesting, loop control across clauses, duplicate
warnings, and runtime rejection."""

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

PROBE = "int>string ct-parser-current swap token-with-lexeme list-new swap list-append list>syntax emit"


class MatchTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-match-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str, *, expect_error: bool = False):
        path = self.dir / "m.sl"
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

    def codes(self, compiler, severity: str = "error"):
        return [d.code for d in compiler.parser.diagnostics if d.severity == severity]

    def selects(self, clauses: str, input_text: str, expected: int, *, rest: str = "") -> None:
        """A probe word runs `match` with clauses that each push a number;
        the selected number must equal `expected` and the remaining input
        must compile as `rest`."""
        self.compile(f"""
            import stdlib.sl
            word probe
              match
                {clauses}
              end
              {PROBE}
            end immediate compile-only
            word check probe {input_text} {rest} {expected} == static-assert end
            compile-time check
            word main 0 end
        """)


class PatternTests(MatchTestBase):
    def test_literals_and_first_match_order(self) -> None:
        clauses = "[0, +, $x] => 1  [0] => 2  [_] => 3"
        self.selects(clauses, "0 + 9", 1)
        self.selects(clauses, "0", 2)
        # The default consumes nothing: 5 stays in the input.
        self.selects(clauses, "5", 3, rest="drop")

    def test_literal_is_exact_spelling(self) -> None:
        self.selects("[0] => 1  [_] => 2", "0x0", 2, rest="drop")

    def test_wildcard_and_escapes(self) -> None:
        self.selects("[*, *] => 1  [_] => 2", "a b", 1)
        self.selects("[\\*] => 1  [_] => 2", "*", 1)
        self.selects("[\\*] => 1  [_] => ct-parser-consume drop 2", "x", 2)
        self.selects("[a, \\,, b] => 1  [_] => 2", "a , b", 1)
        self.selects("[\\_] => 1  [_] => 2", "_", 1)
        self.selects("[\\$x] => 1  [_] => 2", "$x", 1)

    def test_typed_constraints(self) -> None:
        cases = [
            ("ident", "foo", True), ("ident", "1x", False), ("int", "-12", True), ("int", "0x1f", True),
            ("int", "1.5", False), ("float", "1.5", True), ("float", "2", False), ("number", "2", True),
            ("number", "2.5", True), ("string", '"s"', True), ("string", "s", False), ("char", "'c'", True),
            ("literal", '"s"', True), ("literal", "true", False), ("any", "anything", True),
        ]
        for constraint, text, matches in cases:
            with self.subTest(constraint=constraint, text=text):
                expected = 1 if matches else 2
                self.selects(f"[$v:{constraint}] => 1  [_] => ct-parser-consume drop 2", text, expected)

    def test_repeated_binding_requires_equal_values(self) -> None:
        self.selects("[$a, $a] => 1  [_] => 2", "x x", 1)
        self.selects("[$a, $a] => 1  [_] => ct-parser-consume drop ct-parser-consume drop 2", "x y", 2)

    def test_insufficient_input_fails_the_clause(self) -> None:
        self.compile("""
            import stdlib.sl
            word probe match [a, b, c] => 1 [_] => 2 end drop end immediate compile-only
            word main 0 end
            probe
        """)

    def test_no_clause_errors_without_advancing(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word probe match [a] => end end immediate compile-only
            word main probe zzz 0 end
        """, expect_error=True)
        self.assertIn("match-no-clause", self.codes(compiler))


class ValidationTests(MatchTestBase):
    def invalid(self, clauses: str) -> None:
        compiler, _ = self.compile(f"""
            import stdlib.sl
            word probe match {clauses} end end compile-only
            word main 0 end
        """, expect_error=True)
        self.assertIn("match-pattern-invalid", self.codes(compiler), clauses)

    def test_invalid_patterns(self) -> None:
        for clauses in ("[] => 1", "[a,] => 1", "[a,,b] => 1", "[a b] => 1", "[\\] => 1",
                        "[$*rest] => 1", "[!x] => 1", "[foo?] => 1", "[$x+] => 1", "[$x:intt] => 1",
                        "[$x:Int] => 1", "[$if] => 1", "[$end] => 1", "[$1x] => 1",
                        "[_] => 1 [a] => 2", "[a] => 1 [_] => _"):
            with self.subTest(clauses=clauses):
                self.invalid(clauses)

    def test_validated_even_when_not_executed(self) -> None:
        self.invalid("[a] => 1 0 if match [foo?] => 2 end end")

    def test_match_needs_a_clause(self) -> None:
        self.invalid("1 drop")

    def test_duplicate_pattern_warning_ignores_binding_names(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word probe
              match
                [$a, x] => 1 drop
                [$b, x] => 2 drop
                [$a:int, x] => 3 drop
                [_] =>
              end
            end compile-only
            word main 0 end
        """)
        self.assertEqual(self.codes(compiler, "warning").count("match-duplicate-pattern"), 1)


class InputAndBindingTests(MatchTestBase):
    def test_quotation_is_one_input_value(self) -> None:
        self.selects("[$q:syntax] => 1  [_] => 2", "`1 2 +`", 1)
        self.selects("[$q:syntax] => 1  [_] => ct-parser-consume drop 2", "x", 2)
        self.selects("[$q:ident] => 1  [$q] => 2", "`x`", 2)
        self.selects("[$a, $a] => 1  [_] => 2", "`p q` `p q`", 1)

    def test_bindings_are_values_in_clause_bodies(self) -> None:
        self.compile("""
            import stdlib.sl
            word probe
              match
                [$x, $y:syntax] =>
                  $x token-lexeme "a" string-equal? static-assert
                  $x token-lexeme "a" string-equal? static-assert
                  $y syntax-length 2 == static-assert
                  `$x $y` emit
              end
            end immediate compile-only
            word a 40 end
            word check probe a `2 +` 42 == static-assert end
            compile-time check
            word main 0 end
        """)

    def test_unterminated_quotation_in_input(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word probe match [$q] => end end immediate compile-only
            word main probe `1 2
        """, expect_error=True)
        self.assertIn("syntax-unterminated-quotation", self.codes(compiler))

    def test_nested_match_and_shadowing(self) -> None:
        self.compile("""
            import stdlib.sl
            word probe
              match
                [$x, then] =>
                  match
                    [$x] => $x token-lexeme "inner" string-equal? static-assert
                  end
                  $x token-lexeme "outer" string-equal? static-assert
              end
            end immediate compile-only
            word check probe outer then inner end
            compile-time check
            word main 0 end
        """)

    def test_helpers_do_not_see_caller_bindings(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word helper `$x` drop end compile-only
            word probe match [$x] => helper end end immediate compile-only
            word main probe a 0 end
        """, expect_error=True)
        self.assertIn("syntax-binding-unbound", self.codes(compiler))

    def test_break_out_of_a_clause_restores_bindings(self) -> None:
        self.compile("""
            import stdlib.sl
            word probe
              0 begin
                match
                  [.] => 1 +
                  [$stop] => break
                end
              again
              """ + PROBE + """
            end immediate compile-only
            word check probe . . . ; 3 == static-assert end
            compile-time check
            word main 0 end
        """)


class RuntimeTests(MatchTestBase):
    def test_match_in_runtime_code_is_rejected(self) -> None:
        compiler, error = self.compile("""
            import stdlib.sl
            word main match [_] => end 0 end
        """, expect_error=True)
        self.assertEqual(error.diagnostic.code, "match-runtime")

    def test_match_without_input_stream(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word probe match [_] => end end compile-only
            word main 0 end
        """)
        word = compiler.dictionary.lookup("probe")
        with self.assertRaises(ParseError) as caught:
            compiler.parser.compile_time_vm.invoke(word)
        self.assertEqual(caught.exception.code, "emit-no-input")


if __name__ == "__main__":
    unittest.main()
