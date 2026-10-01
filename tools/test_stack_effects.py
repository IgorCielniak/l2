#!/usr/bin/env python3
"""Regression tests for compiler and docs stack-effect comments."""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import docs
import l2_main


class StackEffectTests(unittest.TestCase):
    def test_compiler_counts_documented_effects(self) -> None:
        source = "# swap [*, x1 | x2] -> [*, x2 | x1]\nword swap"
        effect = l2_main._parse_stack_effect_comment_full(source, source.index("word"))
        self.assertEqual(effect, (2, 2))

    def test_compiler_counts_first_dual_return_branch(self) -> None:
        source = "# read [* | path] -> [*, addr | len] || [*, tag | errno]\nword read"
        effect = l2_main._parse_stack_effect_comment_full(source, source.index("word"))
        self.assertEqual(effect, (1, 2))

    def test_compiler_marks_variadic_sides_unknown(self) -> None:
        source = "# map [*, item0 ... itemN | count] -> [* | result]\nword map"
        effect = l2_main._parse_stack_effect_comment_full(source, source.index("word"))
        self.assertEqual(effect, (None, 1))

        source = "# expand [* | item] -> [*, result0 ... resultN]\nword expand"
        effect = l2_main._parse_stack_effect_comment_full(source, source.index("word"))
        self.assertEqual(effect, (1, None))

    def test_compiler_rejects_legacy_dash_effects(self) -> None:
        source = "word old # a b -- c\n"
        self.assertIsNone(l2_main._parse_stack_effect_comment_full(source, 0))

    def test_docs_extracts_unnamed_signature(self) -> None:
        lines = ["# [* | value] -> [* | result]", "word sample", "end"]
        effect, _ = docs._collect_leading_doc_comments(lines, 1, "sample")
        self.assertEqual(effect, "[* | value] -> [* | result]")

    def test_docs_extracts_real_unnamed_stdlib_signatures(self) -> None:
        entries = docs._scan_doc_file(ROOT / "stdlib/meta.sl", include_undocumented=True)
        by_name = {entry.name: entry for entry in entries}
        expected = "[* | stopLexeme] -> [* | tokens]"
        self.assertEqual(by_name["meta-collect-until"].stack_effect, expected)
        self.assertEqual(by_name["meta-collect-until-including"].stack_effect, expected)

    def test_compile_time_event_bus_has_selectable_section(self) -> None:
        reference = docs._extract_docs_tui_assets()["ct_base_text"]
        section_names = {
            match.group(1).strip()
            for line in reference.splitlines()
            if (match := docs._CT_REF_SECTION_RE.match(line)) is not None
        }
        self.assertIn("COMPILER EVENT BUS", section_names)

    def test_event_words_are_in_language_reference(self) -> None:
        entries = docs._extract_docs_tui_assets()["language_entries"]
        by_name = {entry["name"]: entry for entry in entries}
        self.assertEqual(by_name["on-event"]["category"], "Events")
        self.assertEqual(by_name["emit-event"]["category"], "Events")

    def test_docs_extracts_inline_effect_and_punctuation_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "inline.sl"
            path.write_text("word is-ready? # is-ready? [* | value] -> [* | flag]\nend\n")
            entries = docs._scan_doc_file(path, include_undocumented=True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "is-ready?")
        self.assertEqual(entries[0].stack_effect, "[* | value] -> [* | flag]")

    def test_docs_does_not_count_variadic_arities_or_generate_fixed_example(self) -> None:
        effect = "[*, item0 ... itemN | count] -> [* | result]"
        self.assertEqual(docs._parse_stack_effect_counts(effect), (-1, 1))
        self.assertEqual(docs._examples_for_word("map", effect, "word"), [])

    def test_fn_library_uses_only_documented_comment_format(self) -> None:
        source = (ROOT / "libs/fn.sl").read_text(encoding="utf-8")
        legacy = re.compile(r"^\s*word\s+[^\n]*#.*--")
        self.assertFalse(any(legacy.match(line) for line in source.splitlines()))
        for match in re.finditer(r"^[ \t]*word\s+[^\n]*#", source, re.MULTILINE):
            effect = l2_main._parse_stack_effect_comment_full(source, match.start())
            self.assertIsNotNone(effect, source[match.start():source.find("\n", match.start())])


if __name__ == "__main__":
    unittest.main(verbosity=2)