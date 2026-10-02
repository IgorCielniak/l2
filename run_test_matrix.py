#!/usr/bin/env python3
"""Run the full L2 test matrix and summarize each suite."""

from __future__ import annotations

import argparse
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass
class TestResult:
    name: str
    command: list[str]
    returncode: Optional[int]
    duration: float
    output: str
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.error and self.returncode is None:
            return "TIMEOUT" if "timed out" in self.error else "ERROR"
        return "PASS" if self.returncode == 0 else "FAIL"


class TestMatrix:
    def __init__(self, root: Path, timeout: float) -> None:
        self.root = root
        self.timeout = timeout
        self.python = sys.executable

    def commands(self) -> list[tuple[str, list[str]]]:
        python_tests = set(self.root.glob("test_*.py"))
        for directory in (self.root / "tools", self.root / "tests", self.root / "extra_tests"):
            if directory.exists():
                python_tests.update(directory.rglob("test_*.py"))
        python_tests = sorted(path for path in python_tests if "vendor" not in path.parts)
        return [
            ("L2 runtime suite", [self.python, "test.py"]),
            ("L2 compile-time main", [self.python, "test.py", "--ct-run-main"]),
            ("L2 script mode", [self.python, "test.py", "--script"]),
            ("L2 leak-check mode", [self.python, "test.py", "--leak-check"]),
            ("Compiler integrity", [self.python, "main.py", "--check-integrity"]),
            ("Build runtime library", ["bash", "tools/build_l2_lib.sh"]),
            *[
                (f"Python: {path.relative_to(self.root).as_posix()}", [self.python, str(path.relative_to(self.root))])
                for path in python_tests
            ],
        ]

    def run(self, name: str, command: list[str]) -> TestResult:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                cwd=self.root,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
            return TestResult(name, command, proc.returncode, time.monotonic() - started,
                              proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired as exc:
            stdout = self._decode_output(exc.stdout)
            stderr = self._decode_output(exc.stderr)
            return TestResult(name, command, None, time.monotonic() - started,
                              stdout + stderr, f"timed out after {self.timeout:g}s")
        except OSError as exc:
            return TestResult(name, command, None, time.monotonic() - started, "", str(exc))

    @staticmethod
    def _decode_output(value: object) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else ""

    @staticmethod
    def detail(result: TestResult) -> str:
        if result.error:
            return result.error
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        for line in reversed(lines):
            match = re.fullmatch(
                r"Total: (\d+), passed: (\d+), updated: \d+, skipped: \d+, failed: (\d+)",
                line,
            )
            if match:
                return f"{match[2]}/{match[1]} passed, {match[3]} failed"
            match = re.fullmatch(r"Total: (\d+), passed: (\d+), failed: (\d+)", line)
            if match:
                return f"{match[2]}/{match[1]} passed, {match[3]} failed"
            match = re.fullmatch(r"Total: (\d+)/(\d+) tests passed", line)
            if match:
                return f"{match[1]}/{match[2]} passed"
            match = re.fullmatch(r"Ran (\d+) tests? in .*", line)
            if match:
                return f"{match[1]} tests ran"
            match = re.fullmatch(r"(\d+) checks, (\d+) passed, (\d+) failed", line)
            if match:
                return f"{match[2]}/{match[1]} passed, {match[3]} failed"
        for line in reversed(lines):
            if line.startswith("[info]"):
                return line[:72]
        return (lines[-1] if lines else "")[:72]

    @staticmethod
    def print_table(results: Sequence[TestResult]) -> None:
        terminal_width = max(80, shutil.get_terminal_size((80, 20)).columns)
        name_width = min(40, max(24, terminal_width - 48))
        detail_width = max(16, terminal_width - name_width - 22)
        header = f"{'SUITE':<{name_width}}  {'STATUS':<7}  {'SECONDS':>8}  DETAIL"
        print("\n" + header[:terminal_width])
        print("-" * terminal_width)
        for result in results:
            detail = TestMatrix.detail(result).replace("\n", " ")
            name = result.name
            if len(name) > name_width:
                name = name[:name_width - 3] + "..."
            if len(detail) > detail_width:
                detail = detail[:detail_width - 3] + "..."
            row = f"{name:<{name_width}}  {result.status:<7}  {result.duration:>8.2f}  {detail}"
            print(row[:terminal_width])
        passed = sum(result.status == "PASS" for result in results)
        print(f"\nOverall: {passed}/{len(results)} suites passed")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run all L2 test modes and standalone test scripts")
    parser.add_argument("--timeout", type=float, default=1800,
                        help="maximum seconds allowed for each suite (default: 1800)")
    parser.add_argument("--list", action="store_true", help="list planned commands without running them")
    parser.add_argument("-v", "--verbose", action="store_true", help="print captured output for every suite")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(__file__).resolve().parent
    matrix = TestMatrix(root, args.timeout)
    commands = matrix.commands()

    if args.list:
        for name, command in commands:
            print(f"{name}: {shlex.join(command)}")
        return 0

    results = []
    for name, command in commands:
        print(f"[RUN ] {name}", flush=True)
        result = matrix.run(name, command)
        results.append(result)
        print(f"[{result.status:<5}] {name} ({result.duration:.2f}s)", flush=True)
        if args.verbose and result.output:
            print(result.output, end="" if result.output.endswith("\n") else "\n")
        if result.status != "PASS" and result.output:
            output = result.output.rstrip()
            if len(output) > 6000:
                output = "[earlier output truncated]\n" + output[-6000:]
            print(output)
        if result.error:
            print(result.error)

    matrix.print_table(results)
    return 0 if all(result.status == "PASS" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
