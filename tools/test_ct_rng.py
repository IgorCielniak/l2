#!/usr/bin/env python3
"""Contract tests for the deterministic compile-time RNG
(ct-rng-seed/-int/-range) and the retired ct-call policy words.

Expected values are computed with this interpreter's `random.Random`, so the
tests check the contract (seeding, intervals, stream order) without pinning
sequences that may differ across Python versions."""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import ParseError

DEFAULT_SEED = 0x1A2B3C4D


class RngTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = l2_main.Compiler()
        self.vm = self.compiler.parser.compile_time_vm

    def call(self, name: str, *args):
        self.vm.stack = list(args)
        self.compiler.dictionary.lookup(name).compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def test_fresh_compilation_starts_from_the_default_seed(self) -> None:
        reference = random.Random(DEFAULT_SEED)
        self.assertEqual([self.call("ct-rng-int", 1000)[0] for _ in range(5)],
                         [reference.randrange(1000) for _ in range(5)])

    def test_reseeding_repeats_the_sequence(self) -> None:
        self.call("ct-rng-seed", 42)
        first = [self.call("ct-rng-range", 1, 6)[0] for _ in range(8)]
        self.call("ct-rng-seed", 42)
        self.assertEqual([self.call("ct-rng-range", 1, 6)[0] for _ in range(8)], first)
        reference = random.Random(42)
        self.assertEqual(first, [reference.randint(1, 6) for _ in range(8)])

    def test_seed_is_reduced_modulo_2_64(self) -> None:
        self.call("ct-rng-seed", -1)
        negative = [self.call("ct-rng-int", 100)[0] for _ in range(4)]
        self.call("ct-rng-seed", (1 << 64) - 1)
        self.assertEqual([self.call("ct-rng-int", 100)[0] for _ in range(4)], negative)

    def test_intervals(self) -> None:
        for _ in range(200):
            self.assertIn(self.call("ct-rng-int", 3)[0], (0, 1, 2))
            self.assertIn(self.call("ct-rng-range", -2, 2)[0], (-2, -1, 0, 1, 2))
        self.assertEqual(self.call("ct-rng-int", 1), [0])
        self.assertEqual(self.call("ct-rng-range", 7, 7), [7])

    def test_invalid_arguments(self) -> None:
        cases = [
            ("ct-rng-int", (0,), "ct-argument-invalid"),
            ("ct-rng-int", (-5,), "ct-argument-invalid"),
            ("ct-rng-range", (5, 4), "ct-argument-invalid"),
            ("ct-rng-int", (True,), "ct-argument-type"),
            ("ct-rng-seed", (False,), "ct-argument-type"),
            ("ct-rng-range", (True, 3), "ct-argument-type"),
            ("ct-rng-range", (1, 2.5), "ct-argument-type"),
            ("ct-rng-seed", ("7",), "ct-argument-type"),
        ]
        for name, args, code in cases:
            with self.subTest(word=name, args=args):
                with self.assertRaises(ParseError) as caught:
                    self.call(name, *args)
                self.assertEqual(caught.exception.code, code)

    def test_compilations_have_independent_streams(self) -> None:
        self.call("ct-rng-seed", 9)
        self.call("ct-rng-int", 10)
        other = l2_main.Compiler()
        vm = other.parser.compile_time_vm
        vm.stack = [1000]
        other.dictionary.lookup("ct-rng-int").compile_time_intrinsic(vm)
        self.assertEqual(vm.stack, [random.Random(DEFAULT_SEED).randrange(1000)])


class RetiredWordTests(unittest.TestCase):
    def test_ct_call_policy_words_are_gone(self) -> None:
        dictionary = l2_main.Compiler().dictionary
        names = [f"ct-{verb}-ct-call-{noun}" for verb, noun in (
            ("set", "contract"), ("get", "contract"), ("set", "exception-policy"), ("get", "exception-policy"),
            ("set", "sandbox-mode"), ("get", "sandbox-mode"), ("set", "sandbox-allowlist"),
            ("get", "sandbox-allowlist"), ("set", "memo"), ("get", "memo"), ("clear", "memo"), ("get", "memo-size"),
            ("set", "side-effects"), ("get", "side-effects"), ("get", "side-effect-log"),
            ("clear", "side-effect-log"), ("set", "recursion-limit"), ("get", "recursion-limit"),
            ("set", "timeout-ms"), ("get", "timeout-ms"))]
        self.assertEqual(len(names), 20)
        for name in names + ["ct-ctrand-seed", "ct-ctrand-int", "ct-ctrand-range"]:
            with self.subTest(name=name):
                self.assertIsNone(dictionary.lookup(name))
        self.assertNotIn("ct-call-config-invalid", l2_main._DIAGNOSTIC_CODES)


if __name__ == "__main__":
    unittest.main()
