#!/usr/bin/env python3
"""Fast paths of the compile-time VM and the parser give the same results,
errors and notes as the general paths: Python equivalents of stdlib :asm
words (integers, strings, wrapping, strictness, division by zero), direct
calls of plain definitions (recursion, exit, error notes through nested
words), a user word that redefines an :asm word, the memoized text peephole
pass, and the tokenizer's whitespace-skipping pattern."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("L2_ASM_CACHE_DIR", tempfile.mkdtemp(prefix="l2-fast-cache-"))

import l2_main  # noqa: E402


def compile_source(source: str) -> "subprocess.CompletedProcess[str]":
    with tempfile.TemporaryDirectory(prefix="l2-fast-") as raw:
        path = Path(raw) / "prog.sl"
        path.write_text(source)
        return subprocess.run(
            [sys.executable, str(ROOT / "main.py"), str(path), "--no-daemon", "--no-artifact", "--force"],
            capture_output=True, text=True, cwd=ROOT, timeout=120,
        )


def ct_program(body: str, *, prelude: str = "") -> str:
    return f"import stdlib.sl\n{prelude}word check {body} end\ncompile-time check\nword main 0 end\n"


class NativeEquivalentTests(unittest.TestCase):
    def test_integer_results_and_wrapping(self) -> None:
        proc = compile_source(ct_program(
            "9223372036854775807 1 + -9223372036854775808 == static-assert "
            "-9223372036854775808 1 - 9223372036854775807 == static-assert "
            "1 63 shl 0 < static-assert 7 -2 % 1 == static-assert -7 2 / -3 == static-assert "
            "3 4 max 4 == static-assert 3 4 min 3 == static-assert -5 abs 5 == static-assert "
            "1 0 and 0 == static-assert 1 not 0 == static-assert"))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_exact_values(self) -> None:
        # Printed, not compared with == (which takes the same fast path): an
        # off-by-one in it would otherwise cancel out.  Values match the
        # compiler from before the fast path.
        program = (
            "import stdlib.sl\nword show\n"
            "  9223372036854775807 1 + puti cr  -9223372036854775808 1 - puti cr\n"
            "  7 -2 % puti cr  -7 2 / puti cr  6 7 * puti cr\n"
            "  3 5 < puti cr  5 3 < puti cr  4 4 == puti cr  4 5 != puti cr  4 4 <= puti cr  5 4 >= puti cr\n"
            "  1 63 shl puti cr  -16 2 sar puti cr  -1 60 shr puti cr  1 3 rol puti cr\n"
            "  3 9 max puti cr  3 9 min puti cr  -5 abs puti cr  5 neg puti cr  7 inc puti cr  7 dec puti cr\n"
            "  12 10 band puti cr  12 10 bor puti cr  12 10 bxor puti cr  0 bnot puti cr\n"
            "  1 0 and puti cr  1 0 or puti cr  0 not puti cr  15 0 10 clamp puti cr\n"
            "end\ncompile-time show\nword main 0 end\n")
        proc = compile_source(program)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        values = [line for line in proc.stdout.splitlines() if not line.startswith("[")]
        self.assertEqual(values, (
            "-9223372036854775808 9223372036854775807 1 -3 42 1 0 1 1 1 1 -9223372036854775808 -4 15 8 "
            "9 3 5 -5 8 6 8 14 6 -1 0 1 1 10").split())

    def test_string_operand_uses_its_cells(self) -> None:
        proc = compile_source(ct_program('"ab" 1 + 3 == static-assert drop'))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_division_by_zero_reports_through_nested_words(self) -> None:
        proc = compile_source(
            "import stdlib.sl\nword f 1 0 / end\nword g f end\nword check g end\n"
            "compile-time check\nword main 0 end\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error[ct-argument-invalid]: integer division overflow or division by zero at compile time",
                      proc.stderr)
        self.assertIn("note: compile-time stack: check -> g -> f -> /", proc.stderr)
        self.assertIn("2 | word f 1 0 / end", proc.stderr)

    def test_strict_type_errors(self) -> None:
        proc = compile_source(ct_program("list-new 1 <"))
        self.assertIn("error[ct-argument-type]: '<' expects integers or strings at compile time, "
                      "got [list, integer]", proc.stderr)
        self.assertIn("note: compile-time stack: check -> <", proc.stderr)
        proc = compile_source(ct_program("nil 1 +"))
        self.assertIn("'+' expects integers or strings at compile time, got [nil, integer]", proc.stderr)

    def test_asm_word_underflow_is_reported_before_it_runs(self) -> None:
        proc = compile_source(ct_program("puti cr"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("error[ct-stack-underflow]: stack underflow in 'puti': needs 1 value(s), has 0", proc.stderr)
        self.assertEqual([line for line in proc.stdout.splitlines() if not line.startswith("[")], [])
        proc = compile_source(ct_program("1 rot"))
        self.assertIn("stack underflow in 'rot'", proc.stderr)

    def test_asm_depth_scan(self) -> None:
        depth = l2_main._asm_required_depth
        self.assertEqual(depth("mov rax, [r12]\nadd r12, 8\nadd [r12], rax"), 2)
        self.assertEqual(depth("sub r12, 8\nmov [r12], rax"), 0)  # a push needs nothing
        self.assertEqual(depth("mov rax, [r12 + 16]"), 3)
        self.assertEqual(depth("cmp rax, 0\njz .x\nmov rax, [r12 + 32]\n.x:"), 0)  # after a jump: not certain
        self.assertEqual(depth("mov rax, [r12]\nmov r12, rbx\nmov rax, [r12 + 64]"), 1)  # r12 replaced
        self.assertEqual(depth("mov rax, [r12]  ; [r12 + 99] in a comment"), 1)

    def test_redefined_word_is_not_the_stdlib_equivalent(self) -> None:
        proc = compile_source(ct_program("5 3 + 2 == static-assert", prelude="word + - end\n"))
        self.assertEqual(proc.returncode, 0, proc.stderr)


class ConstantFoldingTests(unittest.TestCase):
    def build_and_run(self, source: str):
        with tempfile.TemporaryDirectory(prefix="l2-fold-") as raw:
            path = Path(raw) / "prog.sl"
            path.write_text(source)
            asm = Path(raw) / "prog.asm"
            subprocess.run([sys.executable, str(ROOT / "main.py"), str(path), "--no-daemon", "--force",
                            "--emit-asm", "-o", str(asm)], check=True, capture_output=True, cwd=ROOT)
            exe = Path(raw) / "prog"
            subprocess.run([sys.executable, str(ROOT / "main.py"), str(path), "--no-daemon", "--force",
                            "-o", str(exe)], check=True, capture_output=True, cwd=ROOT)
            ran = subprocess.run([str(exe)], capture_output=True, text=True)
            return asm.read_text(), ran

    def test_redefined_word_is_not_folded(self) -> None:
        _asm, ran = self.build_and_run("import stdlib.sl\nword + - end\nword main 5 3 + puti cr 0 end\n")
        self.assertEqual(ran.stdout.strip(), "2")

    def test_more_words_fold(self) -> None:
        asm, ran = self.build_and_run(
            "import stdlib.sl\nword main 3 9 max puti cr 12 10 band puti cr 1 4 shl puti cr -5 abs puti cr 0 end\n")
        self.assertEqual(ran.stdout.split(), ["9", "8", "16", "5"])
        for word in ("w_max", "w_band", "w_shl", "w_abs"):
            self.assertNotIn(f"call {word}", asm)

    def test_division_by_zero_is_left_to_the_program(self) -> None:
        asm, ran = self.build_and_run("import stdlib.sl\nword main 1 0 / puti cr 0 end\n")
        self.assertIn("call w_", asm)
        self.assertNotEqual(ran.returncode, 0)  # SIGFPE at runtime, as before

    def test_names_without_a_definition_still_fold(self) -> None:
        # No stdlib: `+` exists only through folding (as it always did).
        asm, ran = self.build_and_run(
            ":asm exit_top {\n    mov rax, 60\n    mov rdi, [r12]\n    add r12, 8\n    syscall\n} ;\n"
            "word main 40 2 + exit_top end\n")
        self.assertEqual(ran.returncode, 42)


class PlainDefinitionTests(unittest.TestCase):
    def test_recursion_and_exit(self) -> None:
        proc = compile_source(
            "import stdlib.sl\n"
            "word fact dup 1 <= if drop 1 else dup 1 - fact * end end\n"
            "word check 10 fact 3628800 == static-assert end\ncompile-time check\n"
            "word early 1 exit 2 end\nword check2 early 1 == static-assert end\ncompile-time check2\n"
            "word main 0 end\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_events_still_see_each_call(self) -> None:
        vm_calls = []
        compiler = l2_main.Compiler(include_paths=[ROOT, ROOT / "stdlib"])
        bus = l2_main._ensure_parser_event_bus(compiler.parser)
        bus.subscribe("ct.execute.begin", lambda record: vm_calls.append(record["payload"]["word"]))
        with tempfile.TemporaryDirectory(prefix="l2-fast-events-") as raw:
            path = Path(raw) / "prog.sl"
            path.write_text(ct_program("1 2 + drop", prelude="word f 1 drop end\n").replace(
                "1 2 + drop", "1 2 + drop f"))
            compiler.parse_file(path)
        self.assertIn("+", vm_calls)
        self.assertIn("f", vm_calls)


class PeepholeMemoTests(unittest.TestCase):
    def test_memo_and_pack_return_the_computed_result(self) -> None:
        text = "\n".join([
            "w_a:", "    nop", "    mov rax, rax", "    jmp L_1", "L_1:", "    jz L_2", "    jmp L_3",
            "L_2:", "    add rax, 0", "    ret", "L_3:", "L_4:", "    jmp L_5", "L_5:", "    ret", "",
        ])
        expected = l2_main._optimize_emitted_asm_text_uncached(text)
        l2_main._PEEPHOLE_MEMO.clear()
        first = l2_main.optimize_emitted_asm_text(text)
        second = l2_main.optimize_emitted_asm_text(text)  # from memory
        l2_main._PEEPHOLE_MEMO.clear()
        fresh = l2_main._CachePack("peephole", l2_main._compiler_source_fingerprint, 32 * 1024 * 1024)
        saved = l2_main._PEEPHOLE_STORE
        l2_main._PEEPHOLE_STORE = fresh
        try:
            third = l2_main.optimize_emitted_asm_text(text)  # from the pack file
        finally:
            l2_main._PEEPHOLE_STORE = saved
        for result in (first, second, third):
            self.assertEqual(result[0], expected[0])
            self.assertEqual(result[1], expected[1])
        # Callers may change the stats they get.
        first[1]["removed_nops"] = 99
        self.assertEqual(l2_main.optimize_emitted_asm_text(text)[1], expected[1])
        with_logs = l2_main.optimize_emitted_asm_text(text, collect_pass_logs=True)
        self.assertEqual(with_logs, l2_main._optimize_emitted_asm_text_uncached(text, collect_pass_logs=True))


class TokenizerPatternTests(unittest.TestCase):
    def test_whitespace_in_a_separator_keeps_the_plain_pattern(self) -> None:
        # A separator may contain whitespace; then whitespace is not skipped
        # ahead of a token (it may start one), and the token is group 1.
        reader = l2_main.Reader()
        reader.separators = (",", " -")
        self.assertTrue(reader._build_token_re().pattern.startswith("("))
        reader = l2_main.Reader()
        self.assertTrue(reader._build_token_re().pattern.startswith("["))

    def test_tokens_skip_all_l2_whitespace(self) -> None:
        tokens = l2_main.Reader().tokenize("a b　 c\t\n d # note\n\"s t\" 'x'")
        self.assertEqual([t.lexeme for t in tokens], ["a", "b", "c", "d", '"s t"', "'x'"])
        self.assertEqual([(t.line, t.column) for t in tokens], [(1, 0), (1, 2), (1, 5), (2, 1), (3, 0), (3, 6)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
