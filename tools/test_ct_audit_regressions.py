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
        parser._active_error_recovery = {}
        parser._active_module = l2_main.Module([])
        vm.push(
            "word recovery_partial 42 end\n"
            "macro recovery_macro (x) $x ;\n"
            "word recovery_broken"
        )
        try:
            with self.assertRaises(l2_main.ParseError):
                l2_main._ct_error_define(vm)
        finally:
            parser._active_error_recovery = None
            parser._active_module = None

        self.assertIsNone(parser.dictionary.lookup("recovery_partial"))
        self.assertIsNone(parser.dictionary.lookup("recovery_macro"))
        self.assertNotIn("recovery_macro", parser._macro_signatures)

    def test_language_pack_cleanup_preserves_later_same_named_resources(self) -> None:
        parser = l2_main.Compiler().parser
        parser.ct_lang_create("pack-a")
        parser.ct_lang_create("pack-b")

        parser.ct_lang_register_text_macro_signature("pack-a", "shared-text", [], ["1"])
        parser.ct_lang_register_text_macro_signature("pack-b", "shared-text", [], ["2"])
        parser.ct_lang_add_rewrite_rule(
            "pack-a", "reader", ["before"], ["old"], rule_name="shared-rewrite"
        )
        parser.ct_lang_add_rewrite_rule(
            "pack-b", "reader", ["before"], ["new"], rule_name="shared-rewrite"
        )
        parser.ct_lang_register_pattern_macro("pack-a", "shared-pattern", [[["x"], ["old"]]])
        parser.ct_lang_register_pattern_macro("pack-b", "shared-pattern", [[["x"], ["new"]]])

        parser.ct_lang_remove("pack-a")

        text_macro = parser.dictionary.lookup("shared-text")
        self.assertIsNotNone(text_macro)
        self.assertEqual(text_macro.macro_expansion, ["2"])
        rewrite = next(
            rule for rule in parser.reader_rewrite_rules if rule.name == "shared-rewrite"
        )
        self.assertEqual(list(rewrite.replacement), ["new"])
        pattern_rule_name = parser._pattern_macro_rule_names("shared-pattern")[0]
        pattern_rule = next(
            rule for rule in parser.grammar_rewrite_rules if rule.name == pattern_rule_name
        )
        self.assertEqual(list(pattern_rule.replacement), ["new"])

    def test_recoverable_error_payload_uses_imported_source_location(self) -> None:
        bus = l2_main.EventBus()
        events = []
        bus.subscribe("compile.error", events.append)
        parser = l2_main.Compiler(event_bus=bus).parser
        parser.file_spans = [l2_main.FileSpan(Path("imported.sl"), 7, 8, 12)]
        token = l2_main.Token("bad", 7, 4, 0, 3)
        restore_point = parser._add_error_restore_point("parser", 7, token, None)

        parser._dispatch_recoverable_compile_error(
            l2_main.ParseError("recoverable failure"),
            stage="parser",
            restore_point_id=restore_point,
        )

        payload = events[0]["payload"]
        self.assertEqual(payload["path"], "imported.sl")
        self.assertEqual(payload["line"], 12)
        self.assertEqual(payload["column"], 4)

    def test_recovery_attempts_are_counted_per_source_file(self) -> None:
        bus = l2_main.EventBus()
        events = []
        bus.subscribe("compile.error", events.append)
        parser = l2_main.Compiler(event_bus=bus).parser
        token = l2_main.Token("bad", 7, 4, 0, 3)

        for source_path in ("first.sl", "second.sl"):
            parser.file_spans = [l2_main.FileSpan(Path(source_path), 7, 8, 12)]
            restore_point = parser._add_error_restore_point("parser", 7, token, None)
            parser._dispatch_recoverable_compile_error(
                l2_main.ParseError("same failure"),
                stage="parser",
                restore_point_id=restore_point,
            )

        self.assertEqual([event["payload"]["attempt"] for event in events], [1, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
