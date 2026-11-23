#!/usr/bin/env python3
"""Regression tests for confirmed CT/compiler audit defects."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main


class CTAuditRegressionTests(unittest.TestCase):
    def test_import_resolution_cache_is_compiler_and_search_path_specific(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "lib.sl").write_text("first\n", encoding="utf-8")
            (second / "lib.sl").write_text("second\n", encoding="utf-8")
            importer = root / "main.sl"

            compiler_a = l2_main.Compiler(include_paths=[first])
            compiler_b = l2_main.Compiler(include_paths=[second])

            self.assertEqual(compiler_a._resolve_import_target(importer, "lib.sl"), first / "lib.sl")
            self.assertEqual(compiler_b._resolve_import_target(importer, "lib.sl"), second / "lib.sl")

    def test_import_resolution_cache_discards_deleted_target(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            first_target = first / "lib.sl"
            second_target = second / "lib.sl"
            first_target.write_text("first\n", encoding="utf-8")
            second_target.write_text("second\n", encoding="utf-8")
            compiler = l2_main.Compiler(include_paths=[first, second])

            self.assertEqual(compiler._resolve_import_target(root / "main.sl", "lib.sl"), first_target)
            first_target.unlink()
            self.assertEqual(compiler._resolve_import_target(root / "main.sl", "lib.sl"), second_target)

    def test_import_event_depth_tracks_recursion_not_sibling_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "main.sl").write_text('import "left.sl"\nimport "right.sl"\n', encoding="utf-8")
            (root / "left.sl").write_text('import "nested.sl"\n', encoding="utf-8")
            (root / "nested.sl").write_text("word nested 1 end\n", encoding="utf-8")
            (root / "right.sl").write_text("word right 2 end\n", encoding="utf-8")

            bus = l2_main.EventBus()
            events = []
            bus.subscribe("import.begin", events.append)
            compiler = l2_main.Compiler(include_paths=[root], event_bus=bus)
            compiler._load_with_imports(root / "main.sl")

            depths = {
                Path(event["payload"]["path"]).name: event["payload"]["depth"]
                for event in events
            }
            self.assertEqual(depths, {"main.sl": 0, "left.sl": 1, "nested.sl": 2, "right.sl": 1})

    def test_runtime_only_word_remains_callable_in_runtime_vm_mode(self) -> None:
        compiler = l2_main.Compiler()
        vm = compiler.parser.compile_time_vm
        word = l2_main.Word(
            "runtime-only-test",
            runtime_only=True,
            runtime_intrinsic=lambda _runtime_vm: None,
        )

        with self.assertRaisesRegex(l2_main.CompileTimeError, "runtime-only"):
            vm._call_word(word)

        vm.runtime_mode = True
        try:
            vm._call_word(word)
        finally:
            vm.runtime_mode = False

    def test_failed_error_definition_does_not_register_partial_words(self) -> None:
        parser = l2_main.Compiler().parser
        vm = parser.compile_time_vm
        parser._active_module = l2_main.Module([])
        vm.push(
            "word recovery_partial 42 end\n"
            "macro recovery_macro (x) $x ;\n"
            "word recovery_broken"
        )
        try:
            with self.assertRaises(l2_main.ParseError):
                l2_main._ct_definition_register_source(vm)
        finally:
            parser._active_module = None

        self.assertIsNone(parser.dictionary.lookup("recovery_partial"))
        self.assertIsNone(parser.dictionary.lookup("recovery_macro"))

    def test_language_pack_cleanup_preserves_later_same_named_resources(self) -> None:
        parser = l2_main.Compiler().parser
        parser.lang_create("pack-a")
        parser.lang_create("pack-b")
        parser.lang_set_active("pack-b", True, "ct-lang-activate")

        def define_macro(value: str) -> l2_main.Word:
            word = l2_main.Word(name="shared-macro")
            body = l2_main.Definition(name="shared-macro", body=[], terminator=";")
            word.macro_def = l2_main._MacroDef((), body, l2_main.Syntax([]), {"value": value}, None)
            parser.dictionary.register(word)
            return word

        old_macro = define_macro("1")
        self.assertTrue(parser.lang_adopt("pack-a", "shared-macro", True))
        new_macro = define_macro("2")
        self.assertTrue(parser.lang_adopt("pack-b", "shared-macro", True))
        parser.lang_add_rewrite("pack-a", "reader", "shared-rewrite", ["before"], ["old"])
        parser.lang_add_rewrite("pack-b", "reader", "shared-rewrite", ["before"], ["new"])

        parser.lang_remove("pack-a")

        # Only pack-a's own candidate goes; pack-b's later one stays selected.
        self.assertIsNone(parser.dictionary.resolve(old_macro.handle))
        self.assertIs(parser.dictionary.lookup("shared-macro"), new_macro)
        rewrites = [rule for rule in parser.rewrites.rules["reader"] if rule.name == "shared-rewrite"]
        self.assertEqual([list(rule.replacement) for rule in rewrites], [["new"]])

    def test_recoverable_error_payload_uses_imported_source_location(self) -> None:
        bus = l2_main.EventBus()
        events = []
        bus.subscribe("compiler.error", events.append)
        parser = l2_main.Compiler(event_bus=bus).parser
        origin = l2_main.SourceOrigin("imported.sl", 40, 43, 12, 5, 12, 8)
        token = l2_main.Token("bad", 7, 4, 0, 3, origin=origin)
        parser._last_token = token
        restore_point = parser._add_error_restore_point("parser", 7, token, None)

        parser._dispatch_recoverable_compile_error(
            l2_main.ParseError("recoverable failure", code="parser-directive-invalid"),
            stage="parser",
            restore_point_id=restore_point,
        )

        diagnostic = events[0]["payload"]["diagnostic"]
        self.assertIs(diagnostic.primary, origin)
        self.assertEqual((str(diagnostic.path), diagnostic.line, diagnostic.column), ("imported.sl", 12, 5))

    def test_independent_errors_start_their_own_recovery_chains(self) -> None:
        bus = l2_main.EventBus()
        events = []
        bus.subscribe("compiler.error", events.append)
        parser = l2_main.Compiler(event_bus=bus).parser
        token = l2_main.Token("bad", 7, 4, 0, 3)

        for _ in range(2):
            restore_point = parser._add_error_restore_point("parser", 7, token, None)
            parser._dispatch_recoverable_compile_error(
                l2_main.ParseError("same failure", code="parser-directive-invalid"),
                stage="parser",
                restore_point_id=restore_point,
            )

        self.assertEqual([event["payload"]["recovery"]["attempt"] for event in events], [1, 1])

if __name__ == "__main__":
    unittest.main(verbosity=2)
