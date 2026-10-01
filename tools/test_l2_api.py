#!/usr/bin/env python3
"""Check the public libl2 scalar-evaluation API."""

from __future__ import annotations

import ctypes
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "build" / "libl2.so"
STATUS_OK = 0
STATUS_ERROR = 1


class L2ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not LIBRARY.exists():
            raise RuntimeError(f"missing {LIBRARY}; build the L2 runtime library first")
        cls.lib = ctypes.CDLL(str(LIBRARY))
        cls.lib.l2_eval_ex.argtypes = [ctypes.c_char_p, ctypes.c_long, ctypes.POINTER(ctypes.c_int64)]
        cls.lib.l2_eval_ex.restype = ctypes.c_int
        cls.lib.l2_eval.argtypes = [ctypes.c_char_p, ctypes.c_long]
        cls.lib.l2_eval.restype = ctypes.c_int

    def evaluate(self, source: bytes) -> tuple[int, int]:
        result = ctypes.c_int64(0)
        status = self.lib.l2_eval_ex(source, len(source), ctypes.byref(result))
        return status, result.value

    def test_returns_negative_and_full_width_integer_results(self) -> None:
        self.assertEqual(self.evaluate(b"-1"), (STATUS_OK, -1))
        self.assertEqual(self.evaluate(b"4294967297"), (STATUS_OK, 4294967297))

    def test_empty_stack_is_success_with_zero_result(self) -> None:
        self.assertEqual(self.evaluate(b"1 drop"), (STATUS_OK, 0))

    def test_invalid_and_non_integer_results_return_error(self) -> None:
        self.assertEqual(self.lib.l2_eval_ex(None, 0, ctypes.byref(ctypes.c_int64())), STATUS_ERROR)
        self.assertEqual(self.evaluate(b'"text"'), (STATUS_ERROR, 0))

    def test_legacy_api_keeps_int_width_behavior(self) -> None:
        source = b"4294967297"
        self.assertEqual(self.lib.l2_eval(source, len(source)), 1)


if __name__ == "__main__":
    unittest.main()