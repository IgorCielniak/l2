#!/usr/bin/env python3
"""Contract tests for assembly output buffers: the text
preamble's placement and entry-point neutrality, CT .data fragments next to
compiler data, the runtime BSS buffer (append/set/clear over the standard
layout, compiler BSS kept), atomic set validation, the fragment boundary
rule, and retired words."""

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
from l2_main import CompileError, ParseError, _AsmFragment, _join_asm_lines


class OutputTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-output-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def asm(self, setup: str, body: str = '"ok" puts 0', extra: str = "") -> str:
        """Compile a program whose `setup` CT word edits the buffers; return
        the assembly text."""
        path = self.dir / "o.sl"
        path.write_text(textwrap.dedent(f"""
            import stdlib.sl
            word setup
            {setup}
            end
            compile-time setup
            {extra}
            word main {body} end
        """).lstrip("\n"), encoding="utf-8")
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"])
        return compiler.compile_file(path).snapshot()

    def run_program(self, setup: str, body: str, extra: str = "") -> str:
        path = self.dir / "p.sl"
        path.write_text(textwrap.dedent(f"""
            import stdlib.sl
            word setup
            {setup}
            end
            compile-time setup
            {extra}
            word main {body} end
        """).lstrip("\n"), encoding="utf-8")
        out = self.dir / "p"
        proc = subprocess.run([sys.executable, str(ROOT / "main.py"), str(path), "-o", str(out), "--no-daemon",
                               "--no-cache"], cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return subprocess.run([str(out)], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout

    @staticmethod
    def section(asm: str, name: str) -> str:
        """The emitter's main section of that name (the last one; the
        runtime prelude opens a small .data section of its own)."""
        marker = f"section .{name}\n"
        rest = asm[asm.rindex(marker) + len(marker):]
        end = rest.find("\nsection .")
        return rest if end < 0 else rest[:end]


class TextPreambleTests(OutputTestBase):
    def test_placement_after_runtime_prelude_before_definitions(self) -> None:
        asm = self.asm('"; PRE-1" ct-output-append-text-preamble "; PRE-2" ct-output-append-text-preamble')
        prelude_end = asm.index("sys_argv: dq 0\nsection .text\n")
        first = asm.index("; PRE-1\n; PRE-2\n")
        self.assertLess(prelude_end, first)
        self.assertLess(first, asm.index("w_main:"))

    def test_does_not_affect_default_startup(self) -> None:
        asm = self.asm('"; mentions _start: but owns nothing" ct-output-append-text-preamble')
        self.assertIn("; __ORIGIN__ default_stub\nglobal _start\n_start:", asm)

    def test_explicit_start_definition_still_suppresses_the_stub(self) -> None:
        asm = self.asm("", extra=":asm _start { mov rax, 60\n xor rdi, rdi\n syscall } ;")
        self.assertNotIn("default_stub", asm)

    def test_set_and_clear(self) -> None:
        asm = self.asm('"; GONE" ct-output-append-text-preamble '
                       'list-new "; A" list-append "; B" list-append ct-output-set-text-preamble')
        self.assertIn("; A\n; B\n", asm)
        self.assertNotIn("; GONE", asm)
        asm = self.asm('"; GONE" ct-output-append-text-preamble ct-output-clear-text-preamble')
        self.assertNotIn("; GONE", asm)


class DataTests(OutputTestBase):
    def test_fragments_keep_order_between_definition_data_and_variables(self) -> None:
        asm = self.asm('"ct_a: dq 1" ct-output-append-data "ct_b: dq 2" ct-output-append-data',
                       extra="word keep with counter in counter end end")
        data = self.section(asm, "data")
        self.assertLess(data.index("str_0:"), data.index("ct_a: dq 1\nct_b: dq 2"))
        self.assertLess(data.index("ct_b: dq 2"), data.index("var_counter"))

    def test_set_and_clear_keep_compiler_data(self) -> None:
        for edit in ('list-new "ct_c: dq 3" list-append ct-output-set-data',
                     "ct-output-clear-data"):
            with self.subTest(edit=edit):
                asm = self.asm(f'"ct_a: dq 1" ct-output-append-data {edit}', extra="word keep with counter in counter end end")
                data = self.section(asm, "data")
                self.assertNotIn("ct_a:", data)
                self.assertIn("str_0:", data)
                self.assertIn("var_counter", data)
                self.assertEqual("ct_c: dq 3" in data, "set" in edit)


class BssTests(OutputTestBase):
    BSS_LIST_BODY = "{ 1 2 } drop 0"

    def test_append_keeps_the_standard_layout(self) -> None:
        bss = self.section(self.asm('"ct_extra: resq 4" ct-output-append-bss', body=self.BSS_LIST_BODY), "bss")
        self.assertIn("dstack: resb DSTK_BYTES", bss)
        self.assertIn("list_capture_stack: resq 1024\nct_extra: resq 4", bss)
        self.assertLess(bss.index("ct_extra"), bss.index("bss_list_0"))

    def test_set_replaces_and_clear_empties_but_compiler_bss_stays(self) -> None:
        bss = self.section(self.asm('list-new "ct_only: resq 1" list-append ct-output-set-bss',
                                    body=self.BSS_LIST_BODY), "bss")
        self.assertNotIn("dstack:", bss)
        self.assertTrue(bss.startswith("ct_only: resq 1"))
        self.assertIn("bss_list_0", bss)
        bss = self.section(self.asm('ct-output-clear-bss "after: resq 2" ct-output-append-bss',
                                    body=self.BSS_LIST_BODY), "bss")
        self.assertNotIn("dstack:", bss)
        self.assertTrue(bss.startswith("after: resq 2"))
        self.assertIn("bss_list_0", bss)

    def test_full_replacement_program_runs(self) -> None:
        layout = ["align 16", "dstack: resb DSTK_BYTES", "dstack_top:", "align 16", "rstack: resb RSTK_BYTES",
                  "rstack_top:", "align 16", "print_buf: resb PRINT_BUF_BYTES", "print_buf_end:", "align 16",
                  "persistent: resb 64", "align 16", "list_capture_sp: resq 1", "list_capture_tmp: resq 1",
                  "list_capture_stack: resq 1024", "mine: resq 1"]
        setup = "list-new " + " ".join(f'"{line}" list-append' for line in layout) + " ct-output-set-bss"
        self.assertEqual(self.run_program(setup, '"ran" puts 0'), "ran\n")


class ValidationTests(OutputTestBase):
    def test_invalid_input_changes_nothing(self) -> None:
        for word, value in (("ct-output-set-text-preamble", ["; a", 1]), ("ct-output-set-data", ["x: dq 1", None]),
                            ("ct-output-set-bss", ["y: resq 1", ["nested"]]), ("ct-output-set-bss", "not a list"),
                            ("ct-output-append-data", 5)):
            with self.subTest(word=word, value=value):
                compiler = l2_main.Compiler()
                parser = compiler.parser
                parser.output_text_preamble = [_AsmFragment("; keep")]
                parser.output_data = [_AsmFragment("keep: dq 0")]
                parser.output_bss = None
                vm = parser.compile_time_vm
                vm.stack = [value]
                with self.assertRaises(ParseError) as caught:
                    compiler.dictionary.lookup(word).compile_time_intrinsic(vm)
                self.assertEqual(caught.exception.code, "ct-argument-type")
                self.assertEqual(parser.output_text_preamble, ["; keep"])
                self.assertEqual(parser.output_data, ["keep: dq 0"])
                self.assertIsNone(parser.output_bss)

    def test_set_copies_its_list(self) -> None:
        compiler = l2_main.Compiler()
        vm = compiler.parser.compile_time_vm
        fragments = ["a: dq 1"]
        vm.stack = [fragments]
        compiler.dictionary.lookup("ct-output-set-data").compile_time_intrinsic(vm)
        fragments.append("b: dq 2")
        self.assertEqual(compiler.parser.output_data, ["a: dq 1"])


class BoundaryTests(unittest.TestCase):
    def test_separator_rule(self) -> None:
        F = _AsmFragment
        cases = [
            (["code", F("frag")], "code\nfrag"),                     # neither side breaks: one LF
            ([F("a"), F("b")], "a\nb"),
            ([F("a\n"), F("b")], "a\nb"),                            # preceding LF
            ([F("a"), F("\nb")], "a\nb"),                            # following LF
            ([F("a\r\n"), F("b")], "a\r\nb"),                        # CRLF kept, no extra LF
            ([F("a\r"), F("b")], "a\rb"),                            # lone CR is a terminator
            ([F("a"), F(""), F("b")], "a\nb"),                       # empty fragment emits nothing
            ([F(""), "code"], "code"),
            ([F("  x  "), F("\ty")], "  x  \n\ty"),                  # whitespace untouched
            ([F("one\ntwo"), "code"], "one\ntwo\ncode"),             # embedded line breaks verbatim
            ([F("end\n"), "code"], "end\ncode"),
            (["code", "", "next"], "code\n\nnext"),                 # compiler lines unchanged
        ]
        for lines, expected in cases:
            with self.subTest(lines=lines):
                self.assertEqual(_join_asm_lines(lines), expected)

    def test_snapshot_uses_the_rule_once_fragments_are_present(self) -> None:
        emission = l2_main.Emission(text=["a", _AsmFragment("b\n"), "c"])
        self.assertTrue(emission.snapshot().startswith("section .text\na\nb\n\nc\n"))  # plain join
        emission.fragments = True
        self.assertTrue(emission.snapshot().startswith("section .text\na\nb\nc\nsection .note"))
        self.assertEqual(emission.text_byte_length(), len("a\nb\nc"))
        self.assertEqual(emission.snapshot_byte_length(), len(emission.snapshot().encode()))


class RetiredWordTests(unittest.TestCase):
    def test_retired_words_are_gone(self) -> None:
        dictionary = l2_main.Compiler().dictionary
        for name in ("prelude-clear", "prelude-append", "prelude-set", "data-clear", "data-append", "data-set",
                     "bss-clear", "bss-append", "bss-set"):
            with self.subTest(name=name):
                self.assertIsNone(dictionary.lookup(name))


if __name__ == "__main__":
    unittest.main()
