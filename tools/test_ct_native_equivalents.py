#!/usr/bin/env python3
"""The CT VM computes standard-library integer :asm words (+ - * / % ==
!= < > <= >= and or not) in Python instead of marshalling the stack into
native code.  These tests check that each equivalent is selected for the
stdlib body, agrees with the native assembly on edge cases, keeps
non-integer operands on the native path, and that strings on the CT stack
survive native calls (the old unmarshalling deleted the value below one)."""

from __future__ import annotations

import random
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, ParseError

WORDS = ["+", "-", "*", "/", "%", "==", "!=", "<", ">", "<=", ">=", "and", "or", "not",
         "neg", "abs", "bitnot", "band", "bor", "bxor", "bnot", "shl", "sal", "shr", "sar", "rol", "ror",
         "inc", "dec", "min", "max", "clamp"]
UNARY = {"not", "neg", "abs", "bitnot", "bnot", "inc", "dec"}
TERNARY = {"clamp"}
EDGES = [0, 1, -1, 2, -2, 7, -7, 2**31, -2**31, 2**62, 2**63 - 1, -2**63, 2**63, 2**64 + 5]


def core_body(name: str) -> str:
    source = (ROOT / "stdlib" / "core.sl").read_text(encoding="utf-8")
    match = re.search(r"^:asm " + re.escape(name) + r" \{\n(.*?)^\}", source, re.M | re.S)
    assert match is not None, name
    return match.group(1)


class CompiledStdlib:
    compiler = None

    @classmethod
    def get(cls):
        if cls.compiler is None:
            tmp = Path(tempfile.mkdtemp(prefix="l2-native-"))
            path = tmp / "n.sl"
            path.write_text("import stdlib.sl\nword main 0 end\n", encoding="utf-8")
            cls.compiler = l2_main.Compiler(include_paths=[ROOT, ROOT / "stdlib"])
            cls.compiler.compile_file(path)
        return cls.compiler


def run_word(name: str, args, *, native: bool):
    compiler = CompiledStdlib.get()
    vm = compiler.parser.compile_time_vm
    word = compiler.dictionary.lookup(name)
    saved = dict(l2_main._CT_NATIVE_BY_BODY)
    if native:
        l2_main._CT_NATIVE_BY_BODY[word.definition.body] = None
    try:
        vm.stack = list(args)
        vm.return_stack = []
        vm._run_asm_definition(word)
        return list(vm.stack)
    finally:
        l2_main._CT_NATIVE_BY_BODY.clear()
        l2_main._CT_NATIVE_BY_BODY.update(saved)


class SelectionTests(unittest.TestCase):
    def test_every_stdlib_body_has_an_equivalent(self) -> None:
        for name in WORDS:
            with self.subTest(name=name):
                self.assertIsNotNone(l2_main._ct_native_equivalent(core_body(name)))

    def test_other_bodies_run_natively(self) -> None:
        body = core_body("+").replace("add qword", "sub qword")
        self.assertIs(l2_main._ct_native_equivalent(body), l2_main._ct_native_equivalent(core_body("-")))
        self.assertIsNone(l2_main._ct_native_equivalent(body + "\n    nop\n"))


class AgreementTests(unittest.TestCase):
    def test_equivalents_match_native_assembly(self) -> None:
        rng = random.Random(7)
        pairs = [(a, b) for a in EDGES for b in EDGES]
        pairs += [(rng.randint(-2**64, 2**64), rng.randint(-2**64, 2**64)) for _ in range(150)]
        for name in WORDS:
            arity = 1 if name in UNARY else 3 if name in TERNARY else 2
            for a, b in pairs:
                if arity == 3:
                    args = [99, a, b, rng.randint(-2**63, 2**63 - 1)]
                else:
                    args = [99, a] if arity == 1 else [99, a, b]
                if name in ("/", "%"):
                    wb = l2_main._wrap_i64(b)
                    if wb == 0 or (l2_main._wrap_i64(a) == -2**63 and wb == -1):
                        continue  # native: SIGFPE; equivalent: a diagnostic (below)
                with self.subTest(word=name, args=args):
                    self.assertEqual(run_word(name, args, native=False), run_word(name, args, native=True))

    def test_booleans_act_as_integers(self) -> None:
        self.assertEqual(run_word("+", [True, 2], native=False), [3])
        self.assertEqual(run_word("and", [True, False], native=False), [0])

    def test_division_by_zero_is_a_diagnostic(self) -> None:
        for name, args in (("/", [1, 0]), ("%", [1, 0]), ("/", [-2**63, -1])):
            with self.subTest(word=name, args=args):
                with self.assertRaises(ParseError) as caught:
                    run_word(name, args, native=False)
                self.assertEqual(caught.exception.code, "ct-argument-invalid")


class StackPreservationTests(unittest.TestCase):
    def ct(self, body: str):
        with tempfile.TemporaryDirectory(prefix="l2-native-") as tmp:
            path = Path(tmp) / "s.sl"
            path.write_text(textwrap.dedent(f"""
                import stdlib.sl
                word check {body} end
                compile-time check
                word main 0 end
            """).lstrip("\n"), encoding="utf-8")
            l2_main.Compiler(include_paths=[ROOT, ROOT / "stdlib"]).compile_file(path)

    def test_strings_below_integer_words(self) -> None:
        self.ct('5 "ab" 1 2 == drop drop 5 == static-assert')
        self.ct('5 "ab" dup string-length 2 == static-assert drop 5 == static-assert')

    def test_strings_below_a_native_word(self) -> None:
        # Not an integer equivalent: the value still crosses the native path.
        for name in ("+", "=="):
            with self.subTest(word=name):
                saved = dict(l2_main._CT_NATIVE_BY_BODY)
                body = CompiledStdlib.get().dictionary.lookup(name).definition.body
                l2_main._CT_NATIVE_BY_BODY[body] = None
                calls = []
                original = l2_main.CompileTimeVM._run_asm_definition
                try:
                    def spy(vm, word, _original=original):
                        calls.append(word.name)
                        return _original(vm, word)
                    l2_main.CompileTimeVM._run_asm_definition = spy
                    self.ct(f'5 "ab" 1 2 {name} drop drop 5 == static-assert')
                    self.assertIn(name, calls)
                finally:
                    l2_main.CompileTimeVM._run_asm_definition = original
                    l2_main._CT_NATIVE_BY_BODY.clear()
                    l2_main._CT_NATIVE_BY_BODY.update(saved)

    def test_string_mode_keeps_strings_below(self) -> None:
        self.ct('7 "keep" "" puts "keep" string-equal? static-assert 7 == static-assert')


class StringCellTests(unittest.TestCase):
    """A string operand is its runtime (address, length) cells."""

    def ct(self, body: str):
        return StackPreservationTests.ct(self, body)

    def test_string_length_cell_takes_part(self) -> None:
        self.ct('"abc" 1 + 4 == static-assert drop')
        self.ct('"abcd" 2 - 2 == static-assert drop')
        self.ct('"abc" 3 == static-assert drop')
        self.ct('"abc" 3 > 0 == static-assert drop')

    def test_string_below_an_integer_operand(self) -> None:
        # 5 "xy" + -> 5 (address + 2), as at runtime.
        self.ct('5 "xy" + drop 5 == static-assert')

    def test_other_values_are_type_errors(self) -> None:
        for body in ('list-new 1 + drop', 'nil 1 - drop', '1 list-new < drop'):
            with self.subTest(body=body):
                with self.assertRaises((l2_main.ParseError, l2_main.CompileTimeError)) as ctx:
                    self.ct(body)
                self.assertIn("ct-argument-type", str(ctx.exception.diagnostic.code if ctx.exception.diagnostic else ""))


if __name__ == "__main__":
    unittest.main()
