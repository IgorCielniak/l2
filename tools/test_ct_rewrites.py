#!/usr/bin/env python3
"""Contract tests for rewrites: registration and
validation, RewriteHandles and handle-based properties, fixed selection
order, pipelines and scopes, guards, exact-state loop detection, the step
budget, list simulations, rewrite.applied events, and retired words."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, EventBus, ParseError, RewriteHandle


def lst(*items: str) -> str:
    """L2 code building a list of string literals."""
    out = ["list-new"]
    for item in items:
        escaped = item.replace("\\", "\\\\").replace('"', '\\"')
        out.append(f'"{escaped}" list-append')
    return " ".join(out)


class RewriteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-rewrites-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compiler(self, bus: EventBus = None) -> l2_main.Compiler:
        return l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"], event_bus=bus)

    def compile(self, source: str, *, expect_error: bool = False, bus: EventBus = None):
        path = self.dir / "r.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        compiler = self.compiler(bus)
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

    def ct(self, body: str, *, expect_error: bool = False, bus: EventBus = None, after: str = ""):
        """Run `body` as a compile-time word."""
        return self.compile(f"""
            import stdlib.sl
            word check
            {body}
            end
            compile-time check
            {after}
            word main 0 end
        """, expect_error=expect_error, bus=bus)

    def error_codes(self, compiler):
        return [d.code for d in compiler.parser.diagnostics if d.severity == "error"]

    def engine(self) -> l2_main.RewriteEngine:
        return self.compiler().parser.rewrites


class RegistrationTests(RewriteTestBase):
    def test_invalid_patterns_are_rejected_before_registration(self) -> None:
        engine = self.engine()
        bad = [
            ([], ["x"]),                       # no element
            (["x?"], ["y"]),                   # minimum match length zero
            (["\\"], ["y"]),                   # only a backslash
            (["$x:syntax"], ["y"]),            # syntax constraint is match-only
            (["$x:identifier"], ["y"]),        # former alias
            (["$x:Int"], ["y"]),               # constraints are case-sensitive
            (["**"], ["y"]),                   # wildcard takes no quantifier
            (["!*"], ["y"]),                   # wildcard cannot be negated
            (["!$x+"], ["y"]),                 # negation with a quantifier
            (["$*x+"], ["y"]),                 # sequence capture with a quantifier
            (["$x", "$*x"], ["y"]),            # mixed capture shapes
            (["$x"], ["$y"]),                  # unbound reference
            (["$x+"], ["$x"]),                 # sequence capture referenced as single
            (["$x"], ["$*x"]),                 # single capture referenced as sequence
            (["$x"], ["$x:int"]),              # reference with a constraint
            (["$x"], ["\\"]),                  # replacement of only a backslash
            (["$-x"], ["y"]),                  # malformed capture
            (["x"], [""]),                     # empty replacement element
            (["x", 3], ["y"]),                 # non-string element
        ]
        for pattern, replacement in bad:
            with self.subTest(pattern=pattern, replacement=replacement):
                with self.assertRaises(ParseError) as caught:
                    engine.register("grammar", "r", pattern, replacement)
                self.assertEqual(caught.exception.code, "rewrite-pattern-invalid")
                self.assertEqual(caught.exception.diagnostic.category, "rewrite_error")
                self.assertEqual(caught.exception.diagnostic.phase, "rewrite-registration")
        self.assertEqual(engine.live, {})

    def test_empty_name_is_rejected(self) -> None:
        with self.assertRaises(ParseError) as caught:
            self.engine().register("reader", "", ["x"], ["y"])
        self.assertEqual(caught.exception.code, "rewrite-name-invalid")

    def test_handles_and_duplicate_names(self) -> None:
        engine = self.engine()
        first = engine.register("grammar", "same", ["a"], ["1"])
        second = engine.register("grammar", "same", ["a"], ["2"])
        self.assertIsInstance(first, RewriteHandle)
        self.assertNotEqual(first, second)
        self.assertEqual([entry["handle"] for entry in engine.listing("grammar")], [first, second])
        with self.assertRaises(ParseError) as caught:
            engine.by_name("grammar", "same")
        self.assertEqual(caught.exception.code, "rewrite-query-ambiguous")
        self.assertTrue(engine.remove(first))
        self.assertFalse(engine.remove(first))
        self.assertIs(engine.by_name("grammar", "same"), engine.resolve(second))
        third = engine.register("grammar", "same", ["a"], ["3"])
        self.assertNotEqual(third, first)

    def test_unnamed_rules_get_generated_names(self) -> None:
        engine = self.engine()
        engine.register("reader", None, ["a"], ["b"])
        self.assertTrue(engine.listing("reader")[0]["name"].startswith("reader-rewrite-"))


class MatchingTests(RewriteTestBase):
    def run_list(self, rules, lexemes, stage="grammar"):
        engine = self.engine()
        for pattern, replacement in rules:
            engine.register(stage, None, pattern, replacement)
        return engine.simulate(stage, lexemes, 1000)

    def test_wildcard_escape_and_literal_star(self) -> None:
        self.assertEqual(self.run_list([(["*", "\\*", "1"], ["one"])], ["7", "*", "1"])[0], ["one"])
        self.assertEqual(self.run_list([(["*", "\\*", "1"], ["one"])], ["7", "+", "1"])[0], ["7", "+", "1"])
        self.assertEqual(self.run_list([(["\\$x", "\\!y"], ["lit"])], ["$x", "!y"])[0], ["lit"])
        # In replacements, `*` and `!` are ordinary lexemes; `\` escapes.
        self.assertEqual(self.run_list([(["a"], ["*", "!", "\\$x", "\\\\"])], ["a"])[0], ["*", "!", "$x", "\\"])

    def test_constraints_use_token_categories(self) -> None:
        rule = [(["$v:literal"], ["L"])]
        self.assertEqual(self.run_list(rule, ['"s"', "'c'", "1", "2.5", "true", "nil", "x"])[0],
                         ["L", "L", "L", "L", "true", "nil", "x"])
        self.assertEqual(self.run_list([(["$v:number"], ["N"])], ["0x10", "1.5", "a"])[0], ["N", "N", "a"])
        self.assertEqual(self.run_list([(["$v:ident"], ["7"])], ["abc", "1"])[0], ["7", "1"])

    def test_quantifiers_negation_and_sequences(self) -> None:
        rules = [(["sum", "$xs:int+", "end"], ["[", "$*xs", "]"])]
        self.assertEqual(self.run_list(rules, ["sum", "1", "2", "3", "end"])[0], ["[", "1", "2", "3", "]"])
        rules = [(["f", "!g", "$y"], ["ok", "$y"])]
        self.assertEqual(self.run_list(rules, ["f", "h", "z"])[0], ["ok", "z"])
        self.assertEqual(self.run_list(rules, ["f", "g", "z"])[0], ["f", "g", "z"])
        rules = [(["a", "$*xs", "b"], ["$*xs"])]
        self.assertEqual(self.run_list(rules, ["a", "b"])[0], [])
        rules = [(["$x", "$x"], ["dup", "$x"])]
        self.assertEqual(self.run_list(rules, ["q", "q", "q", "r"])[0], ["dup", "dup", "q", "r"])

    def test_fixed_order_priority_then_registration(self) -> None:
        engine = self.engine()
        low = engine.register("grammar", "low", ["k"], ["10"])
        engine.register("grammar", "first", ["k"], ["11"])
        engine.register("grammar", "second", ["k"], ["12"])
        self.assertEqual(engine.simulate("grammar", ["k"], 10)[0], ["10"])
        engine.set_priority(engine.resolve(low), -1)
        self.assertEqual(engine.simulate("grammar", ["k"], 10)[0], ["11"])
        # Anchored and unanchored rules merge into the one fixed order.
        engine.register("grammar", "wild", ["$x:ident"], ["9"])
        self.assertEqual(engine.simulate("grammar", ["k"], 10)[0], ["11"])
        engine.set_priority(engine.by_name("grammar", "wild"), 5)
        self.assertEqual(engine.simulate("grammar", ["k"], 10)[0], ["9"])

    def test_local_retry_without_fixed_point_sweeps(self) -> None:
        # b -> c only applies at the cursor; earlier output is not revisited.
        rules = [(["a"], ["b"]), (["b"], ["c"]), (["x", "c"], ["y"])]
        result, patches = self.run_list(rules, ["x", "a"])
        self.assertEqual(result, ["x", "c"])
        self.assertEqual([(p["rule"], p["start"], p["end"], p["before"], p["after"]) for p in patches],
                         [(patches[0]["rule"], 1, 2, ["a"], ["b"]), (patches[1]["rule"], 1, 2, ["b"], ["c"])])
        self.assertNotIn("specificity", patches[0])
        self.assertEqual(set(patches[0]), {"stage", "handle", "rule", "start", "end", "before", "after", "priority"})


class PropertyWordTests(RewriteTestBase):
    def test_handle_properties_and_stale_handles(self) -> None:
        self.ct(f"""
            "r" {lst("kw")} {lst("1")} ct-rewrite-add-grammar-named
            dup 5 ct-rewrite-rule-set-priority static-assert
            dup ct-rewrite-rule-get-priority static-assert 5 == static-assert
            dup 0 ct-rewrite-rule-set-enabled static-assert
            dup ct-rewrite-rule-get-enabled static-assert 0 == static-assert
            dup "pipe" ct-rewrite-rule-set-pipeline static-assert
            dup ct-rewrite-rule-get-pipeline static-assert "pipe" string-equal? static-assert
            dup "guard_word" ct-rewrite-rule-set-guard static-assert
            dup ct-rewrite-rule-get-guard static-assert "guard_word" string-equal? static-assert
            dup nil ct-rewrite-rule-set-guard static-assert
            dup ct-rewrite-rule-get-guard static-assert nil? static-assert
            "grammar" "r" ct-rewrite-rule-get-priority static-assert 5 == static-assert
            "grammar" "nope" ct-rewrite-rule-get-priority 0 == static-assert nil? static-assert
            dup ct-rewrite-remove static-assert
            dup ct-rewrite-remove 0 == static-assert
            dup 1 ct-rewrite-rule-set-priority 0 == static-assert
            ct-rewrite-rule-get-priority 0 == static-assert nil? static-assert
        """)

    def test_invalid_property_values_and_handle_types(self) -> None:
        cases = [
            ('h "1" ct-rewrite-rule-set-enabled', "rewrite-mutation-invalid"),
            ('h "" ct-rewrite-rule-set-pipeline', "rewrite-mutation-invalid"),
            ('h "" ct-rewrite-rule-set-guard', "rewrite-mutation-invalid"),
            ('"x" 1 ct-rewrite-rule-set-priority', "rewrite-handle-invalid"),
            ('1 ct-rewrite-remove', "rewrite-handle-invalid"),
            ('1 ct-rewrite-rule-get-priority', "rewrite-handle-invalid"),
            ('"Grammar" "r" ct-rewrite-rule-get-priority', "rewrite-query-invalid"),
        ]
        for code, expected in cases:
            with self.subTest(code=code):
                compiler, _ = self.ct(f"""
                    "r" {lst("kw")} {lst("1")} ct-rewrite-add-grammar-named
                    {code.replace("h ", "dup ", 1) if code.startswith("h ") else code}
                """, expect_error=True)
                self.assertIn(expected, self.error_codes(compiler))

    def test_ambiguous_by_name_query(self) -> None:
        compiler, _ = self.ct(f"""
            "r" {lst("a")} {lst("1")} ct-rewrite-add-grammar-named drop
            "r" {lst("b")} {lst("2")} ct-rewrite-add-grammar-named drop
            "grammar" "r" ct-rewrite-rule-get-enabled
        """, expect_error=True)
        diag = next(d for d in compiler.parser.diagnostics if d.code == "rewrite-query-ambiguous")
        self.assertEqual(diag.phase, "rewrite-query")

    def test_clear_removes_only_unowned_rules(self) -> None:
        self.ct(f"""
            "pack" ct-lang-create drop
            "pack" "owned" {lst("a")} {lst("1")} ct-lang-add-grammar-rewrite-named >r
            {lst("b")} {lst("2")} ct-rewrite-add-grammar
            {lst("c")} {lst("3")} ct-rewrite-add-grammar drop
            ct-rewrite-clear-grammar 2 == static-assert
            ct-rewrite-remove 0 == static-assert
            ct-rewrite-list-grammar list-length 1 == static-assert
            ct-rewrite-list-grammar 0 list-get "handle" map-get drop r> == static-assert
        """)

    def test_controls_validate_their_inputs(self) -> None:
        cases = [
            ("-1 ct-rewrite-set-max-steps", "rewrite-control-invalid"),
            ('"x" ct-rewrite-set-loop-detection', "rewrite-control-invalid"),
            ('"grammar" "" 1 ct-rewrite-set-pipeline-active', "rewrite-control-invalid"),
            ('"other" "p" 1 ct-rewrite-set-pipeline-active', "rewrite-control-invalid"),
            ('"grammar" "p" "yes" ct-rewrite-set-pipeline-active', "rewrite-control-invalid"),
            (f'"grammar" {lst("a")} -1 ct-rewrite-dry-run', "rewrite-input-invalid"),
            ('"grammar" list-new 1 list-append ct-rewrite-run-on-list', "rewrite-input-invalid"),
            (f'" grammar" {lst("a")} ct-rewrite-run-on-list', "rewrite-input-invalid"),
        ]
        for code, expected in cases:
            with self.subTest(code=code):
                compiler, _ = self.ct(code, expect_error=True)
                self.assertIn(expected, self.error_codes(compiler))

    def test_booleans_are_not_integers_for_counts_and_priorities(self) -> None:
        compiler = self.compiler()
        vm = compiler.parser.compile_time_vm
        handle = compiler.parser.rewrites.register("grammar", "r", ["a"], ["b"])
        for name, args, code in (("ct-rewrite-set-max-steps", [True], "rewrite-control-invalid"),
                                 ("ct-rewrite-dry-run", ["grammar", ["a"], False], "rewrite-input-invalid"),
                                 ("ct-rewrite-rule-set-priority", [handle, True], "rewrite-mutation-invalid")):
            with self.subTest(word=name):
                vm.stack = list(args)
                with self.assertRaises(ParseError) as caught:
                    compiler.dictionary.lookup(name).compile_time_intrinsic(vm)
                self.assertEqual(caught.exception.code, code)
        vm.stack = [handle, True]
        compiler.dictionary.lookup("ct-rewrite-rule-set-enabled").compile_time_intrinsic(vm)
        self.assertEqual(vm.stack, [1])

    def test_control_defaults_and_values(self) -> None:
        self.ct("""
            ct-rewrite-get-max-steps 100000 == static-assert
            ct-rewrite-loop-detection? static-assert
            "grammar" ct-rewrite-list-active-pipelines dup list-length 1 == static-assert
            0 list-get "default" string-equal? static-assert
            0 ct-rewrite-set-max-steps ct-rewrite-get-max-steps 0 == static-assert
            0 ct-rewrite-set-loop-detection ct-rewrite-loop-detection? 0 == static-assert
            "grammar" "b" 1 ct-rewrite-set-pipeline-active
            "grammar" "a" 1 ct-rewrite-set-pipeline-active
            "grammar" "default" 0 ct-rewrite-set-pipeline-active
            "grammar" ct-rewrite-list-active-pipelines dup list-length 2 == static-assert
            0 list-get "a" string-equal? static-assert
        """)


class PipelineAndScopeTests(RewriteTestBase):
    def test_pipeline_gates_live_rules_and_scope_restores(self) -> None:
        self.compile(f"""
            import stdlib.sl
            word kw 1 end
            word setup
              "kw" {lst("kw")} {lst("42")} ct-rewrite-add-grammar-named
              "special" ct-rewrite-rule-set-pipeline drop
            end
            compile-time setup
            word before kw end
            word on
              ct-rewrite-scope-push 1 == static-assert
              "grammar" "special" 1 ct-rewrite-set-pipeline-active
            end
            compile-time on
            word during kw end
            word off ct-rewrite-scope-pop static-assert ct-rewrite-scope-pop 0 == static-assert end
            compile-time off
            word after kw end
            word check before 1 == static-assert during 42 == static-assert after 1 == static-assert end
            compile-time check
            word main 0 end
        """)


class GuardTests(RewriteTestBase):
    def test_guard_payload_and_rejection(self) -> None:
        self.compile(f"""
            import stdlib.sl
            word only_big   # payload -- applies?
              dup "stage" map-get drop "grammar" string-equal? static-assert
              dup "rule" map-get drop "big" string-equal? static-assert
              dup "handle" map-get drop nil? 0 == static-assert
              dup "pattern" map-get drop list-length 2 == static-assert
              dup "replacement" map-get drop list-length 1 == static-assert
              dup "end" map-get drop over "start" map-get drop - 2 == static-assert
              dup map-length 8 == static-assert
              "captures" map-get drop "n" map-get drop try-string>int drop 10 >
            end
            word check
              "big" {lst("big", "$n:int")} {lst("1000")} ct-rewrite-add-grammar-named
              "only_big" ct-rewrite-rule-set-guard static-assert
              "grammar" {lst("big", "3", "big", "30")} ct-rewrite-run-on-list
              list-length 1 == static-assert
              dup list-length 3 == static-assert
              dup 1 list-get "3" string-equal? static-assert
              2 list-get "1000" string-equal? static-assert
            end
            compile-time check
            word main 0 end
        """)

    def test_guard_failures(self) -> None:
        cases = [
            ("missing_guard", "", "not defined"),
            ("two", "word two drop 1 1 end", "returned [integer, integer]"),
            ("none", "word none drop end", "returned []"),
            ("str", 'word str drop "y" end', "returned [string]"),
            ("mac", "macro mac `1` emit ;", "is a macro"),
        ]
        for name, definition, reason in cases:
            with self.subTest(guard=name):
                compiler, _ = self.compile(f"""
                    import stdlib.sl
                    {definition}
                    word setup
                      "g" {lst("trigger")} {lst("1")} ct-rewrite-add-grammar-named
                      "{name}" ct-rewrite-rule-set-guard static-assert
                    end
                    compile-time setup
                    word main trigger end
                """, expect_error=True)
                diag = next(d for d in compiler.parser.diagnostics if d.code == "rewrite-guard-failed")
                self.assertEqual(diag.phase, "rewrite-guard")
                self.assertIn(reason, diag.message)
                self.assertIn("'g'", diag.message)


class LoopAndBudgetTests(RewriteTestBase):
    def test_identity_and_cycles_are_detected_before_commit(self) -> None:
        for rules in ([(["a"], ["a"])], [(["a"], ["b"]), (["b"], ["a"])]):
            with self.subTest(rules=rules):
                engine = self.engine()
                for pattern, replacement in rules:
                    engine.register("grammar", None, pattern, replacement)
                with self.assertRaises(ParseError) as caught:
                    engine.simulate("grammar", ["x", "a", "y"], 100)
                self.assertEqual(caught.exception.code, "rewrite-loop-detected")
                self.assertEqual(caught.exception.diagnostic.phase, "rewrite-loop")
                report = engine.loop_reports[-1]
                self.assertEqual(report["sequence"], ["x", "a", "y"])
                self.assertEqual(report["stage"], "grammar")

    def test_growing_rewrite_hits_the_budget_without_loop_detection(self) -> None:
        engine = self.engine()
        engine.register("grammar", None, ["a"], ["a", "a"])
        engine.loop_detection = False
        with self.assertRaises(ParseError) as caught:
            engine.simulate("grammar", ["a"], 5)
        self.assertEqual(caught.exception.code, "rewrite-max-steps")
        self.assertIn("5 committed", caught.exception.diagnostic.message)

    def test_zero_budget_rejects_the_first_application(self) -> None:
        engine = self.engine()
        engine.register("grammar", None, ["a"], ["b"])
        with self.assertRaises(ParseError) as caught:
            engine.simulate("grammar", ["a"], 0)
        self.assertEqual(caught.exception.code, "rewrite-max-steps")
        self.assertEqual(engine.simulate("grammar", ["c"], 0)[0], ["c"])

    def test_live_loop_is_recoverable_and_reported(self) -> None:
        compiler, _ = self.compile(f"""
            import stdlib.sl
            word setup "ping" {lst("ping")} {lst("pong")} ct-rewrite-add-grammar-named drop
                       "pong" {lst("pong")} {lst("ping")} ct-rewrite-add-grammar-named drop end
            compile-time setup
            word main ping end
        """, expect_error=True)
        self.assertIn("rewrite-loop-detected", self.error_codes(compiler))
        self.assertEqual(len(compiler.parser.rewrites.loop_reports), 1)

    def test_live_budget_is_shared_across_stages(self) -> None:
        compiler, _ = self.compile(f"""
            import stdlib.sl
            word setup
              {lst("r")} {lst("g")} ct-rewrite-add-reader drop
              {lst("g")} {lst("1")} ct-rewrite-add-grammar drop
              1 ct-rewrite-set-max-steps
            end
            compile-time setup
            word main r end
        """, expect_error=True)
        self.assertIn("rewrite-max-steps", self.error_codes(compiler))


class EventTests(RewriteTestBase):
    def test_rewrite_applied_events(self) -> None:
        bus = EventBus()
        events = []
        bus.subscribe("rewrite.applied", events.append)
        self.compile(f"""
            import stdlib.sl
            word setup "seven" {lst("seven")} {lst("7")} ct-rewrite-add-grammar-named drop end
            compile-time setup
            word main seven drop 0 end
        """, bus=bus)
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual((payload["stage"], payload["rule"], payload["before"], payload["after"], payload["priority"]),
                         ("grammar", "seven", ["seven"], ["7"], 0))
        self.assertIsInstance(payload["handle"], RewriteHandle)

    def test_simulations_publish_no_events(self) -> None:
        bus = EventBus()
        events = []
        bus.subscribe("rewrite.applied", events.append)
        self.ct(f"""
            {lst("a")} {lst("b")} ct-rewrite-add-grammar drop
            "grammar" {lst("a", "a")} ct-rewrite-run-on-list
            list-length 2 == static-assert
            dup 0 list-get "b" string-equal? static-assert drop
        """, bus=bus)
        self.assertEqual(events, [])


class RetiredWordTests(RewriteTestBase):
    def test_retired_words_are_gone(self) -> None:
        dictionary = self.compiler().dictionary
        for name in ("ct-add-reader-rewrite", "ct-add-grammar-rewrite-priority", "ct-set-rewrite-saturation",
                     "ct-get-rewrite-specificity", "ct-rewrite-txn-begin", "ct-export-rewrite-pack",
                     "ct-set-pattern-group-active", "ct-set-rewrite-trace", "ct-get-rewrite-profile",
                     "ct-rewrite-compatibility-matrix", "ct-rewrite-generate-fixture", "ct-rebuild-rewrite-index",
                     "ct-set-rewrite-pipeline", "ct-get-reader-rewrite-priority", "ct-get-rewrite-provenance"):
            with self.subTest(name=name):
                self.assertIsNone(dictionary.lookup(name))


if __name__ == "__main__":
    unittest.main()
