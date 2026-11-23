#!/usr/bin/env python3
"""Guard: CT lists stay Python lists, because real compiles only pop
from the front of short lists.  This test keeps the front-pop cost of a
large list bounded, so a change that makes it much slower shows up here."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main


class ListFrontPopGuard(unittest.TestCase):
    def test_front_pop_of_large_list_is_bounded(self) -> None:
        compiler = l2_main.Compiler()
        vm = compiler.parser.compile_time_vm
        word = compiler.dictionary.lookup("list-pop-front")
        count = 20_000
        vm.stack = [list(range(count))]
        started = time.perf_counter()
        for expected in range(count):
            word.compile_time_intrinsic(vm)
            found = vm.stack.pop()
            value = vm.stack.pop()
            self.assertEqual((found, value), (1, expected))
        elapsed = time.perf_counter() - started
        self.assertEqual(vm.stack, [[]])
        # ~20k VM-level pops; generous bound for slow CI machines.
        self.assertLess(elapsed, 2.0, f"popping {count} items took {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
