#!/usr/bin/env python3
"""Contract tests for Diagnostics: the code registry, producer
classification (every compiler error names a registered code), immutable
Diagnostic values, the diagnostic-* and ct-diagnostic-* words, and history."""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import Diagnostic, ParseError, SourceOrigin

EXCEPTIONS = {"ParseError", "CompileError", "CompileTimeError"}
CODE_TAKERS = EXCEPTIONS | {"_raise_compile_error", "_record_diagnostic", "Diagnostic", "_lexer_error"}


def _string_constants(node: ast.AST):
    """String constants in an expression, skipping f-strings (computed
    codes such as f"warning-{category}")."""
    if isinstance(node, ast.JoinedStr):
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        yield node.value
    for child in ast.iter_child_nodes(node):
        yield from _string_constants(child)


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


class ProducerClassificationTests(unittest.TestCase):
    """Static checks over l2_main.py: no error producer without a code."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse((ROOT / "l2_main.py").read_text(encoding="utf-8"))

    def test_every_raise_names_a_code(self) -> None:
        missing = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and _call_name(node.exc) in EXCEPTIONS:
                keywords = {k.arg for k in node.exc.keywords}
                if not keywords & {"code", "diagnostic"}:
                    missing.append(node.lineno)
        self.assertEqual(missing, [], "raise sites without code= or diagnostic=")

    def test_code_literals_are_registered(self) -> None:
        known = set(l2_main._DIAGNOSTIC_CODES) | set(l2_main._WARNING_CODES)
        unknown = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in CODE_TAKERS:
                continue
            values = [k.value for k in node.keywords if k.arg == "code"]
            if _call_name(node) == "_lexer_error" and node.args:
                values.append(node.args[0])
            for value in values:
                for const in _string_constants(value):
                    if const not in known:
                        unknown.append((node.lineno, const))
        self.assertEqual(unknown, [])

    def test_raise_compile_error_and_record_diagnostic_require_codes(self) -> None:
        missing = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and _call_name(node) in {"_raise_compile_error", "_record_diagnostic"}:
                if "code" not in {k.arg for k in node.keywords}:
                    missing.append(node.lineno)
        self.assertEqual(missing, [])

    def test_no_message_based_classification(self) -> None:
        source = (ROOT / "l2_main.py").read_text(encoding="utf-8")
        for name in ("_parse_error_heuristics", "_compile_error_heuristics"):
            self.assertNotIn(name, source)
        self.assertNotRegex(source, r'code="E\d{4}"')


class RegistryTests(unittest.TestCase):
    def test_categories_and_phases(self) -> None:
        for code, (category, phase) in l2_main._DIAGNOSTIC_CODES.items():
            self.assertRegex(code, r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", code)
            self.assertRegex(category, r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$", code)
            self.assertTrue(phase, code)

    def test_contract_codes(self) -> None:
        expected = {
            "ct-diagnostic-raised": "ct_diagnostic",
            "static-assertion-failed": "ct_execution_error",
            "recovery-operation-unavailable": "recovery_error",
            "recovery-restore-point-invalid": "recovery_error",
            "event-handler-failed": "recovery_error",
            "event-stream-output-failed": "event_stream_error",
            "source-cursor-eof": "reader_error",
            "frontend-header-invalid": "frontend_error",
            "lexer-unterminated-string": "lexer_error",
            "import-unresolved": "import_error",
        }
        for code, category in expected.items():
            self.assertEqual(l2_main.diagnostic_category(code), category, code)


class DiagnosticValueTests(unittest.TestCase):
    def test_immutable_with_derived_category_and_phase(self) -> None:
        origin = SourceOrigin("/a.sl", 3, 7, 1, 4, 1, 8)
        diag = Diagnostic("error", "bad", code="import-unresolved", primary=origin)
        self.assertEqual((diag.category, diag.phase), ("import_error", "import"))
        self.assertEqual((diag.path, diag.line, diag.column, diag.length), (Path("/a.sl"), 1, 4, 4))
        with self.assertRaises(AttributeError):
            diag.code = "x"  # type: ignore[misc]

    def test_errors_carry_their_code(self) -> None:
        exc = ParseError("x", code="parser-unexpected-eof")
        self.assertEqual(exc.code, "parser-unexpected-eof")
        self.assertEqual(exc.diagnostic.category, "parser_error")


class DiagnosticWordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = l2_main.Compiler()
        self.parser = self.compiler.parser
        self.vm = self.parser.compile_time_vm
        self.origin = SourceOrigin("/src/x.sl", 0, 3, 1, 1, 1, 4)

    def run_word(self, name: str, *args):
        word = self.compiler.dictionary.lookup(name)
        self.assertIsNotNone(word, name)
        self.vm.stack = list(args)
        word.compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def new(self, *args) -> Diagnostic:
        return self.run_word("diagnostic-new", *args)[0]

    def assertCode(self, code: str, name: str, *args) -> None:
        with self.assertRaises(ParseError) as caught:
            self.run_word(name, *args)
        self.assertEqual(caught.exception.code, code)

    def test_new_and_accessors(self) -> None:
        related = [{"kind": "note", "message": "here", "origin": self.origin}, {"kind": "chain", "origin": self.origin}]
        diag = self.new("error", "my-code", "my_lang_error", "my-phase", "boom", self.origin, related)
        self.assertIsInstance(diag, Diagnostic)
        for word, value in (("diagnostic-severity", "error"), ("diagnostic-code", "my-code"),
                            ("diagnostic-category", "my_lang_error"), ("diagnostic-phase", "my-phase"),
                            ("diagnostic-message", "boom"), ("diagnostic-primary", self.origin)):
            self.assertEqual(self.run_word(word, diag), [value], word)
        maps = self.run_word("diagnostic-related", diag)[0]
        self.assertEqual(maps, related)
        maps[0]["kind"] = "changed"
        self.assertEqual(self.run_word("diagnostic-related", diag)[0], related)

    def test_warning_and_note_take_nil_category(self) -> None:
        for severity in ("warning", "note"):
            diag = self.new(severity, "c", None, None, "m", self.origin, [])
            self.assertEqual((diag.severity, diag.category, diag.phase), (severity, None, None))

    def test_new_validation(self) -> None:
        good = ["error", "c", "cat", None, "m", self.origin, []]
        bad = [
            (0, "fatal"), (1, ""), (1, 7), (2, None), (2, "Bad-Cat"), (2, "bad__cat"), (3, ""), (3, 1),
            (4, None), (5, "origin"), (6, "x"), (6, [{"origin": self.origin}]),
            (6, [{"kind": "k", "origin": self.origin, "extra": 1}]), (6, [{"kind": "k"}]),
            (6, [{"kind": "k", "message": 3, "origin": self.origin}]), (6, ["record"]),
        ]
        for index, value in bad:
            args = list(good)
            args[index] = value
            with self.assertRaises(ParseError, msg=f"{index}={value!r}"):
                self.run_word("diagnostic-new", *args)
        with self.assertRaises(ParseError):
            self.run_word("diagnostic-new", "warning", "c", "cat", None, "m", self.origin, [])

    def test_accessors_are_strict(self) -> None:
        for word in ("diagnostic-code", "diagnostic-related", "ct-diagnostic-raise"):
            self.assertCode("ct-argument-type", word, "not a diagnostic")

    def test_raise(self) -> None:
        diag = self.new("error", "my-code", "my_lang_error", "parse", "boom", self.origin, [])
        with self.assertRaises(ParseError) as caught:
            self.run_word("ct-diagnostic-raise", diag)
        self.assertIs(caught.exception.diagnostic, diag)
        warning = self.new("warning", "w", None, None, "m", self.origin, [])
        self.assertCode("ct-argument-invalid", "ct-diagnostic-raise", warning)

    def test_error_warning_note_words(self) -> None:
        with self.assertRaises(ParseError) as caught:
            self.run_word("ct-diagnostic-error", "stop")
        diag = caught.exception.diagnostic
        self.assertEqual((diag.code, diag.category, diag.phase, diag.message),
                         ("ct-diagnostic-raised", "ct_diagnostic", "ct-diagnostic", "stop"))
        self.run_word("ct-diagnostic-warning", "careful")
        self.run_word("ct-diagnostic-note", "fyi")
        history = self.run_word("ct-diagnostic-list")[0]
        self.assertEqual([(d["severity"], d["code"], d["category"], d["phase"], d["message"]) for d in history], [
            ("warning", "ct-warning-raised", None, "ct-diagnostic", "careful"),
            ("note", "ct-note-raised", None, "ct-diagnostic", "fyi"),
        ])
        self.assertEqual(set(history[0]), {"severity", "code", "category", "phase", "message", "primary", "related"})
        self.assertCode("ct-argument-type", "ct-diagnostic-warning", 5)


class EndToEndTests(unittest.TestCase):
    def compile(self, source: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="l2-diag-") as raw:
            path = Path(raw) / "d.sl"
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact", "--no-daemon"],
                cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
            )

    def test_ct_error_reports_call_site(self) -> None:
        proc = self.compile("""
            import stdlib.sl
            word check "bad config" ct-diagnostic-error end
            compile-time check
            word main 0 end
        """)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error[ct-diagnostic-raised]: bad config", proc.stderr)
        self.assertIn("d.sl:2:", proc.stderr)

    def test_ct_warning_does_not_fail(self) -> None:
        proc = self.compile("""
            import stdlib.sl
            word check "heads up" ct-diagnostic-warning end
            compile-time check
            word main 0 end
        """)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_producer_codes(self) -> None:
        cases = {
            "import stdlib.sl\nword main nosuchword 0 end\n": "emission-unknown-word",
            "import stdlib.sl\nimport nosuchfile.sl\nword main 0 end\n": "import-unresolved",
            "import stdlib.sl\nword f 0 static-assert end\ncompile-time f\nword main 0 end\n":
                "static-assertion-failed",
            'import stdlib.sl\nword main "abc 0 end\n': "lexer-unterminated-string",
            "import stdlib.sl\nword main 0\n": "parser-definition-unclosed",
        }
        for source, code in cases.items():
            with self.subTest(code=code):
                proc = self.compile(source)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(f"error[{code}]", proc.stderr)


if __name__ == "__main__":
    unittest.main()
