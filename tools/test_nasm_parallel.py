#!/usr/bin/env python3
"""Parallel assembly: which word definitions may move to another part, how
labels used across parts are aliased, the fallback to one nasm run, and that
programs built from parts behave exactly like programs built in one piece."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main  # noqa: E402

PRELUDE = "section .text\n%define K 8\nglobal _start\n_start:\n    call w_a\n    mov rdi, 0\n    mov rax, 60\n    syscall\n"


def plan(text: str):
    return l2_main._nasm_plan_parts(text, 4, force=True)


def moved_names(text: str):
    result = plan(text)
    if result is None:
        return None
    parts, _aliases = result
    names = set()
    for part in parts[1:]:
        for line in part.split("\n"):
            if line.startswith("w_") and line.endswith(":"):
                names.add(line[:-1])
    return names


def tool_available(name: str) -> bool:
    return any(os.access(os.path.join(directory, name), os.X_OK)
               for directory in os.environ.get("PATH", "").split(os.pathsep))


class PlanTests(unittest.TestCase):
    def test_terminated_blocks_move_and_get_aliases(self) -> None:
        text = PRELUDE + "w_a:\n    call w_b\n    ret\nw_b:\n    mov rax, K\n    ret\n"
        parts, aliases = plan(text)
        self.assertGreaterEqual(len(parts), 2)
        # _start ends in syscall (not ret/jmp), so w_a, which follows it,
        # stays; w_b moves, and w_a's call to it goes through an alias.
        self.assertEqual(aliases, ["w_b"])
        self.assertIn("w_a:", parts[0])
        self.assertIn("extern __l2_part_w_b\n%define w_b __l2_part_w_b", parts[0])
        joined = "\n".join(parts[1:])
        self.assertIn("%define K 8", joined)  # header defines are repeated
        self.assertIn("global __l2_part_w_b:hidden", joined)
        self.assertIn("__l2_part_w_b equ w_b", joined)

    def test_fallthrough_keeps_blocks_in_place(self) -> None:
        # w_a runs into w_b, and a label ends w_c: none of them may move.
        text = PRELUDE + "w_a:\n    inc rax\nw_b:\n    ret\nw_c:\n    jmp .x\n.x:\nw_d:\n    ret\n"
        self.assertEqual(moved_names(text) or set(), set())

    def test_exported_and_directive_blocks_stay(self) -> None:
        text = (PRELUDE + "global w_e\n"
                "w_a:\n    ret\nw_e:\n    ret\nw_f:\n    db 1\n    ret\nw_g:\n    align 16\n    ret\n"
                "w_h:\n    mov rax, 'x'\n    ret\nw_i:\n    ret\n")
        moved = moved_names(text)
        self.assertTrue({"w_e", "w_f", "w_g", "w_h"}.isdisjoint(moved))
        self.assertTrue(moved & {"w_a", "w_i"})

    def test_whole_file_cases(self) -> None:
        body = "w_a:\n    ret\nw_b:\n    ret\n"
        self.assertIsNone(plan(PRELUDE + body + "%macro m 0\n%endmacro\n"))
        self.assertIsNone(plan(PRELUDE + "default rel\n" + body))
        self.assertIsNone(plan(PRELUDE + "section .rodata\nx: dq 1\nsection .text\n" + body))
        self.assertIsNone(l2_main._nasm_plan_parts(PRELUDE + body, 4, force=False))  # too small

    def test_disabled_by_environment(self) -> None:
        os.environ["L2_NASM_PARALLEL"] = "0"
        try:
            self.assertFalse(l2_main._run_nasm_parallel(Path("x.asm"), Path("x.o"), PRELUDE))
        finally:
            del os.environ["L2_NASM_PARALLEL"]


@unittest.skipUnless(tool_available("nasm") and tool_available("ld"), "needs nasm and ld")
class BuildTests(unittest.TestCase):
    def build_and_run(self, source: Path, mode: str, out_dir: Path):
        exe = out_dir / f"prog_{mode}"
        env = dict(os.environ, L2_NASM_PARALLEL=mode)
        built = subprocess.run([sys.executable, str(ROOT / "main.py"), str(source), "--no-daemon", "--force",
                                "-o", str(exe), "--temp-dir", str(out_dir / mode)],
                               capture_output=True, text=True, cwd=ROOT, env=env, timeout=180)
        self.assertEqual(built.returncode, 0, built.stderr)
        ran = subprocess.run([str(exe)], capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
        return built.stdout.replace(f"prog_{mode}", "prog_M"), built.stderr, ran.returncode, ran.stdout

    def test_parts_build_the_same_program(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-nasm-par-") as raw:
            out_dir = Path(raw)
            for source in (ROOT / "tools/bench/large_generated.sl", ROOT / "tests/general/hello.sl"):
                serial = self.build_and_run(source, "0", out_dir)
                parallel = self.build_and_run(source, "force", out_dir)
                self.assertEqual(serial, parallel, source.name)

    def test_large_program_is_split(self) -> None:
        text_path = None
        with tempfile.TemporaryDirectory(prefix="l2-nasm-split-") as raw:
            out = Path(raw) / "large.asm"
            subprocess.run([sys.executable, str(ROOT / "main.py"), str(ROOT / "tools/bench/large_generated.sl"),
                            "--no-daemon", "--force", "--emit-asm", "-o", str(out)],
                           capture_output=True, cwd=ROOT, check=True, timeout=180)
            text_path = out.read_text()
        result = l2_main._nasm_plan_parts(text_path, 4, force=False)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(result[0]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
