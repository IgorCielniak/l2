#!/usr/bin/env python3
"""Contract tests for language packs and token hooks:
pack lifecycle and activation gating, metadata, status, owned candidates
(adoption, replacement, priority, unregistration), owned rewrites, the
token-hook chain (ordering, pinning, results, failures, injection order),
the global hook, and retired words."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, EventBus, ParseError, TokenHookHandle


class PackTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-packs-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str, *, expect_error: bool = False, bus: EventBus = None):
        path = self.dir / "p.sl"
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

    def ct(self, body: str, *, expect_error: bool = False, before: str = "", after: str = ""):
        return self.compile(f"""
            import stdlib.sl
            {before}
            word check
            {body}
            end
            compile-time check
            {after}
            word main 0 end
        """, expect_error=expect_error)

    def error_codes(self, compiler):
        return [d.code for d in compiler.parser.diagnostics if d.severity == "error"]

    def diag(self, compiler, code):
        return next(d for d in compiler.parser.diagnostics if d.code == code)


class LifecycleTests(PackTestBase):
    def test_create_activate_deactivate_remove(self) -> None:
        self.ct("""
            "p" ct-lang-create 1 == static-assert
            "p" ct-lang-create 0 == static-assert
            "p" ct-lang-exists? static-assert
            "p" ct-lang-active? 0 == static-assert
            "missing" ct-lang-active? 0 == static-assert
            "missing" ct-lang-exists? 0 == static-assert
            "p" ct-lang-activate 1 == static-assert
            "p" ct-lang-activate 0 == static-assert
            "p" ct-lang-active? static-assert
            "b" ct-lang-create drop
            ct-lang-list dup list-length 2 == static-assert 0 list-get "b" string-equal? static-assert
            ct-lang-list-active dup list-length 1 == static-assert 0 list-get "p" string-equal? static-assert
            "p" ct-lang-deactivate 1 == static-assert
            "p" ct-lang-deactivate 0 == static-assert
            "p" ct-lang-remove 1 == static-assert
            "p" ct-lang-remove 0 == static-assert
            "missing" ct-lang-status 0 == static-assert nil? static-assert
        """)

    def test_unknown_and_empty_names(self) -> None:
        cases = [
            ('"nope" ct-lang-activate', "language-pack-unknown"),
            ('"nope" ct-lang-deactivate', "language-pack-unknown"),
            ('"nope" nil ct-lang-set-meta', "language-pack-unknown"),
            ('"nope" "h" ct-lang-add-token-hook', "language-pack-unknown"),
            ('"nope" "w" ct-lang-register-word', "language-pack-unknown"),
            ('"nope" "r" list-new "a" list-append list-new ct-lang-add-grammar-rewrite-named', "language-pack-unknown"),
            ('"" ct-lang-create', "language-pack-invalid"),
            ('"" ct-lang-exists?', "language-pack-invalid"),
            ('"" ct-lang-remove', "language-pack-invalid"),
            ('"p" ct-lang-create drop "p" "" ct-lang-add-token-hook', "language-pack-invalid"),
            ('"p" ct-lang-create drop "p" "" ct-lang-register-word', "language-pack-invalid"),
        ]
        for code, expected in cases:
            with self.subTest(code=code):
                compiler, _ = self.ct(code, expect_error=True)
                self.assertIn(expected, self.error_codes(compiler))
                self.assertEqual(self.diag(compiler, expected).category, "language_pack_error")

    def test_names_are_exact(self) -> None:
        self.ct("""
            " p" ct-lang-create static-assert
            "p" ct-lang-exists? 0 == static-assert
            "P" ct-lang-create static-assert
        """)


class MetadataAndStatusTests(PackTestBase):
    def test_metadata_is_validated_and_detached(self) -> None:
        self.ct("""
            "p" ct-lang-create drop
            "p" ct-lang-get-meta static-assert map-length 0 == static-assert
            map-new "k" list-new 1 list-append map-set >r
            "p" r@ ct-lang-set-meta static-assert
            r> "k" map-get drop 2 list-append drop
            "p" ct-lang-get-meta drop "k" map-get drop list-length 1 == static-assert
            "p" nil ct-lang-set-meta static-assert
            "p" ct-lang-get-meta static-assert map-length 0 == static-assert
            "nope" ct-lang-get-meta 0 == static-assert nil? static-assert
        """)
        compiler, _ = self.ct("""
            "p" ct-lang-create drop
            "p" map-new "tok" 0 ct-parser-peek map-set ct-lang-set-meta
        """, expect_error=True)
        diag = self.diag(compiler, "language-pack-metadata-invalid")
        self.assertEqual(diag.phase, "language-pack-metadata")
        self.assertIn("metadata.tok", diag.message)

    def test_status_shape(self) -> None:
        compiler, _ = self.ct("""
            "p" ct-lang-create drop
            "p" "h1" ct-lang-add-token-hook drop
            "p" "h2" ct-lang-add-token-hook 5 ct-lang-set-token-hook-priority drop
            "p" "r" list-new "aa" list-append list-new "bb" list-append ct-lang-add-reader-rewrite-named drop
            "p" ct-lang-status static-assert
            dup map-keys list-length 8 == static-assert
            dup "token_hooks" map-get drop
              dup 0 list-get "handler" map-get drop "h2" string-equal? static-assert
              dup 0 list-get map-length 5 == static-assert
              0 list-get "active?" map-get drop 0 == static-assert
            dup "reader_rewrites" map-get drop 0 list-get "name" map-get drop "r" string-equal? static-assert
            "words" map-get drop list-length 0 == static-assert
        """)
        status = compiler.parser.lang_status("p")
        self.assertEqual(list(status), ["name", "active", "metadata", "token_hooks", "words", "macros",
                                        "reader_rewrites", "grammar_rewrites"])


class ResourceTests(PackTestBase):
    def test_activation_gates_owned_words_and_macros(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word value 1 end
            priority 5 word value 2 end
            macro seven `7` emit ;
            word setup
              "p" ct-lang-create drop
              "p" "value" ct-lang-register-word static-assert
              "p" "seven" ct-lang-register-macro static-assert
              "p" "seven" ct-lang-register-word 0 == static-assert
              "p" "value" ct-lang-get-resource-priority static-assert 5 == static-assert
            end
            compile-time setup
            word inactive value 1 == static-assert end
            compile-time inactive
            word on "p" ct-lang-activate static-assert end
            compile-time on
            word active value 2 == static-assert end
            compile-time active
            word active_macro seven end
            word off "p" ct-lang-deactivate static-assert end
            compile-time off
            word inactive_again value 1 == static-assert end
            compile-time inactive_again
            word check
              active_macro 7 == static-assert
              "p" ct-lang-status drop dup "words" map-get drop 0 list-get "value" string-equal? static-assert
              "macros" map-get drop 0 list-get "seven" string-equal? static-assert
            end
            compile-time check
            word main 0 end
        """)

    def test_replacement_priority_and_unregistration(self) -> None:
        self.ct("""
            "p" ct-lang-create drop "p" ct-lang-activate drop
            "p" "value" ct-lang-register-word static-assert
            `word value 2 end` emit
        """, before="word value 1 end", after="""
            word step2
              "p" "value" ct-lang-register-word static-assert
              "value" ct-definition-candidates list-length 1 == static-assert
              value 2 == static-assert
              "p" "value" -3 ct-lang-set-resource-priority static-assert
              "p" "value" ct-lang-get-resource-priority drop -3 == static-assert
              "p" "missing" 1 ct-lang-set-resource-priority 0 == static-assert
              "p" "missing" ct-lang-get-resource-priority 0 == static-assert nil? static-assert
              "p" "value" ct-lang-unregister-resource static-assert
              "p" "value" ct-lang-unregister-resource 0 == static-assert
              "value" ct-definition-exists? 0 == static-assert
            end
            compile-time step2
        """)

    def test_generic_mutation_of_owned_candidates_is_rejected(self) -> None:
        compiler, _ = self.ct("""
            "p" ct-lang-create drop "p" ct-lang-activate drop
            "p" "value" ct-lang-register-word drop
            "value" ct-definition-candidates 0 list-get "handle" map-get drop 9 ct-definition-set-priority
        """, before="word value 1 end", expect_error=True)
        self.assertIn("definition-owned", self.error_codes(compiler))

    def test_remove_cleans_up_only_its_own_resources(self) -> None:
        self.ct("""
            "a" ct-lang-create drop "a" ct-lang-activate drop
            "b" ct-lang-create drop "b" ct-lang-activate drop
            "a" "r" list-new "x" list-append list-new "1" list-append ct-lang-add-grammar-rewrite-named drop
            "b" "r" list-new "x" list-append list-new "2" list-append ct-lang-add-grammar-rewrite-named drop
            "a" "h" ct-lang-add-token-hook >r
            "a" ct-lang-remove static-assert
            r> ct-lang-remove-token-hook 0 == static-assert
            ct-rewrite-list-grammar list-length 1 == static-assert
            "grammar" list-new "x" list-append ct-rewrite-run-on-list drop 0 list-get "2" string-equal? static-assert
        """)

    def test_owned_rewrites_follow_activation_and_enabled(self) -> None:
        self.ct("""
            "p" ct-lang-create drop
            "p" "r" list-new "x" list-append list-new "1" list-append ct-lang-add-grammar-rewrite-named >r
            "grammar" list-new "x" list-append ct-rewrite-run-on-list drop 0 list-get "x" string-equal? static-assert
            "p" ct-lang-activate drop
            "grammar" list-new "x" list-append ct-rewrite-run-on-list drop 0 list-get "1" string-equal? static-assert
            r@ 0 ct-rewrite-rule-set-enabled drop
            "p" ct-lang-deactivate drop "p" ct-lang-activate drop
            r> ct-rewrite-rule-get-enabled drop 0 == static-assert
            "grammar" list-new "x" list-append ct-rewrite-run-on-list drop 0 list-get "x" string-equal? static-assert
        """)

    def test_activation_publishes_definition_events(self) -> None:
        bus = EventBus()
        events = []
        bus.subscribe("definition.*", events.append)
        self.compile("""
            import stdlib.sl
            word value 1 end
            word setup
              "p" ct-lang-create drop
              "p" "value" ct-lang-register-word drop
              "p" ct-lang-activate drop
              "p" ct-lang-activate drop
            end
            compile-time setup
            word main 0 end
        """, bus=bus)
        updates = [e for e in events if e["event"] == "definition.candidates.changed"
                   and e["payload"].get("operation") == "update"]
        # adoption, then one activation (the idempotent second one publishes nothing)
        self.assertEqual(len(updates), 2, [e["payload"].get("operation") for e in events])


class TokenHookTests(PackTestBase):
    HOOKS = """
        word first_hook   # tags `tok` tokens; never consumes
          token-lexeme "tok" string-equal? if `1` emit end 0
        end
        word second_hook
          token-lexeme "tok" string-equal? if `2` emit end 0
        end
        word consumer
          token-lexeme "tok" string-equal?
        end
    """

    def test_chain_order_injection_order_and_consumption(self) -> None:
        compiler, _ = self.compile(f"""
            import stdlib.sl
            {self.HOOKS}
            word setup
              "p" ct-lang-create drop "p" ct-lang-activate drop
              "p" "second_hook" ct-lang-add-token-hook drop
              "p" "first_hook" ct-lang-add-token-hook drop
              "p" "consumer" ct-lang-add-token-hook -1 ct-lang-set-token-hook-priority drop
            end
            compile-time setup
            word value tok + end
            word main 0 end
        """)
        # Later registration first on equal priority: first_hook's 1, then
        # second_hook's 2; consumer (priority -1) removes `tok`.
        body = compiler.dictionary.lookup("value").definition.body
        self.assertEqual([node.data for node in body[:2]], [1, 2])

    def test_disabled_hooks_and_inactive_packs_do_not_run(self) -> None:
        compiler, _ = self.compile(f"""
            import stdlib.sl
            {self.HOOKS}
            word setup
              "p" ct-lang-create drop
              "p" "consumer" ct-lang-add-token-hook drop
              "q" ct-lang-create drop "q" ct-lang-activate drop
              "q" "consumer" ct-lang-add-token-hook 0 ct-lang-set-token-hook-enabled static-assert
            end
            compile-time setup
            word tok 5 end
            word value tok end
            word check value 5 == static-assert end
            compile-time check
            word main 0 end
        """)

    def test_hook_failures(self) -> None:
        cases = [
            ("missing_hook", "", "not defined"),
            ("two", "word two drop 1 1 end", "returned [integer, integer]"),
            ("none", "word none drop end", "returned []"),
            ("mac", "macro mac `1` emit ;", "is a macro"),
            ("rt", "word rt drop 0 end runtime-only", "runtime-only"),
        ]
        for name, definition, reason in cases:
            with self.subTest(hook=name):
                compiler, _ = self.compile(f"""
                    import stdlib.sl
                    {definition}
                    word setup
                      "p" ct-lang-create drop "p" ct-lang-activate drop
                      "p" "{name}" ct-lang-add-token-hook drop
                    end
                    compile-time setup
                    word main 0 end
                """, expect_error=True)
                diag = self.diag(compiler, "token-hook-failed")
                self.assertEqual((diag.category, diag.phase), ("lexer_error", "token-hook"))
                self.assertIn(reason, diag.message)
                self.assertIn("pack 'p'", diag.message)

    def test_handle_operations(self) -> None:
        self.ct("""
            "p" ct-lang-create drop
            "p" "h" ct-lang-add-token-hook
            dup 3 ct-lang-set-token-hook-priority static-assert
            dup 0 ct-lang-set-token-hook-enabled static-assert
            dup ct-lang-remove-token-hook static-assert
            dup ct-lang-remove-token-hook 0 == static-assert
            dup 1 ct-lang-set-token-hook-priority 0 == static-assert
            1 ct-lang-set-token-hook-enabled 0 == static-assert
        """)
        for code, expected in (('1 ct-lang-remove-token-hook', "language-pack-handle-invalid"),
                               ('"p" ct-lang-create drop "p" "h" ct-lang-add-token-hook "x" ct-lang-set-token-hook-priority',
                                "language-pack-invalid")):
            with self.subTest(code=code):
                compiler, _ = self.ct(code, expect_error=True)
                self.assertIn(expected, self.error_codes(compiler))
        compiler = l2_main.Compiler()
        vm = compiler.parser.compile_time_vm
        compiler.parser.lang_create("p")
        handle = compiler.parser.lang_add_token_hook("p", "h")
        self.assertIsInstance(handle, TokenHookHandle)
        vm.stack = [handle, True]
        with self.assertRaises(ParseError) as caught:
            compiler.dictionary.lookup("ct-lang-set-token-hook-priority").compile_time_intrinsic(vm)
        self.assertEqual(caught.exception.code, "language-pack-invalid")


class GlobalHookTests(PackTestBase):
    def test_global_hook_binds_a_candidate(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word hook token-lexeme "gone" string-equal? end
            word install "hook" ct-parser-set-token-hook end
            compile-time install
            word redefine `word hook drop 0 end` emit end
            compile-time redefine
            word value 3 gone end
            word stop ct-parser-clear-token-hook ct-parser-clear-token-hook end
            compile-time stop
            word gone 4 end
            word after gone end
            word main 0 end
        """)
        # The bound candidate still runs after `hook` is redefined.
        self.assertEqual(len(compiler.dictionary.lookup("value").definition.body), 1)

    def test_global_hook_rejections(self) -> None:
        for definition, name, reason in (("", "missing", "is not defined"),
                                         ("macro mac `1` emit ;", "mac", "is a macro"),
                                         ("", "list-new", "built-in")):
            with self.subTest(name=name):
                compiler, _ = self.ct(f'"{name}" ct-parser-set-token-hook', before=definition, expect_error=True)
                diag = self.diag(compiler, "token-hook-handler-invalid")
                self.assertIn(reason, diag.message)


class RetiredWordTests(PackTestBase):
    def test_retired_words_are_gone(self) -> None:
        dictionary = l2_main.Compiler().dictionary
        for name in ("set-token-hook", "clear-token-hook", "ct-lang-set-token-hook", "ct-lang-add-validator",
                     "ct-lang-run-validators", "ct-lang-run-active-validators", "ct-lang-set-auto-validate",
                     "ct-lang-get-auto-validate", "ct-lang-meta-get", "ct-lang-meta-set", "ct-lang-active-list"):
            with self.subTest(name=name):
                self.assertIsNone(dictionary.lookup(name))


if __name__ == "__main__":
    unittest.main()
