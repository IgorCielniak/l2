#!/usr/bin/env python3
"""Compiler-focused test runner for the L2 toolchain."""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import importlib.util
import json
import os
import platform
import shlex
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DEFAULT_EXTRA_TESTS = [
    "extra_tests/ct_test.sl",
    "extra_tests/args.sl",
    "extra_tests/c_extern.sl",
    "extra_tests/fn_test.sl",
    "extra_tests/nob_test.sl",
]

COLORS = {
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "reset": "\033[0m",
}


def colorize(text: str, color: str) -> str:
    return COLORS.get(color, "") + text + COLORS["reset"]


def format_status(tag: str, color: str) -> str:
    return colorize(f"[{tag}]", color)


def normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n")


def diff_text(expected: str, actual: str, label: str) -> str:
    expected_lines = expected.splitlines(keepends=True)
    actual_lines = actual.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(expected_lines, actual_lines, fromfile=f"{label} (expected)", tofile=f"{label} (actual)")
    )


def resolve_path(root: Path, raw: str) -> Path:
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else root / candidate


def match_patterns(name: str, patterns: Sequence[str]) -> bool:
    if not patterns:
        return True
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or pattern in name:
            return True
    return False


def quote_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def is_arm_host() -> bool:
    machine = platform.machine().lower()
    return machine.startswith("arm") or machine.startswith("aarch")


def qemu_emulator() -> str:
    return os.environ.get("L2_QEMU", "qemu-x86_64")


def ensure_arm_runtime_support() -> None:
    if not is_arm_host():
        return
    emulator = qemu_emulator()
    if shutil.which(emulator):
        return
    print(f"[error] {emulator} not found; install qemu-user or set L2_QEMU", file=sys.stderr)
    sys.exit(1)


def wrap_runtime_command(cmd: List[str]) -> List[str]:
    if not is_arm_host():
        return cmd
    emulator = qemu_emulator()
    if not emulator:
        return cmd
    first = Path(cmd[0]).name if cmd else ""
    if first == Path(emulator).name:
        return cmd
    return [emulator, *cmd]


def read_json(meta_path: Path) -> Dict[str, Any]:
    if not meta_path.exists():
        return {}
    raw = meta_path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {meta_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"metadata in {meta_path} must be an object")
    return data


def read_args_file(path: Path) -> List[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    return shlex.split(text)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@dataclass
class TestCaseConfig:
    description: Optional[str] = None
    compile_only: bool = False
    expect_compile_error: bool = False
    expected_exit: int = 0
    skip: bool = False
    skip_reason: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    args: Optional[List[str]] = None
    stdin: Optional[str] = None
    binary: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    libs: List[str] = field(default_factory=list)

    @classmethod
    def from_meta(cls, data: Dict[str, Any]) -> "TestCaseConfig":
        cfg = cls()
        if not data:
            return cfg
        if "description" in data:
            if not isinstance(data["description"], str):
                raise ValueError("description must be a string")
            cfg.description = data["description"].strip() or None
        if "compile_only" in data:
            cfg.compile_only = bool(data["compile_only"])
        if "expect_compile_error" in data:
            cfg.expect_compile_error = bool(data["expect_compile_error"])
        if "expected_exit" in data:
            cfg.expected_exit = int(data["expected_exit"])
        if "skip" in data:
            cfg.skip = bool(data["skip"])
        if "skip_reason" in data:
            if not isinstance(data["skip_reason"], str):
                raise ValueError("skip_reason must be a string")
            cfg.skip_reason = data["skip_reason"].strip() or None
        if "env" in data:
            env = data["env"]
            if not isinstance(env, dict):
                raise ValueError("env must be an object of key/value pairs")
            cfg.env = {str(k): str(v) for k, v in env.items()}
        if "args" in data:
            args_val = data["args"]
            if not isinstance(args_val, list) or not all(isinstance(item, str) for item in args_val):
                raise ValueError("args must be a list of strings")
            cfg.args = list(args_val)
        if "stdin" in data:
            if not isinstance(data["stdin"], str):
                raise ValueError("stdin must be a string")
            cfg.stdin = data["stdin"]
        if "binary" in data:
            if not isinstance(data["binary"], str):
                raise ValueError("binary must be a string")
            cfg.binary = data["binary"].strip() or None
        if "tags" in data:
            tags = data["tags"]
            if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
                raise ValueError("tags must be a list of strings")
            cfg.tags = list(tags)
        if "requires" in data:
            requires = data["requires"]
            if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
                raise ValueError("requires must be a list of module names")
            cfg.requires = [item.strip() for item in requires if item.strip()]
        if "libs" in data:
            libs = data["libs"]
            if not isinstance(libs, list) or not all(isinstance(item, str) for item in libs):
                raise ValueError("libs must be a list of strings")
            cfg.libs = [item.strip() for item in libs if item.strip()]
        return cfg


@dataclass
class TestCase:
    name: str
    source: Path
    binary_stub: str
    expected_stdout: Path
    expected_stderr: Path
    compile_expected: Path
    stdin_path: Path
    args_path: Path
    meta_path: Path
    build_dir: Path
    config: TestCaseConfig

    @property
    def binary_path(self) -> Path:
        binary_name = self.config.binary or self.binary_stub
        return self.build_dir / binary_name

    def runtime_args(self) -> List[str]:
        if self.config.args is not None:
            return list(self.config.args)
        return read_args_file(self.args_path)

    def stdin_data(self) -> Optional[str]:
        if self.config.stdin is not None:
            return self.config.stdin
        if self.stdin_path.exists():
            return self.stdin_path.read_text(encoding="utf-8")
        return None

    def description(self) -> str:
        return self.config.description or ""


@dataclass
class CaseResult:
    case: TestCase
    status: str
    stage: str
    message: str
    details: Optional[str] = None
    duration: float = 0.0

    @property
    def failed(self) -> bool:
        return self.status == "failed"


class TestRunner:
    def __init__(self, root: Path, args: argparse.Namespace) -> None:
        self.root = root
        self.args = args
        self.tests_dir = resolve_path(root, args.tests_dir)
        self.build_dir = resolve_path(root, args.build_dir)
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.main_py = self.root / "main.py"
        self.base_env = os.environ.copy()
        self._module_cache: Dict[str, bool] = {}
        extra_entries = list(DEFAULT_EXTRA_TESTS)
        if args.extra:
            extra_entries.extend(args.extra)
        self.extra_sources = [resolve_path(self.root, entry) for entry in extra_entries]
        self.cases = self._discover_cases()

    def _discover_cases(self) -> List[TestCase]:
        sources: List[Path] = []
        if self.tests_dir.exists():
            sources.extend(sorted(self.tests_dir.glob("*.sl")))
        for entry in self.extra_sources:
            if entry.is_dir():
                sources.extend(sorted(entry.glob("*.sl")))
                continue
            sources.append(entry)

        cases: List[TestCase] = []
        seen: Set[Path] = set()
        for source in sources:
            try:
                resolved = source.resolve()
            except FileNotFoundError:
                continue
            if not resolved.exists() or resolved in seen:
                continue
            seen.add(resolved)
            case = self._case_from_source(resolved)
            cases.append(case)
        cases.sort(key=lambda case: case.name)
        return cases

    def _case_from_source(self, source: Path) -> TestCase:
        meta_path = source.with_suffix(".meta.json")
        config = TestCaseConfig()
        if meta_path.exists():
            config = TestCaseConfig.from_meta(read_json(meta_path))
        try:
            relative = source.relative_to(self.root).as_posix()
        except ValueError:
            relative = source.as_posix()
        if relative.endswith(".sl"):
            relative = relative[:-3]
        return TestCase(
            name=relative,
            source=source,
            binary_stub=source.stem,
            expected_stdout=source.with_suffix(".expected"),
            expected_stderr=source.with_suffix(".stderr"),
            compile_expected=source.with_suffix(".compile.expected"),
            stdin_path=source.with_suffix(".stdin"),
            args_path=source.with_suffix(".args"),
            meta_path=meta_path,
            build_dir=self.build_dir,
            config=config,
        )

    def run(self) -> int:
        if not self.tests_dir.exists():
            print("tests directory not found", file=sys.stderr)
            return 1
        if not self.main_py.exists():
            print("main.py missing; cannot compile tests", file=sys.stderr)
            return 1
        selected = [case for case in self.cases if match_patterns(case.name, self.args.patterns)]
        if not selected:
            print("no tests matched the provided filters", file=sys.stderr)
            return 1
        if self.args.list:
            self._print_listing(selected)
            return 0
        results: List[CaseResult] = []
        for case in selected:
            result = self._run_case(case)
            results.append(result)
            self._print_result(result)
            if result.failed and self.args.stop_on_fail:
                break
        self._print_summary(results)
        return 1 if any(r.failed for r in results) else 0

    def _print_listing(self, cases: Sequence[TestCase]) -> None:
        width = max((len(case.name) for case in cases), default=0)
        for case in cases:
            desc = case.description()
            suffix = f" - {desc}" if desc else ""
            print(f"{case.name.ljust(width)}{suffix}")

    def _run_case(self, case: TestCase) -> CaseResult:
        missing = [req for req in case.config.requires if not self._module_available(req)]
        if missing:
            reason = f"missing dependency: {', '.join(sorted(missing))}"
            return CaseResult(case, "skipped", "deps", reason)
        if case.config.skip:
            reason = case.config.skip_reason or "skipped via metadata"
            return CaseResult(case, "skipped", "skip", reason)
        start = time.perf_counter()
        compile_proc = self._compile(case)
        if case.config.expect_compile_error:
            result = self._handle_expected_compile_failure(case, compile_proc)
            result.duration = time.perf_counter() - start
            return result
        if compile_proc.returncode != 0:
            details = self._format_process_output(compile_proc)
            duration = time.perf_counter() - start
            return CaseResult(case, "failed", "compile", f"compiler exited {compile_proc.returncode}", details, duration)
        updated_notes: List[str] = []
        compile_status, compile_note, compile_details = self._check_compile_output(case, compile_proc)
        if compile_status == "failed":
            duration = time.perf_counter() - start
            return CaseResult(case, compile_status, "compile", compile_note, compile_details, duration)
        if compile_status == "updated" and compile_note:
            updated_notes.append(compile_note)
        if case.config.compile_only:
            duration = time.perf_counter() - start
            if updated_notes:
                return CaseResult(case, "updated", "compile", "; ".join(updated_notes), details=None, duration=duration)
            return CaseResult(case, "passed", "compile", "compile-only", details=None, duration=duration)
        run_proc = self._run_binary(case)
        if run_proc.returncode != case.config.expected_exit:
            duration = time.perf_counter() - start
            message = f"expected exit {case.config.expected_exit}, got {run_proc.returncode}"
            details = self._format_process_output(run_proc)
            return CaseResult(case, "failed", "run", message, details, duration)
        if case.source.stem == "nob_test":
            status, note, details = self._compare_nob_test_stdout(case, run_proc.stdout)
        else:
            status, note, details = self._compare_stream(
                case,
                "stdout",
                case.expected_stdout,
                run_proc.stdout,
                create_on_update=True,
            )
        if status == "failed":
            duration = time.perf_counter() - start
            return CaseResult(case, status, "stdout", note, details, duration)
        if status == "updated" and note:
            updated_notes.append(note)
        stderr_status, stderr_note, stderr_details = self._compare_stream(
            case,
            "stderr",
            case.expected_stderr,
            run_proc.stderr,
            create_on_update=True,
            ignore_when_missing=True,
        )
        if stderr_status == "failed":
            duration = time.perf_counter() - start
            return CaseResult(case, stderr_status, "stderr", stderr_note, stderr_details, duration)
        if stderr_status == "updated" and stderr_note:
            updated_notes.append(stderr_note)
        duration = time.perf_counter() - start
        if updated_notes:
            return CaseResult(case, "updated", "compare", "; ".join(updated_notes), details=None, duration=duration)
        return CaseResult(case, "passed", "run", "ok", details=None, duration=duration)

    def _compile(self, case: TestCase) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(self.main_py), str(case.source), "-o", str(case.binary_path)]
        for lib in case.config.libs:
            cmd.extend(["-l", lib])
        if self.args.ct_run_main:
            cmd.append("--ct-run-main")
        if self.args.verbose:
            print(f"\n{format_status('CMD', 'blue')} {quote_cmd(cmd)}")
        return subprocess.run(
            cmd,
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self._env_for(case),
        )

    def _run_binary(self, case: TestCase) -> subprocess.CompletedProcess[str]:
        runtime_cmd = [self._runtime_entry(case), *case.runtime_args()]
        runtime_cmd = wrap_runtime_command(runtime_cmd)
        if self.args.verbose:
            print(f"{format_status('CMD', 'blue')} {quote_cmd(runtime_cmd)}")
        proc = subprocess.run(
            runtime_cmd,
            cwd=self.root,
            capture_output=True,
            env=self._env_for(case),
            input=case.stdin_data().encode("utf-8") if case.stdin_data() is not None else None,
        )
        return subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )

    def _runtime_entry(self, case: TestCase) -> str:
        binary = case.binary_path
        try:
            rel = os.path.relpath(binary, start=self.root)
        except ValueError:
            return str(binary)
        if rel.startswith(".."):
            return str(binary)
        if not rel.startswith("./"):
            rel = f"./{rel}"
        return rel

    def _handle_expected_compile_failure(
        self,
        case: TestCase,
        compile_proc: subprocess.CompletedProcess[str],
    ) -> CaseResult:
        duration = 0.0
        if compile_proc.returncode == 0:
            details = self._format_process_output(compile_proc)
            return CaseResult(case, "failed", "compile", "expected compilation to fail", details, duration)
        payload = compile_proc.stderr or compile_proc.stdout
        status, note, details = self._compare_stream(
            case,
            "compile",
            case.compile_expected,
            payload,
            create_on_update=True,
        )
        if status == "failed":
            return CaseResult(case, status, "compile", note, details, duration)
        if status == "updated":
            return CaseResult(case, status, "compile", note, details=None, duration=duration)
        return CaseResult(case, "passed", "compile", "expected failure observed", details=None, duration=duration)

    def _check_compile_output(
        self,
        case: TestCase,
        compile_proc: subprocess.CompletedProcess[str],
    ) -> Tuple[str, str, Optional[str]]:
        if not case.compile_expected.exists() and not self.args.update:
            return "skipped", "", None
        payload = self._collect_compile_output(compile_proc)
        if not payload and not case.compile_expected.exists():
            return "skipped", "", None
        return self._compare_stream(
            case,
            "compile",
            case.compile_expected,
            payload,
            create_on_update=True,
        )

    def _compare_stream(
        self,
        case: TestCase,
        label: str,
        expected_path: Path,
        actual_text: str,
        *,
        create_on_update: bool,
        ignore_when_missing: bool = False,
    ) -> Tuple[str, str, Optional[str]]:
        normalized_actual = normalize_text(actual_text)
        normalized_actual = self._normalize_case_output(case, label, normalized_actual)
        actual_clean = normalized_actual.rstrip("\n")
        if not expected_path.exists():
            if ignore_when_missing:
                return "passed", "", None
            if self.args.update and create_on_update:
                write_text(expected_path, normalized_actual)
                return "updated", f"created {expected_path.name}", None
            details = normalized_actual or None
            return "failed", f"missing expectation {expected_path.name}", details
        expected_text = normalize_text(expected_path.read_text(encoding="utf-8"))
        expected_text = self._normalize_case_output(case, label, expected_text)
        expected_clean = expected_text.rstrip("\n")
        if expected_clean == actual_clean:
            return "passed", "", None
        if self.args.update and create_on_update:
            write_text(expected_path, normalized_actual)
            return "updated", f"updated {expected_path.name}", None
        diff = diff_text(expected_text, normalized_actual, label)
        if not diff:
            diff = f"expected:\n{expected_text}\nactual:\n{normalized_actual}"
        return "failed", f"{label} mismatch", diff

    def _collect_compile_output(self, proc: subprocess.CompletedProcess[str]) -> str:
        parts: List[str] = []
        if proc.stdout:
            parts.append(proc.stdout)
        if proc.stderr:
            if parts and not parts[-1].endswith("\n"):
                parts.append("\n")
            parts.append(proc.stderr)
        return "".join(parts)

    def _compare_nob_test_stdout(
        self,
        case: TestCase,
        actual_text: str,
    ) -> Tuple[str, str, Optional[str]]:
        proc = subprocess.run(
            ["ls"],
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self._env_for(case),
        )
        if proc.returncode != 0:
            details = self._format_process_output(proc)
            return "failed", f"ls exited {proc.returncode}", details
        expected_sorted = self._sort_lines(normalize_text(proc.stdout))
        actual_sorted = self._sort_lines(normalize_text(actual_text))
        if expected_sorted.rstrip("\n") == actual_sorted.rstrip("\n"):
            return "passed", "", None
        diff = diff_text(expected_sorted, actual_sorted, "stdout")
        if not diff:
            diff = f"expected:\n{expected_sorted}\nactual:\n{actual_sorted}"
        return "failed", "stdout mismatch", diff

    def _normalize_case_output(self, case: TestCase, label: str, text: str) -> str:
        if case.source.stem == "nob_test" and label == "stdout":
            return self._sort_lines(text)
        if case.source.stem == "ct_test" and label == "compile":
            return self._mask_build_path(text, case.binary_stub)
        return text

    def _sort_lines(self, text: str) -> str:
        lines = text.splitlines()
        if not lines:
            return text
        suffix = "\n" if text.endswith("\n") else ""
        return "\n".join(sorted(lines)) + suffix

    def _mask_build_path(self, text: str, binary_stub: str) -> str:
        build_dir = str(self.build_dir.resolve())
        masked = text.replace(build_dir, "<build>")
        pattern = rf"/\S*/build/{re.escape(binary_stub)}"
        return re.sub(pattern, f"<build>/{binary_stub}", masked)

    def _env_for(self, case: TestCase) -> Dict[str, str]:
        env = dict(self.base_env)
        env.update(case.config.env)
        return env

    def _module_available(self, module: str) -> bool:
        if module not in self._module_cache:
            self._module_cache[module] = importlib.util.find_spec(module) is not None
        return self._module_cache[module]

    def _format_process_output(self, proc: subprocess.CompletedProcess[str]) -> str:
        parts = []
        if proc.stdout:
            parts.append("stdout:\n" + proc.stdout.strip())
        if proc.stderr:
            parts.append("stderr:\n" + proc.stderr.strip())
        return "\n\n".join(parts) if parts else "(no output)"

    def _print_result(self, result: CaseResult) -> None:
        tag_color = {
            "passed": (" OK ", "green"),
            "updated": ("UPD", "blue"),
            "failed": ("ERR", "red"),
            "skipped": ("SKIP", "yellow"),
        }
        label, color = tag_color.get(result.status, ("???", "red"))
        prefix = format_status(label, color)
        if result.status == "failed" and result.details:
            message = f"{result.case.name} ({result.stage}) {result.message}"
        elif result.message:
            message = f"{result.case.name} {result.message}"
        else:
            message = result.case.name
        print(f"{prefix} {message}")
        if result.status == "failed" and result.details:
            print(textwrap.indent(result.details, "    "))

    def _print_summary(self, results: Sequence[CaseResult]) -> None:
        total = len(results)
        passed = sum(1 for r in results if r.status == "passed")
        updated = sum(1 for r in results if r.status == "updated")
        skipped = sum(1 for r in results if r.status == "skipped")
        failed = sum(1 for r in results if r.status == "failed")
        print()
        print(f"Total: {total}, passed: {passed}, updated: {updated}, skipped: {skipped}, failed: {failed}")
        if failed:
            print("\nFailures:")
            for result in results:
                if result.status != "failed":
                    continue
                print(f"- {result.case.name} ({result.stage}) {result.message}")
                if result.details:
                    print(textwrap.indent(result.details, "    "))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run L2 compiler tests")
    parser.add_argument("patterns", nargs="*", help="glob or substring filters for test names")
    parser.add_argument("--tests-dir", default="tests", help="directory containing .sl test files")
    parser.add_argument("--build-dir", default="build", help="directory for compiled binaries")
    parser.add_argument("--extra", action="append", help="additional .sl files or directories to treat as tests")
    parser.add_argument("--list", action="store_true", help="list tests and exit")
    parser.add_argument("--update", action="store_true", help="update expectation files with actual output")
    parser.add_argument("--stop-on-fail", action="store_true", help="stop after the first failure")
    parser.add_argument("--ct-run-main", action="store_true", help="execute each test's 'main' via the compile-time VM during compilation")
    parser.add_argument("-v", "--verbose", action="store_true", help="show compiler/runtime commands")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    ensure_arm_runtime_support()
    runner = TestRunner(Path(__file__).resolve().parent, args)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())

