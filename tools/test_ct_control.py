#!/usr/bin/env python3
"""Contract tests for control frames (found-aware get,
shallow snapshots, underflow), canonical and atomic close ops, ct-op-emit by
reference, validated labels, block openers, strict control overrides, and
ct-parser-last-line."""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, ParseError


class ControlTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-control-")
        self.dir = Path(self._tmp.name)
        self.compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"])
        self.vm = self.compiler.parser.compile_time_vm

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, name: str, *args):
        self.vm.stack = list(args)
        self.compiler.dictionary.lookup(name).compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def compile(self, source: str, *, expect_error: bool = False):
        path = self.dir / "c.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"])
        error = None
        try:
            emission = compiler.compile_file(path)
        except (ParseError, CompileError) as exc:
            error, emission = exc, None
        if expect_error:
            self.assertIsNotNone(error)
        else:
            self.assertIsNone(error, error)
        return compiler, emission

    def codes(self, compiler):
        return [d.code for d in compiler.parser.diagnostics if d.severity == "error"]


class FrameTests(ControlTestBase):
    def test_get_distinguishes_absent_from_nil(self) -> None:
        frame = {"type": "x", "present": None}
        self.assertEqual(self.call("ct-control-get", frame, "present"), [None, 1])
        self.assertEqual(self.call("ct-control-get", frame, "absent"), [None, 0])
        self.assertEqual(self.call("ct-control-get", frame, "type"), ["x", 1])

    def test_set_mutates_and_returns_its_target(self) -> None:
        frame = {"type": "x"}
        result = self.call("ct-control-set", frame, "k", None)
        self.assertIs(result[0], frame)
        self.assertEqual(frame, {"type": "x", "k": None})

    def test_snapshots_are_shallow(self) -> None:
        nested = [1]
        frame = {"type": "x", "nested": nested}
        self.call("ct-control-push", frame)
        frame["type"] = "changed"                      # top-level edit after push: not seen
        peeked = self.call("ct-control-peek")[0]
        self.assertEqual(peeked["type"], "x")
        peeked["type"] = "peek-edit"                   # edit of a peeked copy: not seen
        nested.append(2)                               # nested edit: shared
        popped = self.call("ct-control-pop")[0]
        self.assertEqual(popped["type"], "x")
        self.assertIs(popped["nested"], nested)
        self.assertEqual(popped["nested"], [1, 2])

    def test_peek_and_pop_on_empty_stack(self) -> None:
        self.assertEqual(self.call("ct-control-peek"), [None])
        with self.assertRaises(ParseError) as caught:
            self.call("ct-control-pop")
        self.assertEqual(caught.exception.code, "parser-control-underflow")
        self.assertEqual(self.compiler.parser.control_stack, [])

    def test_argument_types(self) -> None:
        for name, args in (("ct-control-get", ([], "k")), ("ct-control-get", ({}, 1)),
                           ("ct-control-set", ("frame", "k", 1)), ("ct-control-push", (None,)),
                           ("ct-control-frame-new", (1,))):
            with self.subTest(word=name, args=args):
                with self.assertRaises(ParseError) as caught:
                    self.call(name, *args)
                self.assertEqual(caught.exception.code, "ct-argument-type")
                self.assertEqual(caught.exception.diagnostic.category, "ct_execution_error")


class CloseOpTests(ControlTestBase):
    def test_add_close_op_is_canonical_and_validates_existing_list(self) -> None:
        frame = {"type": "x"}
        data = {"k": 1}
        self.call("ct-control-add-close-op", frame, "label", data)
        self.assertEqual(frame["close_ops"], [{"op": "label", "data": data}])
        self.assertIs(frame["close_ops"][0]["data"], data)
        for bad in ([{"op": "label"}], [{"op": "", "data": 1}], ["label"], [{"op": "a", "data": 1, "x": 2}], "x"):
            with self.subTest(close_ops=bad):
                frame = {"type": "x", "close_ops": bad}
                snapshot = repr(bad)
                with self.assertRaises(ParseError):
                    self.call("ct-control-add-close-op", frame, "label", 1)
                self.assertEqual(repr(frame["close_ops"]), snapshot)
        with self.assertRaises(ParseError):
            self.call("ct-control-add-close-op", {"type": "x"}, "", 1)

    def test_end_is_atomic(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word open-bad
              "custom" ct-control-frame-new
              "label" "L_first" ct-control-add-close-op
              dup "close_ops" ct-control-get drop "not-a-descriptor" list-append drop
              ct-control-push
            end
            immediate compile-only
            word main open-bad end 0 end
        """, expect_error=True)
        diag = next(d for d in compiler.parser.diagnostics if d.code == "parser-control-frame-invalid")
        self.assertEqual(diag.category, "parser_error")
        self.assertIn("close_ops[1]", diag.message)

    def test_nil_close_ops_closes_cleanly_and_data_is_not_cloned(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word open-plain "plain" ct-control-frame-new ct-control-push end
            immediate compile-only
            word open-data
              "d" ct-control-frame-new
              list-new >r
              "data" r@ ct-control-add-close-op ct-control-push
              r> 5 list-append drop
            end
            immediate compile-only
            word a open-plain end end
            word b open-data end end
            word main 0 end
        """)
        self.assertEqual(compiler.dictionary.lookup("a").definition.body, [])
        node = compiler.dictionary.lookup("b").definition.body[0]
        self.assertEqual((node.op, node.data), ("data", [5]))

    def test_unsupported_close_op_fails_at_emission(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word open-weird "w" ct-control-frame-new "no_such_op" 1 ct-control-add-close-op ct-control-push end
            immediate compile-only
            word main open-weird end 0 end
        """, expect_error=True)
        self.assertEqual(self.codes(compiler)[-1:], ["emission-op-unsupported"])


class OpAndLabelTests(ControlTestBase):
    def test_op_emit_keeps_data_by_reference(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word emit-list list-new dup "data" swap ct-op-emit 7 list-append drop end
            immediate compile-only
            word body emit-list end
            word main 0 end
        """)
        node = compiler.dictionary.lookup("body").definition.body[0]
        self.assertEqual((node.op, node.data), ("data", [7]))

    def test_unsupported_op_fails_at_emission(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word bad "no_such_op" 1 ct-op-emit end
            immediate compile-only
            word main bad 0 end
        """, expect_error=True)
        diag = next(d for d in compiler.parser.diagnostics if d.code == "emission-op-unsupported")
        self.assertEqual(diag.category, "emission_error")
        self.assertIn("'no_such_op'", diag.message)

    def test_label_prefixes(self) -> None:
        first = self.call("ct-label-new", "loop_1")[0]
        second = self.call("ct-label-new", "loop_1")[0]
        self.assertTrue(first.startswith("L_loop_1_"))
        self.assertNotEqual(first, second)
        self.assertTrue(self.call("ct-label-new", "_x")[0].startswith("L__x_"))
        for bad in ("", "1abc", "a-b", "a b", "naïve", "x."):
            with self.subTest(prefix=bad):
                with self.assertRaises(ParseError) as caught:
                    self.call("ct-label-new", bad)
                self.assertEqual(caught.exception.code, "ct-argument-invalid")
        with self.assertRaises(ParseError):
            self.call("ct-label-new", 3)

    def test_last_line(self) -> None:
        self.assertEqual(self.call("ct-parser-last-line"), [0])
        self.compile("""
            import stdlib.sl
            word check ct-parser-last-line 3 == static-assert end
            compile-time check
            word main 0 end
        """)


class OpenerAndOverrideTests(ControlTestBase):
    def test_block_openers(self) -> None:
        self.call("ct-parser-register-block-opener", "unless")
        self.call("ct-parser-register-block-opener", "unless")
        self.assertIn("unless", self.compiler.parser.block_openers)
        self.call("ct-parser-unregister-block-opener", "for")
        self.assertNotIn("for", self.compiler.parser.block_openers)
        with self.assertRaises(ParseError):
            self.call("ct-parser-register-block-opener", "")

    def test_removing_a_builtin_opener_keeps_the_construct(self) -> None:
        self.compile("""
            import stdlib.sl
            word drop-for "for" ct-parser-unregister-block-opener end
            compile-time drop-for
            word count 0 3 for 1 + end end
            word check count 3 == static-assert end
            compile-time check
            word main 0 end
        """)

    def test_override_names_are_restricted(self) -> None:
        for name in ("if", "else", "for", "while", "do"):
            self.call("ct-control-register-override", name)
        self.assertEqual(self.compiler.parser.control_overrides, {"if", "else", "for", "while", "do"})
        for bad in ("begin", "match", "custom", ""):
            with self.subTest(name=bad):
                with self.assertRaises(ParseError) as caught:
                    self.call("ct-control-register-override", bad)
                self.assertEqual(caught.exception.code, "ct-argument-invalid")

    def test_unavailable_override_handler_is_an_error(self) -> None:
        compiler, _ = self.compile("""
            import stdlib.sl
            word reg "while" ct-control-register-override end
            compile-time reg
            word main 0 while 0 do end 0 end
        """, expect_error=True)
        diag = next(d for d in compiler.parser.diagnostics if d.code == "ct-word-unavailable")
        self.assertIn("control override 'while'", diag.message)
        self.assertEqual(diag.category, "ct_execution_error")

    def test_unregistered_name_uses_the_builtin(self) -> None:
        self.compile("""
            import stdlib.sl
            word reg "if" ct-control-register-override "if" ct-control-unregister-override end
            compile-time reg
            word pick 1 if 7 else 8 end end
            word check pick 7 == static-assert end
            compile-time check
            word main 0 end
        """)


if __name__ == "__main__":
    unittest.main()
