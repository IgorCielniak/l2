#!/usr/bin/env python3
"""Contract tests for unified macros: definitions,
fixed-arity argument capture, bindings, isolated CT stacks, buffered
emission, nested and direct calls, ct-macro-context, macro.expansion
events, ct-macro-get/-set, ct-definition-set-body, pack adoption,
winner-first selection, the quotation fast path, and retired words."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, EventBus, ParseError, Syntax

PROBE = "int>string ct-parser-current swap token-with-lexeme list-new swap list-append list>syntax emit"


class MacroTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-macros-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str, *, expect_error: bool = False, bus: EventBus = None):
        path = self.dir / "m.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
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
        return compiler, error

    def codes(self, compiler):
        return [d.code for d in compiler.parser.diagnostics if d.severity == "error"]

    def check(self, definitions: str, expr: str, expected: int) -> None:
        """`expr` (using the macros) must evaluate to `expected` at CT."""
        self.compile(f"""
            import stdlib.sl
            {definitions}
            word check {expr} {expected} == static-assert end
            compile-time check
            word main 0 end
        """)

    def run_word(self, compiler, name: str, *args):
        word = compiler.dictionary.lookup(name)
        vm = compiler.parser.compile_time_vm
        vm.stack = list(args)
        word.compile_time_intrinsic(vm)
        return list(vm.stack)


class DefinitionAndArgumentTests(MacroTestBase):
    def test_zero_and_fixed_arity(self) -> None:
        self.check("macro answer `42` emit ;", "answer", 42)
        self.check("macro add (x y) `$x $y +` emit ;", "add 20 22", 42)
        self.check("macro add () `1` emit ;", "add", 1)

    def test_quotation_argument_is_one_value(self) -> None:
        self.check("macro run (code) $code emit ;", "run `40 2 +`", 42)

    def test_commas_are_ordinary_tokens(self) -> None:
        self.check("macro second (a b) $b token-lexeme \",\" string-equal? static-assert `7` emit ;", "second x ,", 7)

    def test_arguments_are_not_expanded(self) -> None:
        self.check("macro answer `42` emit ;  macro grab (x) $x token-lexeme \"answer\" string-equal? static-assert `1` emit ;",
                   "grab answer", 1)

    def test_empty_expansion(self) -> None:
        self.check("macro erase (x) ;", "41 erase 99 1 +", 42)

    def test_insufficient_arguments(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            macro add (x y) `$x $y +` emit ;
            word main 0 end
            add 1
        """, expect_error=True)
        diag = next(d for d in compiler.parser.diagnostics if d.code == "macro-insufficient-arguments")
        self.assertEqual(diag.category, "macro_error")
        self.assertIn("expects 2", diag.message)
        self.assertIn("only 1", diag.message)

    def test_invalid_parameter_lists(self) -> None:
        for header in ("macro m (x x)", "macro m (if)", "macro m (end)", "macro m (1x)", "macro m (x"):
            with self.subTest(header=header):
                compiler, _ = self.compile(f"""
                    import stdlib.sl
                    {header} `1` emit ;
                    word main 0 end
                """, expect_error=True)
                self.assertIn("macro-signature-invalid", self.codes(compiler))

    def test_semicolon_inside_a_block_is_not_the_terminator(self) -> None:
        self.check("macro m 1 if `;` drop end `5` emit ;", "m", 5)

    def test_unterminated_macro(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word main 0 end
            macro m `1` emit
        """, expect_error=True)
        self.assertIn("parser-macro-unterminated", self.codes(compiler))


class BindingAndExecutionTests(MacroTestBase):
    def test_bindings_are_repeatable_values(self) -> None:
        self.check("macro twice (x) $x token-lexeme $x token-lexeme string-equal? static-assert `$x $x +` emit ;",
                   "twice 21", 42)

    def test_unbound_name_is_an_error(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            macro m (x) $y drop ;
            word main m 1 0 end
        """, expect_error=True)
        self.assertIn("syntax-binding-unbound", self.codes(compiler))

    def test_isolated_stack_discards_leftovers(self) -> None:
        self.check("macro noisy 1 2 3 `4` emit ;", "10 noisy +", 14)

    def test_failed_body_discards_pending_output(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            macro bad `999` emit "boom" ct-parser-error ;
            word main bad 0 end
        """, expect_error=True)
        self.assertIn("ct-parser-error-raised", self.codes(compiler))

    def test_nested_direct_call_and_emitted_call(self) -> None:
        defs = """
            macro increment (x) `$x 1 +` emit ;
            macro increment_next increment ;
            macro add (x y) `$x $y +` emit ;
            macro add_twice (x) `add $x $x` emit ;
        """
        self.check(defs, "increment_next 41", 42)
        self.check(defs, "add_twice 21", 42)

    def test_generated_macro_definition(self) -> None:
        self.check("macro make_twice `macro twice (y) \\` \\$y \\$y + \\` emit ;` emit ;  make_twice",
                   "twice 21", 42)

    def test_macro_context(self) -> None:
        self.compile("""
            import stdlib.sl
            word ctx_name ct-macro-context "macro" map-get drop end compile-only
            macro inner
              ct-macro-context
              dup "macro" map-get drop "inner" string-equal? static-assert
              dup "expansion-trace" map-get drop list-length 2 == static-assert
              dup "call-site" map-get drop nil? 0 == static-assert
              "definition-origin" map-get drop nil? 0 == static-assert
            ;
            macro outer ctx_name "outer" string-equal? static-assert inner ;
            word main outer 0 end
        """)
        compiler, _ = self.compile("""
            import stdlib.sl
            word probe ct-macro-context drop end
            compile-time probe
            word main 0 end
        """, expect_error=True)
        self.assertIn("macro-context-unavailable", self.codes(compiler))

    def test_winner_first_selection(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            macro pick `1` emit ;
            priority 1
            word pick 2 end
            word check pick 2 == static-assert end
            compile-time check
            word main 0 end
        """)
        compiler, _ = self.compile("""
            import stdlib.sl
            macro rt `1` emit ;
            word mark "rt" map-new "runtime-only" 1 map-set ct-definition-set-flags-by-name drop end
            compile-time mark
            word main rt 0 end
        """, expect_error=True)
        self.assertIn("ct-runtime-only-target", self.codes(compiler))

    def test_fast_path_matches_general_path(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            macro fast (a b) `$a $b +` emit ;
            macro slow (a b) 0 drop `$a $b +` emit ;
            word main 0 end
        """)
        fast = compiler.dictionary.lookup("fast").macro_def
        slow = compiler.dictionary.lookup("slow").macro_def
        self.assertIsNotNone(fast.fast)
        self.assertIsNone(slow.fast)
        self.check("macro fast (a b) `$a $b +` emit ;  macro slow (a b) 0 drop `$a $b +` emit ;",
                   "fast 2 3 slow 2 3 ==", 1)


class EventTests(MacroTestBase):
    def test_macro_expansion_events(self) -> None:
        bus = EventBus(4096)
        rows = []
        bus.subscribe("macro.expansion", rows.append)
        self.compile("""
            import stdlib.sl
            macro child (x) `$x 1 +` emit ;
            macro parent child ;
            word main parent 41 drop 0 end
        """, bus=bus)
        payloads = [row["payload"] for row in rows if row["payload"]["macro"] in ("child", "parent")]
        self.assertEqual([p["macro"] for p in payloads], ["child", "parent"])
        child, parent = payloads
        self.assertEqual((child["depth"], parent["depth"]), (1, 0))
        self.assertEqual((child["status"], child["attempt"]), ("success", 1))
        self.assertEqual([t.lexeme for t in parent["tokens"]], ["41", "1", "+"])
        self.assertIsInstance(child["call-site"], l2_main.SourceOrigin)

    def test_failed_expansion_event(self) -> None:
        bus = EventBus(4096)
        rows = []
        bus.subscribe("macro.expansion", rows.append)
        self.compile("""
            import stdlib.sl
            macro bad "boom" ct-parser-error ;
            word main bad 0 end
        """, bus=bus, expect_error=True)
        failed = [row["payload"] for row in rows if row["payload"]["macro"] == "bad"]
        self.assertEqual(failed[0]["status"], "failed")
        self.assertNotIn("tokens", failed[0])
        self.assertIsNotNone(failed[0]["code"])

    def test_no_payloads_without_listeners(self) -> None:
        bus = EventBus(4096)
        self.compile("""
            import stdlib.sl
            macro m `1` emit ;
            word main m drop 0 end
        """, bus=bus)
        self.assertFalse(any(row["event"] == "macro.expansion" for row in bus.recent()))


class MacroRecordTests(MacroTestBase):
    SOURCE = """
        import stdlib.sl
        macro add (x y) `$x $y +` emit ;
        word plain 1 end
        word main 0 end
    """

    def test_get_returns_detached_record(self) -> None:
        compiler, _ = self.compile(self.SOURCE)
        record, found = self.run_word(compiler, "ct-macro-get-by-name", "add")
        self.assertEqual(found, 1)
        self.assertEqual(set(record), {"parameters", "body", "metadata"})
        self.assertEqual(record["parameters"], ["x", "y"])
        self.assertIsInstance(record["body"], Syntax)
        record["parameters"].append("z")
        record["metadata"]["k"] = 1
        again, _ = self.run_word(compiler, "ct-macro-get-by-name", "add")
        self.assertEqual(again["parameters"], ["x", "y"])
        self.assertEqual(again["metadata"], {})
        self.assertEqual(self.run_word(compiler, "ct-macro-get-by-name", "plain"), [None, 0])

    def test_set_validates_atomically(self) -> None:
        compiler, _ = self.compile(self.SOURCE)
        add = compiler.dictionary.lookup("add")
        before = add.macro_def
        for patch, code in (({"name": "x"}, "definition-argument-invalid"),
                            ({"parameters": ["x", "x"]}, "definition-argument-invalid"),
                            ({"parameters": "x"}, "definition-argument-type"),
                            ({"body": "x"}, "definition-argument-type"),
                            ({"metadata": {1: 2}}, "definition-argument-type"),
                            ({"metadata": {"f": add}}, "definition-argument-invalid")):
            with self.subTest(patch=patch):
                with self.assertRaises(ParseError) as caught:
                    self.run_word(compiler, "ct-macro-set-by-name", "add", patch)
                self.assertEqual(caught.exception.code, code)
        bad_body = Syntax(compiler.parser.reader.tokenize_text("1 if", "<t>"))
        with self.assertRaises(ParseError):
            self.run_word(compiler, "ct-macro-set-by-name", "add", {"body": bad_body})
        self.assertIs(add.macro_def, before)

    def test_set_updates_in_place_and_publishes_update(self) -> None:
        bus = EventBus(4096)
        bus.subscribe("definition.*", lambda record: None)  # definition events are built only when wanted
        compiler, _ = self.compile(self.SOURCE, bus=bus)
        add = compiler.dictionary.lookup("add")
        handle = add.handle
        mark = len(bus.recent())
        body = Syntax(compiler.parser.reader.tokenize_text("`$x $y *` emit", "<t>"))
        self.assertEqual(self.run_word(compiler, "ct-macro-set", handle, {"body": body, "metadata": {"doc": "mul"}}), [1])
        self.assertIs(compiler.dictionary.lookup("add").handle, handle)
        events = [row for row in bus.recent()[mark:] if row["event"] == "definition.candidates.changed"]
        self.assertEqual([row["payload"]["operation"] for row in events], ["update"])
        record, _ = self.run_word(compiler, "ct-macro-get", handle)
        self.assertEqual(record["metadata"], {"doc": "mul"})
        # No-op: accepted, no event.
        mark = len(bus.recent())
        self.assertEqual(self.run_word(compiler, "ct-macro-set", handle, {"metadata": {"doc": "mul"}}), [1])
        self.assertEqual([row for row in bus.recent()[mark:] if row["event"].startswith("definition.")], [])

    def test_set_does_not_execute_the_body(self) -> None:
        compiler, _ = self.compile(self.SOURCE)
        body = Syntax(compiler.parser.reader.tokenize_text('"never" ct-parser-error', "<t>"))
        self.assertEqual(self.run_word(compiler, "ct-macro-set-by-name", "add", {"body": body}), [1])

    def test_set_targets(self) -> None:
        compiler, _ = self.compile(self.SOURCE)
        handle = compiler.dictionary.lookup("add").handle
        self.assertEqual(self.run_word(compiler, "ct-macro-set-by-name", "missing", {}), [0])
        with self.assertRaises(ParseError) as caught:
            self.run_word(compiler, "ct-macro-set-by-name", "plain", {"metadata": {}})
        self.assertEqual(caught.exception.code, "definition-kind-invalid")
        compiler.dictionary.unregister(compiler.dictionary.resolve(handle))
        self.assertEqual(self.run_word(compiler, "ct-macro-set", handle, {}), [0])

    def test_set_body_of_a_word(self) -> None:
        self.compile("""
            import stdlib.sl
            word value 1 end
            word change "value" `40 2 +` ct-definition-set-body-by-name static-assert end
            compile-time change
            word check value 42 == static-assert end
            compile-time check
            word main 0 end
        """)
        compiler, _ = self.compile(self.SOURCE)
        body = Syntax(compiler.parser.reader.tokenize_text("1", "<t>"))
        with self.assertRaises(ParseError) as caught:
            self.run_word(compiler, "ct-definition-set-body-by-name", "add", body)
        self.assertEqual(caught.exception.code, "definition-kind-invalid")


class PackAdoptionTests(MacroTestBase):
    def test_register_macro_adopts_the_selected_candidate(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            macro m `1` emit ;
            word setup
              "pack" ct-lang-create drop
              "pack" ct-lang-activate drop
              "pack" "m" ct-lang-register-macro static-assert
              "pack" "m" ct-lang-register-macro 0 == static-assert
              "pack" "missing" ct-lang-register-macro 0 == static-assert
            end
            compile-time setup
            word check m 1 == static-assert end
            compile-time check
            word main 0 end
        """)
        record = self.run_word(compiler, "ct-definition-get-by-name", "m")[0]
        self.assertEqual(record["owner"], "pack")


class RetirementTests(MacroTestBase):
    def test_retired_words_are_gone(self) -> None:
        compiler, _ = self.compile("word main 0 end\n")
        for name in ("ct-register-text-macro", "ct-register-pattern-macro", "ct-capture-get", "ct-clone-macro",
                     "ct-macro-doc-set", "ct-set-macro-preview", "emit-definition", "ct-word-is-text-macro",
                     "ct-lang-register-text-macro-signature", "ct-detect-pattern-conflicts"):
            self.assertIsNone(compiler.dictionary.lookup(name), name)


if __name__ == "__main__":
    unittest.main()
