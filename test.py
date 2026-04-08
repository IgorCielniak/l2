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
    "extra_tests/c_extern_structs.sl",
    "extra_tests/fn_test.sl",
    "extra_tests/nob_test.sl",
    "extra_tests/termios_test.sl",
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
    compile_args: List[str] = field(default_factory=list)
    run_example: bool = False
    smoke_run: bool = False
    stdout_regex: Optional[str] = None
    stderr_regex: Optional[str] = None
    runtime_timeout: Optional[float] = None
    use_l2eval: bool = False

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
        if "compile_args" in data:
            ca = data["compile_args"]
            if not isinstance(ca, list) or not all(isinstance(item, str) for item in ca):
                raise ValueError("compile_args must be a list of strings")
            cfg.compile_args = list(ca)
        if "run_example" in data:
            cfg.run_example = bool(data["run_example"])
        if "smoke_run" in data:
            cfg.smoke_run = bool(data["smoke_run"])
        if "stdout_regex" in data:
            val = data["stdout_regex"]
            if not isinstance(val, str):
                raise ValueError("stdout_regex must be a string")
            re.compile(val)
            cfg.stdout_regex = val
        if "stderr_regex" in data:
            val = data["stderr_regex"]
            if not isinstance(val, str):
                raise ValueError("stderr_regex must be a string")
            re.compile(val)
            cfg.stderr_regex = val
        if "runtime_timeout" in data:
            timeout = float(data["runtime_timeout"])
            if timeout <= 0:
                raise ValueError("runtime_timeout must be > 0")
            cfg.runtime_timeout = timeout
        if "use_l2eval" in data:
            cfg.use_l2eval = bool(data["use_l2eval"])
        return cfg


@dataclass
class TestCase:
    name: str
    source: Path
    binary_stub: str
    expected_stdout: Path
    expected_stderr: Path
    compile_expected: Path
    asm_forbid: Path
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
        self.examples_dir = resolve_path(root, args.examples_dir)
        self.build_dir = resolve_path(root, args.build_dir)
        self.build_dir.mkdir(parents=True, exist_ok=True)
        self.main_py = self.root / "main.py"
        self.l2eval_builder = self.root / "tools" / "build_l2eval_lib.sh"
        self.base_env = os.environ.copy()
        self._module_cache: Dict[str, bool] = {}
        self.run_example_patterns = list(args.run_example or [])
        self._l2eval_ready = False
        extra_entries = list(DEFAULT_EXTRA_TESTS)
        if args.extra:
            extra_entries.extend(args.extra)
        self.extra_sources = [resolve_path(self.root, entry) for entry in extra_entries]
        self.cases = self._discover_cases()

    def _discover_cases(self) -> List[TestCase]:
        sources: List[Path] = []
        if self.tests_dir.exists():
            sources.extend(sorted(self.tests_dir.glob("*.sl")))
        if not self.args.no_compile_examples and self.examples_dir.exists():
            sources.extend(sorted(self.examples_dir.rglob("*.sl")))
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
        if self._is_example_source(source):
            should_run_example = self._should_run_example(relative, config)
            if not should_run_example:
                config.compile_only = True
                if "--check" not in config.compile_args:
                    config.compile_args.append("--check")
            elif (
                not source.with_suffix(".expected").exists()
                and not source.with_suffix(".stderr").exists()
                and config.stdout_regex is None
                and config.stderr_regex is None
            ):
                # Allow opt-in runnable examples without fixture files.
                config.smoke_run = True
        return TestCase(
            name=relative,
            source=source,
            binary_stub=source.stem,
            expected_stdout=source.with_suffix(".expected"),
            expected_stderr=source.with_suffix(".stderr"),
            compile_expected=source.with_suffix(".compile.expected"),
            asm_forbid=source.with_suffix(".asm.forbid"),
            stdin_path=source.with_suffix(".stdin"),
            args_path=source.with_suffix(".args"),
            meta_path=meta_path,
            build_dir=self.build_dir,
            config=config,
        )

    def _is_example_source(self, source: Path) -> bool:
        try:
            source.relative_to(self.examples_dir)
        except ValueError:
            return False
        return True

    def _should_run_example(self, case_name: str, config: TestCaseConfig) -> bool:
        if config.run_example:
            return True
        return bool(self.run_example_patterns and match_patterns(case_name, self.run_example_patterns))

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
        try:
            fixture_libs = self._prepare_case_native_libs(case)
        except RuntimeError as exc:
            return CaseResult(case, "failed", "fixture", str(exc))
        compile_proc = self._compile(case, extra_libs=fixture_libs)
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
        asm_status, asm_note, asm_details = self._check_asm_forbidden_patterns(case)
        if asm_status == "failed":
            duration = time.perf_counter() - start
            return CaseResult(case, asm_status, "asm", asm_note, asm_details, duration)
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

        if case.config.stdout_regex is not None:
            status, note, details = self._check_regex_stream("stdout", case.config.stdout_regex, run_proc.stdout)
        elif case.source.stem == "nob_test":
            status, note, details = self._compare_nob_test_stdout(case, run_proc.stdout)
        elif case.config.smoke_run and not case.expected_stdout.exists():
            status, note, details = "passed", "", None
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

        if case.config.stderr_regex is not None:
            stderr_status, stderr_note, stderr_details = self._check_regex_stream("stderr", case.config.stderr_regex, run_proc.stderr)
        elif case.config.smoke_run and not case.expected_stderr.exists():
            if run_proc.stderr.strip():
                stderr_status = "failed"
                stderr_note = "unexpected stderr output during smoke run"
                stderr_details = run_proc.stderr
            else:
                stderr_status, stderr_note, stderr_details = "passed", "", None
        else:
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
        message = "smoke-run ok" if case.config.smoke_run else "ok"
        return CaseResult(case, "passed", "run", message, details=None, duration=duration)

    def _compile(self, case: TestCase, *, extra_libs: Optional[Sequence[str]] = None) -> subprocess.CompletedProcess[str]:
        cmd = [
            sys.executable,
            str(self.main_py),
            str(case.source),
            "-o",
            str(case.binary_path),
            "--no-cache",
        ]
        for lib in case.config.libs:
            cmd.extend(["-l", lib])
        for lib in (extra_libs or []):
            cmd.extend(["-l", lib])
        cmd.extend(case.config.compile_args)
        if self.args.ct_run_main and not self._is_example_source(case.source):
            cmd.append("--ct-run-main")
        if self.args.verbose:
            print(f"\n{format_status('CMD', 'blue')} {quote_cmd(cmd)}")
        # When --ct-run-main is used, the compiler executes main at compile time,
        # so it may need stdin data that would normally go to the binary.
        compile_input = None
        stdin_data = case.stdin_data()
        if self.args.ct_run_main and not self._is_example_source(case.source) and stdin_data is not None:
            compile_input = stdin_data
        return subprocess.run(
            cmd,
            cwd=self.root,
            capture_output=True,
            text=True,
            input=compile_input,
            env=self._env_for(case),
        )

    def _prepare_case_native_libs(self, case: TestCase) -> List[str]:
        """Build optional per-test native C fixtures and return linkable artifacts."""
        if case.config.use_l2eval:
            return self._prepare_l2eval_libs(case)

        c_source = case.source.with_suffix(".c")
        if not c_source.exists():
            return []

        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if cc is None:
            raise RuntimeError("no C compiler found (expected one of: cc, gcc, clang)")

        ar = shutil.which("ar")
        if ar is None:
            raise RuntimeError("'ar' is required to build static C fixtures")

        obj_path = case.build_dir / f"{case.binary_stub}_fixture.o"
        archive_path = case.build_dir / f"lib{case.binary_stub}_fixture.a"

        compile_cmd = [cc, "-O2", "-fno-stack-protector", "-c", str(c_source), "-o", str(obj_path)]
        archive_cmd = [ar, "rcs", str(archive_path), str(obj_path)]

        if self.args.verbose:
            print(f"{format_status('CMD', 'blue')} {quote_cmd(compile_cmd)}")
            print(f"{format_status('CMD', 'blue')} {quote_cmd(archive_cmd)}")

        compile_proc = subprocess.run(
            compile_cmd,
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self._env_for(case),
        )
        if compile_proc.returncode != 0:
            raise RuntimeError(
                "failed to compile C fixture:\n" + self._format_process_output(compile_proc)
            )

        archive_proc = subprocess.run(
            archive_cmd,
            cwd=self.root,
            capture_output=True,
            text=True,
            env=self._env_for(case),
        )
        if archive_proc.returncode != 0:
            raise RuntimeError(
                "failed to archive C fixture:\n" + self._format_process_output(archive_proc)
            )

        return [str(archive_path.resolve())]

    def _prepare_l2eval_libs(self, case: TestCase) -> List[str]:
        lib_path = (self.root / "build" / "libl2eval.a").resolve()

        if not self._l2eval_ready:
            if not lib_path.exists():
                if not self.l2eval_builder.exists():
                    raise RuntimeError(f"missing l2eval builder script: {self.l2eval_builder}")

                cmd = ["bash", str(self.l2eval_builder)]
                if self.args.verbose:
                    print(f"{format_status('CMD', 'blue')} {quote_cmd(cmd)}")

                proc = subprocess.run(
                    cmd,
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    env=self._env_for(case),
                )
                if proc.returncode != 0:
                    raise RuntimeError("failed to build l2eval library:\n" + self._format_process_output(proc))

            self._l2eval_ready = True

        if not lib_path.exists():
            raise RuntimeError(f"l2eval library missing after build: {lib_path}")
        return [str(lib_path), "c"]

    def _run_binary(self, case: TestCase) -> subprocess.CompletedProcess[str]:
        runtime_cmd = [self._runtime_entry(case), *case.runtime_args()]
        runtime_cmd = wrap_runtime_command(runtime_cmd)
        if self.args.verbose:
            print(f"{format_status('CMD', 'blue')} {quote_cmd(runtime_cmd)}")
        stdin_data = case.stdin_data()
        timeout = case.config.runtime_timeout if case.config.runtime_timeout is not None else self.args.runtime_timeout
        try:
            proc = subprocess.run(
                runtime_cmd,
                cwd=self.root,
                capture_output=True,
                env=self._env_for(case),
                input=stdin_data.encode("utf-8") if stdin_data is not None else None,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_text: str
            stderr_text: str
            raw_stdout = exc.stdout
            raw_stderr = exc.stderr
            if isinstance(raw_stdout, bytes):
                stdout_text = raw_stdout.decode("utf-8", errors="replace")
            else:
                stdout_text = raw_stdout or ""
            if isinstance(raw_stderr, bytes):
                stderr_text = raw_stderr.decode("utf-8", errors="replace")
            else:
                stderr_text = raw_stderr or ""
            if stderr_text and not stderr_text.endswith("\n"):
                stderr_text += "\n"
            if timeout is not None:
                stderr_text += f"<timed out after {timeout}s>"
            else:
                stderr_text += "<timed out>"
            return subprocess.CompletedProcess(
                args=runtime_cmd,
                returncode=124,
                stdout=stdout_text,
                stderr=stderr_text,
            )
        return subprocess.CompletedProcess(
            args=proc.args,
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors="replace"),
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )

    def _check_regex_stream(self, label: str, pattern: str, actual_text: str) -> Tuple[str, str, Optional[str]]:
        normalized = normalize_text(actual_text)
        if re.search(pattern, normalized, re.MULTILINE):
            return "passed", "", None
        details = textwrap.dedent(
            f"""\
            pattern: {pattern!r}
            actual {label}:
            {normalized if normalized else '(empty)'}
            """
        ).rstrip()
        return "failed", f"{label} regex mismatch", details

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
        if (
            label == "compile"
            and self.args.ct_run_main
            and expected_clean
            and actual_clean.endswith(expected_clean)
        ):
            # --ct-run-main may prepend program output to compile stdout.
            # Treat expected compile text as authoritative suffix.
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

    def _check_asm_forbidden_patterns(self, case: TestCase) -> Tuple[str, str, Optional[str]]:
        """Fail test if generated asm contains forbidden markers listed in *.asm.forbid."""
        if not case.asm_forbid.exists():
            return "passed", "", None

        asm_path = case.build_dir / f"{case.binary_stub}.asm"
        if not asm_path.exists():
            return "failed", f"missing generated asm file {asm_path.name}", None

        asm_text = asm_path.read_text(encoding="utf-8")
        patterns: List[str] = []
        for raw in case.asm_forbid.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)

        hits: List[str] = []
        for pattern in patterns:
            if pattern.startswith("re:"):
                expr = pattern[3:]
                if re.search(expr, asm_text, re.MULTILINE):
                    hits.append(pattern)
                continue
            if pattern in asm_text:
                hits.append(pattern)

        if not hits:
            return "passed", "", None

        detail = "forbidden asm pattern(s) matched:\n" + "\n".join(f"- {p}" for p in hits)
        return "failed", "assembly contains forbidden patterns", detail

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
        if label == "compile":
            # Normalize absolute source paths to relative for stable compile error comparison
            source_dir = str(case.source.parent.resolve())
            if source_dir:
                text = text.replace(source_dir + "/", "")
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
    parser.add_argument("--examples-dir", default="examples", help="directory containing .sl examples to compile-check")
    parser.add_argument("--no-compile-examples", action="store_true", help="skip compile-only checks for examples")
    parser.add_argument("--run-example", action="append", default=[], help="pattern for example cases to run instead of compile-only (repeatable)")
    parser.add_argument("--build-dir", default="build", help="directory for compiled binaries")
    parser.add_argument("--extra", action="append", help="additional .sl files or directories to treat as tests")
    parser.add_argument("--list", action="store_true", help="list tests and exit")
    parser.add_argument("--update", action="store_true", help="update expectation files with actual output")
    parser.add_argument("--stop-on-fail", action="store_true", help="stop after the first failure")
    parser.add_argument("--ct-run-main", action="store_true", help="execute each test's 'main' via the compile-time VM during compilation")
    parser.add_argument("--runtime-timeout", type=float, default=None, help="timeout in seconds for runtime execution")
    parser.add_argument("-v", "--verbose", action="store_true", help="show compiler/runtime commands")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    ensure_arm_runtime_support()
    runner = TestRunner(Path(__file__).resolve().parent, args)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())

