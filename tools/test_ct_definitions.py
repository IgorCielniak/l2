#!/usr/bin/env python3
"""Contract tests for definition candidates: candidate
retention and priority selection, handles, candidate records, lifecycle
words, exact source fragments, events, staged source definitions,
predeclaration, batch registration, CT winner-first dispatch, and emission
from the selected-definition snapshot."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from collections.abc import Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, DefinitionHandle, EventBus, ParseError

RECORD_KEYS = {
    "handle", "name", "owner", "eligible?", "priority", "selected?", "kind", "source-origin",
    "immediate?", "compile-only?", "runtime-only?", "inline?", "preserve?", "prefer-l2?",
    "compile-time-intrinsic?", "runtime-intrinsic?", "emission-intrinsic?",
}
DEFINITION_EVENTS = ("definition.", "word.compile.end")


class DefinitionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-definitions-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str, *, expect_error: bool = False):
        """Compile in-process; returns (compiler, bus, error-or-None)."""
        path = self.dir / "d.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        bus = EventBus(8192)
        # definition.* events are built only when wanted.
        bus.subscribe("definition.*", lambda record: None)
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"], event_bus=bus)
        error = None
        try:
            compiler.compile_file(path)
        except (ParseError, CompileError) as exc:
            error = exc
        if expect_error:
            self.assertIsNotNone(error)
        else:
            self.assertIsNone(error, error)
        return compiler, bus, error

    def run_word(self, compiler, name: str, *args):
        word = compiler.dictionary.lookup(name)
        self.assertIsNotNone(word, name)
        vm = compiler.parser.compile_time_vm
        vm.stack = list(args)
        word.compile_time_intrinsic(vm)
        return list(vm.stack)

    def assertCode(self, compiler, code: str, name: str, *args) -> None:
        with self.assertRaises(ParseError) as caught:
            self.run_word(compiler, name, *args)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.diagnostic.category,
                         l2_main.diagnostic_category(code))

    def candidates(self, compiler, name: str):
        return self.run_word(compiler, "ct-definition-candidates", name)[0]

    def events(self, bus: EventBus, prefixes=DEFINITION_EVENTS):
        return [row for row in bus.recent() if row["event"].startswith(prefixes)]


class SelectionTests(DefinitionTestBase):
    def test_priority_and_registration_order(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            priority 5
            word pickx 1 end
            word pickx 2 end
            priority -3
            word pickx 3 end
            word main 0 end
        """)
        records = self.candidates(compiler, "pickx")
        self.assertEqual([r["priority"] for r in records], [5, 0, -3])
        self.assertEqual([r["selected?"] for r in records], [1, 0, 0])
        selected = compiler.dictionary.lookup("pickx")
        self.assertEqual(selected.handle, records[0]["handle"])

    def test_equal_priority_newest_wins_and_unregister_reveals_fallback(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            word pickx 1 end
            word pickx 2 end
            word main 0 end
        """)
        newest, older = self.candidates(compiler, "pickx")
        self.assertEqual(newest["selected?"], 1)
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister", newest["handle"]), [1])
        (remaining,) = self.candidates(compiler, "pickx")
        self.assertEqual(remaining["handle"], older["handle"])
        self.assertEqual(remaining["selected?"], 1)
        self.assertIs(compiler.dictionary.lookup("pickx").handle, older["handle"])

    def test_lower_priority_is_retained(self) -> None:
        dictionary = l2_main.bootstrap_dictionary()
        high = l2_main.Word(name="pickx", priority=2)
        low = l2_main.Word(name="pickx", priority=1)
        dictionary.register(high)
        selected = dictionary.register(low)
        self.assertIs(selected, high)
        self.assertEqual([w.priority for w in dictionary.ordered_candidates("pickx")], [2, 1])

    def test_intrinsic_only_names_have_no_candidates(self) -> None:
        compiler, _, _ = self.compile("word main 0 end\n")
        self.assertEqual(self.candidates(compiler, "list-new"), [])
        self.assertEqual(self.run_word(compiler, "ct-definition-exists?", "list-new"), [1])
        self.assertEqual(self.candidates(compiler, "no-such-name"), [])
        self.assertEqual(self.run_word(compiler, "ct-definition-exists?", "no-such-name"), [0])
        names = self.run_word(compiler, "ct-definition-list-names")[0]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("list-new", names)
        self.assertIn("main", names)
        prefixed = self.run_word(compiler, "ct-definition-list-names-prefix", "ct-definition-")[0]
        self.assertIn("ct-definition-candidates", prefixed)
        self.assertTrue(all(name.startswith("ct-definition-") for name in prefixed))

    def test_source_definition_keeps_intrinsic_baselines(self) -> None:
        compiler, _, _ = self.compile("""
            word CT 7 end
            word main 0 end
        """)
        (record,) = self.candidates(compiler, "CT")
        self.assertEqual((record["compile-time-intrinsic?"], record["runtime-intrinsic?"],
                          record["emission-intrinsic?"]), (1, 1, 1))
        word = compiler.dictionary.lookup("CT")
        self.assertIsNotNone(word.intrinsic)
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister-by-name", "CT"), [1])
        self.assertEqual(self.candidates(compiler, "CT"), [])
        self.assertEqual(self.run_word(compiler, "ct-definition-exists?", "CT"), [1])


class RecordAndHandleTests(DefinitionTestBase):
    SOURCE = """
        import stdlib.sl
        word plain 1 end
        inline word fast 2 end
        :asm raw { nop };
        word main 0 end
    """

    def test_record_shape_and_immutability(self) -> None:
        compiler, _, _ = self.compile(self.SOURCE)
        (record,) = self.candidates(compiler, "plain")
        self.assertIsInstance(record, Mapping)
        self.assertNotIsInstance(record, dict)
        self.assertEqual(set(record), RECORD_KEYS)
        self.assertEqual(record["kind"], "l2")
        self.assertIsNone(record["owner"])
        self.assertEqual(record["eligible?"], 1)
        self.assertIsInstance(record["source-origin"], l2_main.SourceOrigin)
        self.assertEqual(self.candidates(compiler, "raw")[0]["kind"], "assembly")
        self.assertEqual(self.candidates(compiler, "fast")[0]["inline?"], 1)
        self.assertCode(compiler, "ct-map-immutable", "map-set", record, "priority", 9)

    def test_records_are_detached_snapshots(self) -> None:
        compiler, _, _ = self.compile(self.SOURCE)
        (before,) = self.candidates(compiler, "plain")
        self.run_word(compiler, "ct-definition-set-priority", before["handle"], 4)
        self.assertEqual(before["priority"], 0)
        self.assertEqual(self.run_word(compiler, "ct-definition-get", before["handle"])[0]["priority"], 4)

    def test_handle_identity_and_staleness(self) -> None:
        compiler, _, _ = self.compile(self.SOURCE)
        handle = self.candidates(compiler, "plain")[0]["handle"]
        again = self.candidates(compiler, "plain")[0]["handle"]
        other = self.candidates(compiler, "raw")[0]["handle"]
        self.assertIsInstance(handle, DefinitionHandle)
        self.assertEqual(handle, again)
        self.assertNotEqual(handle, other)
        self.assertCode(compiler, "ct-map-key-invalid", "map-set", {}, handle, 1)
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister", handle), [1])
        self.assertEqual(self.run_word(compiler, "ct-definition-get", handle), [None, 0])
        self.assertEqual(self.run_word(compiler, "ct-definition-body", handle), [None, 0])
        self.assertEqual(self.run_word(compiler, "ct-definition-set-priority", handle, 3), [0])
        self.assertEqual(self.run_word(compiler, "ct-definition-rename", handle, "zzz"), [0])
        self.assertEqual(self.run_word(compiler, "ct-definition-clone", handle, "zzz"), [None, 0])
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister", handle), [0])
        self.assertEqual(self.run_word(compiler, "ct-definition-prefer-l2", handle), [0])
        # Handles are never reused.
        compiler.dictionary.register(l2_main.Word(name="plain"))
        self.assertNotEqual(self.candidates(compiler, "plain")[0]["handle"], handle)

    def test_argument_errors(self) -> None:
        compiler, _, _ = self.compile(self.SOURCE)
        handle = self.candidates(compiler, "plain")[0]["handle"]
        self.assertCode(compiler, "definition-argument-type", "ct-definition-get", "plain")
        self.assertCode(compiler, "definition-argument-type", "ct-definition-set-priority", handle, True)
        self.assertCode(compiler, "definition-argument-type", "ct-definition-set-priority", handle, "1")
        self.assertCode(compiler, "definition-argument-invalid", "ct-definition-candidates", "")
        for bad in ("", " plain", "1", "12.5", '"q"', "a b"):
            self.assertCode(compiler, "definition-argument-invalid", "ct-definition-rename", handle, bad)
        self.assertEqual(self.candidates(compiler, "plain")[0]["priority"], 0)


class LifecycleWordTests(DefinitionTestBase):
    def test_set_priority_by_handle_and_name(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            word pickx 1 end
            word pickx 2 end
            word solo 3 end
            word main 0 end
        """)
        newest, older = self.candidates(compiler, "pickx")
        mark = len(bus.recent())
        self.assertEqual(self.run_word(compiler, "ct-definition-set-priority", older["handle"], 1), [1])
        self.assertEqual(self.candidates(compiler, "pickx")[0]["handle"], older["handle"])
        names = [row["event"] for row in bus.recent()[mark:]]
        self.assertEqual(names, ["definition.candidates.changed", "definition.selection.changed"])
        change, selection = (row["payload"] for row in bus.recent()[mark:])
        self.assertEqual(change["operation"], "set_priority")
        self.assertEqual((change["candidate-before"]["priority"], change["candidate-after"]["priority"]), (0, 1))
        self.assertEqual((selection["previous-handle"], selection["selected-handle"], selection["cause"]),
                         (newest["handle"], older["handle"], "set_priority"))
        # An unchanged value succeeds without an event.
        mark = len(bus.recent())
        self.assertEqual(self.run_word(compiler, "ct-definition-set-priority", older["handle"], 1), [1])
        self.assertEqual(bus.recent()[mark:], [])
        # Name-based mutation needs exactly one candidate.
        self.assertEqual(self.run_word(compiler, "ct-definition-set-priority-by-name", "pickx", 9), [0])
        self.assertEqual(self.run_word(compiler, "ct-definition-set-priority-by-name", "missing", 9), [0])
        self.assertEqual(self.run_word(compiler, "ct-definition-set-priority-by-name", "solo", 9), [1])
        self.assertEqual(self.candidates(compiler, "solo")[0]["priority"], 9)

    def test_rename(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            word src 1 end
            word src 2 end
            priority 5
            word dst 3 end
            word main 0 end
        """)
        newest, older = self.candidates(compiler, "src")
        mark = len(bus.recent())
        self.assertEqual(self.run_word(compiler, "ct-definition-rename", newest["handle"], "dst"), [1])
        self.assertEqual(self.candidates(compiler, "src")[0]["handle"], older["handle"])
        dst = self.candidates(compiler, "dst")
        self.assertEqual([r["handle"] for r in dst][1], newest["handle"])
        self.assertEqual(dst[1]["name"], "dst")
        rows = bus.recent()[mark:]
        self.assertEqual(rows[0]["event"], "definition.candidates.changed")
        payload = rows[0]["payload"]
        self.assertEqual((payload["old-name"], payload["new-name"]), ("src", "dst"))
        self.assertEqual((payload["candidate-before"]["name"], payload["candidate-after"]["name"]), ("src", "dst"))
        self.assertEqual([row["payload"]["name"] for row in rows[1:]], ["src"])
        # Renaming to the current name is an idempotent success with no event.
        mark = len(bus.recent())
        self.assertEqual(self.run_word(compiler, "ct-definition-rename", older["handle"], "src"), [1])
        self.assertEqual(bus.recent()[mark:], [])
        self.assertEqual(self.run_word(compiler, "ct-definition-rename-by-name", "dst", "x"), [0])

    def test_renamed_candidate_becomes_newest_and_is_emitted(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            word helper_a 41 end
            word helper 1 end
            word main helper 0 end
        """)
        handle = self.candidates(compiler, "helper_a")[0]["handle"]
        self.run_word(compiler, "ct-definition-rename", handle, "helper")
        self.assertIs(compiler.dictionary.lookup("helper").handle, handle)
        self.assertEqual(compiler.dictionary.lookup("helper").definition.name, "helper")

    def test_clone(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            priority 3
            word orig ( a -- b ) 1 + end
            word main 0 end
        """)
        (record,) = self.candidates(compiler, "orig")
        mark = len(bus.recent())
        handle, created = self.run_word(compiler, "ct-definition-clone", record["handle"], "copy")
        self.assertEqual(created, 1)
        self.assertNotEqual(handle, record["handle"])
        (clone,) = self.candidates(compiler, "copy")
        self.assertEqual((clone["priority"], clone["kind"]), (3, "l2"))
        self.assertEqual(self.run_word(compiler, "ct-definition-source", handle),
                         self.run_word(compiler, "ct-definition-source", record["handle"]))
        self.assertEqual(bus.recent()[mark]["payload"]["operation"], "clone")
        self.assertIsNone(bus.recent()[mark]["payload"]["candidate-before"])
        # The clone's body is independent of the original.
        original = compiler.dictionary.resolve(record["handle"])
        copy = compiler.dictionary.resolve(handle)
        self.assertIsNot(original.definition, copy.definition)
        self.assertIsNot(original.definition.body, copy.definition.body)
        # Cloning onto an existing name joins its candidate set.
        self.run_word(compiler, "ct-definition-clone-by-name", "orig", "main")
        self.assertEqual(len(self.candidates(compiler, "main")), 2)

    def test_clone_does_not_copy_intrinsic_baselines(self) -> None:
        compiler, _, _ = self.compile("""
            word CT 7 end
            word main 0 end
        """)
        handle = self.candidates(compiler, "CT")[0]["handle"]
        new_handle, _ = self.run_word(compiler, "ct-definition-clone", handle, "not_ct")
        clone = compiler.dictionary.resolve(new_handle)
        self.assertIsNone(clone.intrinsic)
        self.assertIsNone(clone.compile_time_intrinsic)
        self.assertEqual(self.candidates(compiler, "not_ct")[0]["emission-intrinsic?"], 0)

    def test_set_flags(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            word flagged 1 end
            :asm raw { nop };
            word main 0 end
        """)
        handle = self.candidates(compiler, "flagged")[0]["handle"]
        self.assertEqual(self.run_word(compiler, "ct-definition-set-flags", handle, {"compile-only": True}), [1])
        record = self.candidates(compiler, "flagged")[0]
        self.assertEqual((record["compile-only?"], record["immediate?"]), (1, 0))
        self.assertEqual(bus.recent()[-1]["payload"]["operation"], "update")
        self.assertCode(compiler, "definition-flags-conflict", "ct-definition-set-flags", handle, {"runtime-only": 1})
        self.assertCode(compiler, "definition-argument-invalid", "ct-definition-set-flags", handle, {"bogus": 1})
        self.assertEqual(self.run_word(compiler, "ct-definition-set-flags", handle, {"preserve": 1}), [1])
        record = self.candidates(compiler, "flagged")[0]
        self.assertEqual((record["inline?"], record["preserve?"]), (0, 1))
        self.assertEqual(self.run_word(compiler, "ct-definition-set-flags-by-name", "flagged",
                                       {"compile-only": 0, "inline": 1, "preserve": 1}), [1])
        record = self.candidates(compiler, "flagged")[0]
        self.assertEqual((record["compile-only?"], record["inline?"], record["preserve?"]), (0, 1, 1))

    def test_bodies_assembly_and_set_asm(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            word body_word 1 2 + end
            :asm raw { nop };
            word main 0 end
        """)
        body, found = self.run_word(compiler, "ct-definition-body-by-name", "body_word")
        self.assertEqual((found, [item["op"] for item in body]), (1, ["literal", "literal", "word"]))
        body.clear()  # detached
        self.assertEqual(len(self.run_word(compiler, "ct-definition-body-by-name", "body_word")[0]), 3)
        self.assertEqual(self.run_word(compiler, "ct-definition-asm-by-name", "body_word"), [None, 0])
        self.assertEqual(self.run_word(compiler, "ct-definition-asm-by-name", "raw")[0].strip(), "nop")
        self.assertEqual(self.run_word(compiler, "ct-definition-set-asm-by-name", "raw", " ret "), [1])
        self.assertEqual(self.run_word(compiler, "ct-definition-asm-by-name", "raw"), [" ret ", 1])
        handle = self.candidates(compiler, "body_word")[0]["handle"]
        self.assertCode(compiler, "definition-kind-invalid", "ct-definition-set-asm", handle, "nop")
        # Source is preserved across body edits.
        self.assertEqual(self.run_word(compiler, "ct-definition-source-by-name", "raw"), [":asm raw { nop };", 1])

    def test_unregister_by_name_needs_a_unique_candidate(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            word twice 1 end
            word twice 2 end
            word once 3 end
            word main 0 end
        """)
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister-by-name", "twice"), [0])
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister-by-name", "missing"), [0])
        mark = len(bus.recent())
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister-by-name", "once"), [1])
        change, selection = (row["payload"] for row in bus.recent()[mark:])
        self.assertEqual((change["operation"], change["candidate-after"]), ("unregister", None))
        self.assertEqual(change["candidate-before"]["name"], "once")
        self.assertIsNone(selection["selected-handle"])
        self.assertEqual(self.run_word(compiler, "ct-definition-exists?", "once"), [0])

    def test_owned_candidates_reject_generic_mutation(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            word owned 1 end
            word main 0 end
        """)
        word = compiler.dictionary.lookup("owned")
        word.owner = "pack"
        compiler.dictionary.owner_active = lambda owner: False
        compiler.dictionary._refresh("owned")
        (record,) = self.candidates(compiler, "owned")
        self.assertEqual((record["owner"], record["eligible?"], record["selected?"]), ("pack", 0, 0))
        self.assertEqual(self.run_word(compiler, "ct-definition-get-by-name", "owned"), [None, 0])
        self.assertEqual(self.run_word(compiler, "ct-definition-get", record["handle"])[1], 1)
        self.assertEqual(self.run_word(compiler, "ct-definition-exists?", "owned"), [1])
        for name, extra in (("ct-definition-set-priority", (1,)), ("ct-definition-rename", ("other",)),
                            ("ct-definition-clone", ("other",)), ("ct-definition-unregister", ()),
                            ("ct-definition-prefer-l2", ())):
            self.assertCode(compiler, "definition-owned", name, record["handle"], *extra)
        self.assertEqual(self.run_word(compiler, "ct-definition-unregister-by-name", "owned"), [0])


class PreferL2Tests(DefinitionTestBase):
    def test_prefer_l2_runs_definition_instead_of_intrinsic(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            word list-new 77 end
            word probe "list-new" ct-definition-prefer-l2-by-name drop end
            compile-time probe
            word check list-new 77 == static-assert end
            compile-time check
            word main 0 end
        """)
        record = self.candidates(compiler, "list-new")[0]
        self.assertEqual((record["prefer-l2?"], record["compile-time-intrinsic?"]), (1, 1))

    def test_prefer_l2_errors_and_missing_names(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            word rt 1 end runtime-only
            word main 0 end
        """)
        self.assertCode(compiler, "definition-flags-conflict", "ct-definition-prefer-l2-by-name", "rt")
        self.assertEqual(self.run_word(compiler, "ct-definition-prefer-l2-by-name", "missing"), [0])


class SourceTests(DefinitionTestBase):
    def test_exact_declaration_slices(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            priority 2
            word spaced   1   # comment
              2 + end
            :asm raw {
              nop
            };
            macro twice (x) `$x $x` emit ;
            word main 0 end
        """)
        self.assertEqual(self.run_word(compiler, "ct-definition-source-by-name", "spaced"),
                         ["word spaced   1   # comment\n  2 + end", 1])
        self.assertEqual(self.run_word(compiler, "ct-definition-source-by-name", "raw"),
                         [":asm raw {\n  nop\n};", 1])
        self.assertEqual(self.run_word(compiler, "ct-definition-source-by-name", "twice"),
                         ["macro twice (x) `$x $x` emit ;", 1])
        self.assertEqual(self.run_word(compiler, "ct-definition-source-by-name", "list-new"), [None, 0])

    def test_generated_definitions_have_no_source(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            macro defconst (name value) `word $name $value end` emit ;
            defconst seven 7
            word main 0 end
        """)
        self.assertEqual(self.run_word(compiler, "ct-definition-source-by-name", "seven"), [None, 0])
        self.assertEqual(len(self.candidates(compiler, "seven")), 1)


class RegisterSourceTests(DefinitionTestBase):
    def test_batch_returns_handles_and_publishes_per_candidate(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            word batch
                "word one 1 end\n# between\n:asm two { nop };" ct-definition-register-source
                dup list-length 2 == static-assert
                0 list-get ct-definition-source static-assert "word one 1 end" string-equal? static-assert
            end
            compile-time batch
            word main one drop 0 end
        """)
        self.assertEqual(self.run_word(compiler, "ct-definition-source-by-name", "two"), [":asm two { nop };", 1])
        changes = [row["payload"]["name"] for row in bus.recent()
                   if row["event"] == "definition.candidates.changed"
                   and row["payload"].get("name") in ("one", "two")]
        self.assertEqual(changes, ["one", "two"])

    def test_validation_failure_registers_nothing(self) -> None:
        compiler, _, error = self.compile("""
            import stdlib.sl
            word bad "word leaked 1 end\nword broken else end" ct-definition-register-source drop end
            compile-time bad
            word main 0 end
        """, expect_error=True)
        self.assertEqual(self.candidates(compiler, "leaked"), [])
        compiler, _, error = self.compile("""
            import stdlib.sl
            word bad "word ok 1 end\nextern foo 1 1" ct-definition-register-source drop end
            compile-time bad
            word main 0 end
        """, expect_error=True)
        self.assertEqual(error.diagnostic.code, "definition-source-invalid")
        self.assertEqual(self.candidates(compiler, "ok"), [])

    def test_requires_an_active_compilation(self) -> None:
        compiler, _, _ = self.compile("word main 0 end\n")
        self.assertCode(compiler, "definition-source-invalid", "ct-definition-register-source", "word x 1 end")


class StagingAndOrderTests(DefinitionTestBase):
    def test_partial_definition_is_invisible_to_ct_code(self) -> None:
        compiler, _, _ = self.compile("""
            import stdlib.sl
            word probe "staged" ct-definition-exists? 0 == static-assert end
            word staged compile-time probe 1 end
            word after "staged" ct-definition-exists? static-assert end
            compile-time after
            word main 0 end
        """)

    def test_source_events_follow_word_compile_end(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            word first 1 end
            word main 0 end
        """)
        rows = [row for row in self.events(bus) if row["payload"].get("name") == "first"]
        self.assertEqual([row["event"] for row in rows],
                         ["word.compile.end", "definition.candidates.changed", "definition.selection.changed"])
        change = rows[1]["payload"]
        self.assertEqual(change["operation"], "register")
        self.assertIsNone(change["candidate-before"])
        self.assertIsNone(change["previous-selected"])
        self.assertEqual(change["selected"], change["candidate"])

    def test_shadowed_registration_publishes_without_selection_change(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            priority 1
            word shadow 1 end
            word shadow 2 end
            word main 0 end
        """)
        rows = [row["event"] for row in self.events(bus, ("definition.",))
                if row["payload"].get("name") == "shadow"]
        self.assertEqual(rows, ["definition.candidates.changed", "definition.selection.changed",
                                "definition.candidates.changed"])

    def test_predeclare_completes_the_same_candidate(self) -> None:
        compiler, bus, _ = self.compile("""
            import stdlib.sl
            word a
                ct-definition-predeclare-all
                b 42 == static-assert
            end compile-time a
            word b 42 end
            word main 0 end
        """)
        (record,) = self.candidates(compiler, "b")
        registrations = [row for row in bus.recent() if row["event"] == "definition.candidates.changed"
                         and row["payload"].get("name") == "b"]
        self.assertEqual(len(registrations), 1)
        self.assertEqual(registrations[0]["payload"]["candidate"], record["handle"])
        self.assertEqual(self.run_word(compiler, "ct-definition-source", record["handle"]), ["word b 42 end", 1])


class DispatchTests(DefinitionTestBase):
    def test_runtime_only_winner_does_not_fall_through(self) -> None:
        _, _, error = self.compile("""
            import stdlib.sl
            word pickx 1 end
            priority 1
            word pickx 2 end runtime-only
            word probe pickx drop end
            compile-time probe
            word main 0 end
        """, expect_error=True)
        self.assertEqual(error.diagnostic.code, "ct-runtime-only-target")
        self.assertEqual(error.diagnostic.category, "ct_execution_error")

    def test_ct_calls_observe_new_selection(self) -> None:
        self.compile("""
            import stdlib.sl
            word helper 1 end
            word use helper end
            word first use 1 == static-assert end
            compile-time first
            word helper 2 end
            word second use 2 == static-assert end
            compile-time second
            word drop_it "helper" ct-definition-candidates 0 list-get "handle" map-get drop
                ct-definition-unregister static-assert end
            compile-time drop_it
            word third use 1 == static-assert end
            compile-time third
            word main 0 end
        """)


class EmissionTests(DefinitionTestBase):
    def build_and_run(self, source: str) -> str:
        path = self.dir / "e.sl"
        exe = self.dir / "e"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), str(path), "--no-daemon", "-o", str(exe)],
            cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60).stdout

    def test_selected_candidate_is_emitted(self) -> None:
        out = self.build_and_run("""
            import stdlib.sl
            priority 1
            word pickx 1 end
            word pickx 2 end
            word main pickx puti cr 0 end
        """)
        self.assertEqual(out.strip(), "1")

    def test_fallback_is_emitted_after_unregister(self) -> None:
        out = self.build_and_run("""
            import stdlib.sl
            word pickx 1 end
            word pickx 2 end
            word remove "pickx" ct-definition-candidates 0 list-get "handle" map-get drop
                ct-definition-unregister drop end
            compile-time remove
            word main pickx puti cr 0 end
        """)
        self.assertEqual(out.strip(), "1")

    def test_clone_and_rename_are_emitted(self) -> None:
        out = self.build_and_run("""
            import stdlib.sl
            word base 5 end
            word setup
                "base" "copy" ct-definition-clone-by-name drop drop
                "base" "moved" ct-definition-rename-by-name drop
            end
            compile-time setup
            word main copy moved + puti cr 0 end
        """)
        self.assertEqual(out.strip(), "10")


if __name__ == "__main__":
    unittest.main()
