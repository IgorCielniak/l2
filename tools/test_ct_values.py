#!/usr/bin/env python3
"""Contract tests for the CT data and value words: nil, lists, maps, strings, scalars.

Each test sets up the compile-time VM stack, runs one built-in word through
its registered compile-time intrinsic, and checks the resulting stack (or
that a CT error is raised).  Exact error text is not part of the contract,
so error tests only check that the error is raised.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import ParseError, Token


def tok(lexeme: str, line: int = 1) -> Token:
    return Token(lexeme=lexeme, line=line, column=1, start=0, end=len(lexeme))


class CtValueTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compiler = l2_main.Compiler()
        cls.vm = cls.compiler.parser.compile_time_vm

    def run_word(self, name: str, *args):
        word = self.compiler.dictionary.lookup(name)
        self.assertIsNotNone(word, f"{name} is not registered")
        self.assertIsNotNone(word.compile_time_intrinsic, f"{name} has no CT intrinsic")
        self.vm.stack = list(args)
        word.compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def assertCtError(self, name: str, *args) -> None:
        with self.assertRaises(ParseError):
            self.run_word(name, *args)


class RegistrationTests(CtValueTestBase):
    def test_renamed_and_removed_words(self) -> None:
        lookup = self.compiler.dictionary.lookup
        for name in ("map-merge", "string-equal?", "try-string>int", "list-deep-clone"):
            self.assertIsNotNone(lookup(name), name)
        for name in ("map-update", "string=", "string>number", "list-join"):
            self.assertIsNone(lookup(name), f"{name} should be retired")


class ScalarTests(CtValueTestBase):
    def test_nil(self) -> None:
        self.assertEqual(self.run_word("nil"), [None])
        self.assertEqual(self.run_word("nil?", None), [1])
        self.assertEqual(self.run_word("nil?", 0), [0])

    def test_identifier_rule(self) -> None:
        for text in ("foo", "_x", "a1", "größe", "→", "Ünder_9"):
            self.assertEqual(self.run_word("identifier?", text), [1], text)
        for text in ("", "1abc", "a-b", "a b", "a.b"):
            self.assertEqual(self.run_word("identifier?", text), [0], text)
        self.assertEqual(self.run_word("identifier?", tok("ok_name")), [1])
        self.assertEqual(self.run_word("identifier?", 42), [0])

    def test_int_to_string(self) -> None:
        self.assertEqual(self.run_word("int>string", -42), ["-42"])
        self.assertEqual(self.run_word("int>string", True), ["1"])
        self.assertEqual(self.run_word("int>string", False), ["0"])
        self.assertCtError("int>string", 3.0)
        self.assertCtError("int>string", "3")


class ListReadTests(CtValueTestBase):
    def test_reads_consume_the_list(self) -> None:
        lst = [1, 2, 3]
        self.assertEqual(self.run_word("list-get", lst, 1), [2])
        self.assertEqual(self.run_word("list-get", lst, -1), [3])
        self.assertEqual(self.run_word("list-last", lst), [3])
        self.assertEqual(self.run_word("list-peek-front", lst), [1])
        self.assertEqual(self.run_word("list-length", lst), [3])
        self.assertEqual(self.run_word("list-empty?", []), [1])

    def test_checked_reads(self) -> None:
        self.assertCtError("list-get", [1, 2], 2)
        self.assertCtError("list-get", [1, 2], -3)
        self.assertCtError("list-last", [])
        self.assertCtError("list-peek-front", [])

    def test_index_must_be_integer_not_boolean(self) -> None:
        self.assertCtError("list-get", [1, 2], True)
        self.assertCtError("list-get", [1, 2], "0")
        self.assertCtError("list-slice", [1, 2], False, 1)

    def test_find_and_contains(self) -> None:
        self.assertEqual(self.run_word("list-find", ["a", "b", "b"], "b"), [1, 1])
        self.assertEqual(self.run_word("list-find", ["a"], "z"), [-1, 0])
        self.assertEqual(self.run_word("list-contains?", ["a"], "a"), [1])
        self.assertEqual(self.run_word("list-contains?", ["a"], "z"), [0])

    def test_structural_equality(self) -> None:
        self.assertEqual(self.run_word("list-find", [[1, [2]]], [1, [2]]), [0, 1])
        left = {"a": 1, "b": 2}
        right = {"b": 2, "a": 1}  # insertion order is ignored
        self.assertEqual(self.run_word("list-contains?", [left], right), [1])
        self.assertEqual(self.run_word("list-contains?", [1], True), [1])  # numeric equivalence
        self.assertEqual(self.run_word("list-contains?", [1], "1"), [0])
        self.assertEqual(self.run_word("list-contains?", [tok("x", line=1)], tok("x", line=9)), [1])
        self.assertEqual(self.run_word("list-contains?", [tok("x")], "x"), [0])

    def test_equality_is_cycle_safe(self) -> None:
        a: list = [1]
        a.append(a)
        b: list = [1]
        b.append(b)
        self.assertEqual(self.run_word("list-contains?", [a], b), [1])

    def test_opaque_values_compare_by_identity(self) -> None:
        marker = object()
        self.assertEqual(self.run_word("list-contains?", [marker], marker), [1])
        self.assertEqual(self.run_word("list-contains?", [object()], object()), [0])

    def test_slice_python_bounds(self) -> None:
        lst = [0, 1, 2, 3, 4]
        self.assertEqual(self.run_word("list-slice", lst, 1, 3), [[1, 2]])
        self.assertEqual(self.run_word("list-slice", lst, -2, 99), [[3, 4]])
        self.assertEqual(self.run_word("list-slice", lst, 3, 1), [[]])


class ListMutationTests(CtValueTestBase):
    def test_mutations_return_the_list(self) -> None:
        lst: list = []
        self.assertEqual(self.run_word("list-append", lst, 1), [[1]])
        self.assertEqual(self.run_word("list-push-front", lst, 0), [[0, 1]])
        self.assertEqual(self.run_word("list-set", lst, -1, 9), [[0, 9]])
        self.assertEqual(self.run_word("list-reverse", lst), [[9, 0]])
        self.assertEqual(self.run_word("list-clear", lst), [[]])

    def test_pop_shapes(self) -> None:
        self.assertEqual(self.run_word("list-pop", [1, 2]), [[1], 2, 1])
        self.assertEqual(self.run_word("list-pop-front", [1, 2]), [[2], 1, 1])
        self.assertEqual(self.run_word("list-pop", []), [[], None, 0])
        self.assertEqual(self.run_word("list-pop-front", []), [[], None, 0])

    def test_remove(self) -> None:
        self.assertEqual(self.run_word("list-remove", ["a", "b"], 0), [["b"], "a"])
        self.assertCtError("list-remove", ["a"], 1)

    def test_insert_bounds(self) -> None:
        self.assertEqual(self.run_word("list-insert", [1, 2], 2, 3), [[1, 2, 3]])
        self.assertEqual(self.run_word("list-insert", [1, 2], -1, 9), [[1, 9, 2]])
        self.assertEqual(self.run_word("list-insert", [1, 2], -2, 0), [[0, 1, 2]])
        self.assertCtError("list-insert", [1, 2, 3, 4, 5], 7, 0)
        self.assertCtError("list-insert", [1, 2], -3, 0)

    def test_invalid_index_does_not_mutate(self) -> None:
        lst = [1, 2]
        self.assertCtError("list-set", lst, 5, 0)
        self.assertCtError("list-insert", lst, 9, 0)
        self.assertEqual(lst, [1, 2])

    def test_extend_with_itself_appends_one_snapshot(self) -> None:
        lst = [1, 2]
        self.assertEqual(self.run_word("list-extend", lst, lst), [[1, 2, 1, 2]])

    def test_type_errors(self) -> None:
        self.assertCtError("list-append", {}, 1)
        self.assertCtError("list-length", "abc")


class CloneTests(CtValueTestBase):
    def test_clone_is_shallow(self) -> None:
        inner = [1]
        out = self.run_word("list-clone", [inner])[0]
        self.assertIs(out[0], inner)

    def test_deep_clone_isolates_and_preserves_sharing(self) -> None:
        shared = [1]
        nested_map = {"k": shared}
        src = [shared, shared, nested_map]
        out = self.run_word("list-deep-clone", src)[0]
        self.assertEqual(out, [[1], [1], {"k": [1]}])
        self.assertIsNot(out[0], shared)
        self.assertIs(out[0], out[1])          # shared reference preserved
        self.assertIs(out[2]["k"], out[0])     # sharing across map/list preserved
        out[0].append(2)
        self.assertEqual(shared, [1])          # original untouched

    def test_deep_clone_preserves_cycles(self) -> None:
        cyc: list = []
        cyc.append(cyc)
        out = self.run_word("list-deep-clone", cyc)[0]
        self.assertIs(out[0], out)
        self.assertIsNot(out, cyc)

    def test_deep_clone_copies_immutable_maps(self) -> None:
        from types import MappingProxyType
        frozen = MappingProxyType({"a": 1})
        out = self.run_word("list-deep-clone", [frozen])[0]
        self.assertIsInstance(out[0], dict)
        self.assertEqual(out[0], {"a": 1})


class MapTests(CtValueTestBase):
    def test_get_found_flag_and_present_nil(self) -> None:
        m = {"a": 1, "n": None}
        self.assertEqual(self.run_word("map-get", m, "a"), [1, 1])
        self.assertEqual(self.run_word("map-get", m, "n"), [None, 1])
        self.assertEqual(self.run_word("map-get", m, "z"), [None, 0])
        self.assertEqual(self.run_word("map-has?", m, "a"), [1])
        self.assertEqual(self.run_word("map-has?", m, "z"), [0])

    def test_reads_consume_the_map(self) -> None:
        m = {"a": 1}
        self.assertEqual(self.run_word("map-keys", m), [["a"]])
        self.assertEqual(self.run_word("map-values", m), [[1]])
        self.assertEqual(self.run_word("map-length", m), [1])
        self.assertEqual(self.run_word("map-empty?", {}), [1])

    def test_mutations(self) -> None:
        m: dict = {}
        self.assertEqual(self.run_word("map-set", m, "a", 1), [{"a": 1}])
        self.assertEqual(self.run_word("map-delete", m, "a"), [{}, 1])
        self.assertEqual(self.run_word("map-delete", m, "a"), [{}, 0])
        self.assertEqual(self.run_word("map-clear", {"x": 1}), [{}])

    def test_supported_keys(self) -> None:
        for key in ("s", 1, True, None):
            self.assertEqual(self.run_word("map-set", {}, key, "v"), [{key: "v"}])
        for key in (1.5, [1], {"a": 1}, tok("x")):
            self.assertCtError("map-set", {}, key, "v")
            self.assertCtError("map-get", {}, key)

    def test_bool_int_key_equivalence_keeps_representative(self) -> None:
        m = {1: "one"}
        out = self.run_word("map-set", m, True, "uno")[0]
        self.assertEqual(list(out.items()), [(1, "uno")])
        self.assertIs(type(list(out.keys())[0]), int)
        self.assertEqual(self.run_word("map-get", {"1": "s"}, 1), [None, 0])

    def test_set_keeps_insertion_position(self) -> None:
        m = {"a": 1, "b": 2}
        out = self.run_word("map-set", m, "a", 9)[0]
        self.assertEqual(list(out.keys()), ["a", "b"])

    def test_merge_order_and_precedence(self) -> None:
        target = {"a": 1, "b": 2}
        source = {"c": 3, "a": 9}
        out = self.run_word("map-merge", target, source)[0]
        self.assertEqual(list(out.items()), [("a", 9), ("b", 2), ("c", 3)])

    def test_merge_with_itself_is_noop(self) -> None:
        m = {"a": 1}
        self.assertEqual(self.run_word("map-merge", m, m), [{"a": 1}])

    def test_merge_validates_all_keys_first(self) -> None:
        from types import MappingProxyType
        target = {"a": 1}
        bad = MappingProxyType({"x": 1, 2.5: 2})
        self.assertCtError("map-merge", target, bad)
        self.assertEqual(target, {"a": 1})

    def test_immutable_maps_read_but_not_write(self) -> None:
        from types import MappingProxyType
        frozen = MappingProxyType({"a": 1})
        self.assertEqual(self.run_word("map-get", frozen, "a"), [1, 1])
        self.assertCtError("map-set", frozen, "a", 2)
        clone = self.run_word("map-clone", frozen)[0]
        self.assertIsInstance(clone, dict)

    def test_nil_key_is_preserved(self) -> None:
        self.assertEqual(self.run_word("map-keys", {None: 1}), [[None]])


class StringTests(CtValueTestBase):
    def test_basic(self) -> None:
        self.assertEqual(self.run_word("string-append", "ab", "cd"), ["abcd"])
        self.assertEqual(self.run_word("string-length", "größe"), [5])  # code points
        self.assertEqual(self.run_word("string-equal?", "a", "a"), [1])
        self.assertEqual(self.run_word("string-equal?", "a", "A"), [0])

    def test_predicates_match_empty(self) -> None:
        self.assertEqual(self.run_word("string-contains?", "abc", ""), [1])
        self.assertEqual(self.run_word("string-starts-with?", "abc", ""), [1])
        self.assertEqual(self.run_word("string-ends-with?", "abc", "bc"), [1])

    def test_split(self) -> None:
        self.assertEqual(self.run_word("string-split", "a,,b", ","), [["a", "", "b"]])
        self.assertEqual(self.run_word("string-split", "", ","), [[""]])
        self.assertCtError("string-split", "abc", "")

    def test_join_strict(self) -> None:
        self.assertEqual(self.run_word("string-join", ["a", "b"], ""), ["ab"])
        self.assertEqual(self.run_word("string-join", [], ","), [""])
        self.assertCtError("string-join", ["a", 1], ",")
        self.assertCtError("string-join", ["a", True], ",")

    def test_replace(self) -> None:
        self.assertEqual(self.run_word("string-replace", "aaa", "aa", "b"), ["ba"])
        self.assertEqual(self.run_word("string-replace", "x_y", "_", ""), ["xy"])
        self.assertCtError("string-replace", "abc", "", "z")

    def test_strip_uses_fixed_whitespace(self) -> None:
        self.assertEqual(self.run_word("string-strip", "　\t hi  \n"), ["hi"])
        self.assertEqual(self.run_word("string-strip", "   "), [""])
        # U+200B ZERO WIDTH SPACE is not in White_Space
        self.assertEqual(self.run_word("string-strip", "​x"), ["​x"])

    def test_case_mapping_is_ascii_only(self) -> None:
        self.assertEqual(self.run_word("string-upper", "MiXeD ß é"), ["MIXED ß é"])
        self.assertEqual(self.run_word("string-lower", "ÀBC"), ["Àbc"])

    def test_strict_string_inputs(self) -> None:
        self.assertCtError("string-append", "a", 1)
        self.assertCtError("string-length", 5)
        self.assertCtError("string-equal?", tok("a"), "a")


class TryStringToIntTests(CtValueTestBase):
    def check(self, text: str, expected) -> None:
        result = self.run_word("try-string>int", text)
        if expected is None:
            self.assertEqual(result, [0, 0], text)
        else:
            self.assertEqual(result, [expected, 1], text)

    def test_valid(self) -> None:
        for text, value in [
            ("0", 0), ("42", 42), ("-7", -7), ("+7", 7), ("007", 7),
            ("0x1F", 31), ("0X1f", 31), ("-0x10", -16), ("0b101", 5),
            ("0o17", 15), ("1_000", 1000), ("0xdead_beef", 0xDEADBEEF),
            ("  12 ", 12), ("　-3\t", -3),
            ("123456789012345678901234567890", 123456789012345678901234567890),
        ]:
            self.check(text, value)

    def test_invalid(self) -> None:
        for text in [
            "", "  ", "abc", "1.5", "1e5", "nan", "inf", "+-1", "--1", "0x",
            "0x-5", "_1", "1_", "1__0", "0x_1", "1 2", "0b102", "0o8",
            "٣",  # Arabic-Indic digit three: not ASCII
        ]:
            self.check(text, None)


if __name__ == "__main__":
    unittest.main()
