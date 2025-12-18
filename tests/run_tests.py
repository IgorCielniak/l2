#!/usr/bin/env python3
"""Simple end-to-end test runner for L2.

Each test case provides an L2 program source and an expected stdout. The runner
invokes the bootstrap compiler on the fly and executes the produced binary.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
COMPILER = ROOT / "main.py"
PYTHON = Path(sys.executable)


@dataclass
class TestCase:
    name: str
    source: str
    expected_stdout: str


CASES: List[TestCase] = [
    TestCase(
        name="call_syntax_parens",
        source=f"""
import {ROOT / 'stdlib/stdlib.sl'}
import {ROOT / 'stdlib/io.sl'}
import {ROOT / 'fn.sl'}

: main
    2 40 +
    puts
    extend-syntax
    foo(1, 2)
    puts
    0
;

fn foo(int a, int b){{
    return a + b;
}}
""",
        expected_stdout="42\n3\n",
    ),
    TestCase(
        name="loops_and_cmp",
        source=f"""
import {ROOT / 'stdlib/stdlib.sl'}
import {ROOT / 'stdlib/io.sl'}

: main
    0
    5 for
        1 +
    next
    puts
    5 5 == puts
    5 4 == puts
    0
;
""",
        expected_stdout="5\n1\n0\n",
    ),
    TestCase(
        name="override_dup_compile_time",
        source=f"""
import {ROOT / 'stdlib/stdlib.sl'}
import {ROOT / 'stdlib/io.sl'}

: dup
    6
;
compile-only

: emit-overridden
    "dup" use-l2-ct
    42
    dup
    int>string
    nil
    token-from-lexeme
    list-new
    swap
    list-append
    inject-tokens
;
immediate
compile-only

: main
    emit-overridden
    puts
    0
;
""",
        expected_stdout="6\n",
    ),
    TestCase(
        name="string_puts",
        source=f"""
import {ROOT / 'stdlib/stdlib.sl'}
import {ROOT / 'stdlib/io.sl'}

: main
    \"hello world\" puts
    \"line1\\nline2\" puts
    \"\" puts
    0
;
""",
        expected_stdout="hello world\nline1\nline2\n\n",
    ),
]


def run_case(case: TestCase) -> None:
    print(f"[run] {case.name}")
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / f"{case.name}.sl"
        exe_path = Path(tmp) / f"{case.name}.out"
        src_path.write_text(case.source.strip() + "\n", encoding="utf-8")

        compile_cmd = [str(PYTHON), str(COMPILER), str(src_path), "-o", str(exe_path)]
        compile_result = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if compile_result.returncode != 0:
            sys.stderr.write("[fail] compile error\n")
            sys.stderr.write(compile_result.stdout)
            sys.stderr.write(compile_result.stderr)
            raise SystemExit(compile_result.returncode)

        run_result = subprocess.run(
            [str(exe_path)],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if run_result.returncode != 0:
            sys.stderr.write("[fail] execution error\n")
            sys.stderr.write(run_result.stdout)
            sys.stderr.write(run_result.stderr)
            raise SystemExit(run_result.returncode)

        if run_result.stdout != case.expected_stdout:
            sys.stderr.write(f"[fail] output mismatch for {case.name}\n")
            sys.stderr.write("expected:\n" + case.expected_stdout)
            sys.stderr.write("got:\n" + run_result.stdout)
            raise SystemExit(1)

    print(f"[ok] {case.name}")


def main() -> None:
    for case in CASES:
        run_case(case)
    print("[all tests passed]")


if __name__ == "__main__":
    main()
