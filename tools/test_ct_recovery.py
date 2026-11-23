#!/usr/bin/env python3
"""Contract tests for error publication and recovery:
compiler.error.<category> and compiler.error events, recoverable dispatch,
retry chains and limits, terminal errors, recovery lifecycle events,
history ordering, recovery words, --no-events, and the abort threshold."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import EventBus, ParseError, CompileError

LIFECYCLE = ("compiler.error", "compile.recovery")


class RecoveryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-recovery-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str):
        """Compile in-process; returns (compiler, bus, exception-or-None)."""
        path = self.dir / "r.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        bus = EventBus(4096)
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"], event_bus=bus)
        error = None
        try:
            compiler.compile_file(path)
        except (ParseError, CompileError) as exc:
            error = exc
        return compiler, bus, error

    def lifecycle(self, bus: EventBus):
        return [row for row in bus.recent() if row["event"].startswith(LIFECYCLE)]


class PublicationTests(RecoveryTestBase):
    def test_category_event_then_general_event(self) -> None:
        compiler, bus, error = self.compile("""
            import stdlib.sl
            word main nosuchword 0 end
        """)
        self.assertIsNotNone(error)
        rows = self.lifecycle(bus)
        self.assertEqual([row["event"] for row in rows], ["compiler.error.emission_error", "compiler.error"])
        first, second = (row["payload"] for row in rows)
        self.assertIs(first["diagnostic"], second["diagnostic"])
        self.assertEqual(first["diagnostic"].code, "emission-unknown-word")
        self.assertTrue(first["recoverable"])
        self.assertEqual(first["recovery"], {"attempt": 1, "limit": 8})
        errors = [d for d in compiler.parser.diagnostics if d.severity == "error"]
        self.assertEqual([d.code for d in errors], ["emission-unknown-word"])

    def test_unrecoverable_error_is_published_once(self) -> None:
        compiler, bus, error = self.compile("""
            import stdlib.sl
            word main 0
        """)
        self.assertIsNotNone(error)
        rows = [row for row in self.lifecycle(bus) if row["event"] == "compiler.error"]
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload"]
        self.assertEqual((payload["diagnostic"].code, payload["recoverable"]), ("parser-definition-unclosed", False))
        self.assertNotIn("recovery", payload)


class ResumeTests(RecoveryTestBase):
    REPAIR = """
        import stdlib.sl
        word repair
          event-payload dup "recoverable" map-get drop if
            "word helper 42 end" ct-definition-register-source drop
            ct-recovery-resume drop
          end drop
        end on-event compiler.error
    """

    def test_successful_recovery_lifecycle(self) -> None:
        compiler, bus, error = self.compile(self.REPAIR + """
        word main helper drop 0 end
        """)
        self.assertIsNone(error)
        rows = self.lifecycle(bus)
        self.assertEqual([row["event"] for row in rows], [
            "compiler.error.emission_error", "compiler.error", "compile.recovery.start", "compile.recovery.end"])
        end = rows[-1]["payload"]
        self.assertEqual(end["outcome"], "success")
        self.assertEqual(end["recovery"], {"attempt": 1, "limit": 8})
        self.assertNotIn("terminal-diagnostic", end)
        # The recovered attempt stays in history but is not reported.
        self.assertEqual([d.code for d in compiler.parser.diagnostics], ["emission-unknown-word"])
        self.assertEqual(compiler.parser.reportable_diagnostics(), [])

    def test_parse_stage_recovery(self) -> None:
        compiler, bus, error = self.compile(self.REPAIR + """
        word trigger helper drop end
        compile-time trigger
        word main 0 end
        """)
        self.assertIsNone(error)
        events = [row["event"] for row in self.lifecycle(bus)]
        self.assertEqual(events[-1], "compile.recovery.end")


class ChainTests(RecoveryTestBase):
    def test_terminal_after_limit(self) -> None:
        compiler, bus, error = self.compile("""
            import stdlib.sl
            word retry
              event-payload "recoverable" map-get drop if ct-recovery-resume drop end
            end on-event compiler.error
            word setup 3 ct-recovery-set-retry-limit drop end
            compile-time setup
            word trigger missing_word end
            compile-time trigger
            word main 0 end
        """)
        self.assertIsNotNone(error)
        general = [row["payload"] for row in self.lifecycle(bus) if row["event"] == "compiler.error"]
        self.assertEqual([p.get("recovery", {}).get("attempt") for p in general], [1, 2, 3, None])
        terminal = general[-1]
        self.assertEqual((terminal["recoverable"], terminal["terminal"]), (False, True))
        end = [row["payload"] for row in self.lifecycle(bus) if row["event"] == "compile.recovery.end"]
        self.assertEqual(len(end), 1)
        self.assertEqual(end[0]["outcome"], "error")
        self.assertIs(end[0]["terminal-diagnostic"], terminal["diagnostic"])
        starts = [row for row in self.lifecycle(bus) if row["event"] == "compile.recovery.start"]
        self.assertEqual(len(starts), 1)
        # Every attempt is retained; only the terminal one is reported.
        self.assertEqual(len([d for d in compiler.parser.diagnostics if d.severity == "error"]), 4)
        reported = [d for d in compiler.parser.reportable_diagnostics() if d.severity == "error"]
        self.assertEqual(reported, [terminal["diagnostic"]])

    def test_limit_is_snapshotted_per_chain(self) -> None:
        compiler, bus, error = self.compile("""
            import stdlib.sl
            word retry
              event-payload dup "recoverable" map-get drop if
                99 ct-recovery-set-retry-limit drop
                ct-recovery-resume drop
              end drop
            end on-event compiler.error
            word setup 2 ct-recovery-set-retry-limit drop end
            compile-time setup
            word trigger missing_word end
            compile-time trigger
            word main 0 end
        """)
        general = [row["payload"] for row in self.lifecycle(bus) if row["event"] == "compiler.error"]
        self.assertEqual([p.get("recovery", {}).get("limit") for p in general], [2, 2, None])

    def test_history_is_appended_after_dispatch(self) -> None:
        compiler, bus, error = self.compile("""
            import stdlib.sl
            word count
              event-payload "recoverable" map-get drop if
                ct-diagnostic-list list-length puti cr
              end
            end on-event compiler.error
            word main nosuchword 0 end
        """)
        self.assertIsNotNone(error)
        self.assertEqual(len(compiler.parser.diagnostics), 1)


class RecoveryWordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = l2_main.Compiler()
        self.parser = self.compiler.parser
        self.vm = self.parser.compile_time_vm

    def run_word(self, name: str, *args):
        word = self.compiler.dictionary.lookup(name)
        self.assertIsNotNone(word, name)
        self.vm.stack = list(args)
        word.compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def assertCode(self, code: str, name: str, *args) -> None:
        with self.assertRaises(ParseError) as caught:
            self.run_word(name, *args)
        self.assertEqual(caught.exception.code, code)

    def test_retired_names(self) -> None:
        for name in ("ct-error-restore-points", "ct-error-restore-point", "ct-error-jump", "ct-error-resume",
                     "ct-error-get-retry-limit", "ct-error-set-retry-limit"):
            self.assertIsNone(self.compiler.dictionary.lookup(name), name)

    def test_outside_dispatch(self) -> None:
        self.assertCode("recovery-operation-unavailable", "ct-recovery-resume")
        point = self.parser._add_error_restore_point("parse", 0, None, None)
        self.assertCode("recovery-operation-unavailable", "ct-recovery-jump", point)
        self.assertEqual(self.run_word("ct-recovery-get-restore-point", point)[0]["id"], point)
        self.assertCode("recovery-restore-point-invalid", "ct-recovery-get-restore-point", 999999)
        self.assertEqual(len(self.run_word("ct-recovery-list-restore-points")[0]), 1)

    def test_jump_is_atomic(self) -> None:
        good = self.parser._add_error_restore_point("parse", 0, None, None)
        other_stage = self.parser._add_error_restore_point("emit", 0, None, None)
        self.parser._active_error_recovery = {"stage": "parse", "restore_point_id": None, "resume": False}
        self.run_word("ct-recovery-jump", good)
        self.assertCode("recovery-restore-point-invalid", "ct-recovery-jump", other_stage)
        self.assertCode("recovery-restore-point-invalid", "ct-recovery-jump", 123456)
        self.assertEqual(self.parser._active_error_recovery["restore_point_id"], good)
        self.assertEqual(self.run_word("ct-recovery-resume"), [1])
        self.assertTrue(self.parser._active_error_recovery["resume"])

    def test_retry_limit(self) -> None:
        self.assertEqual(self.run_word("ct-recovery-get-retry-limit"), [8])
        self.assertEqual(self.run_word("ct-recovery-set-retry-limit", 5000), [8])
        for bad in (0, -1, True, "3"):
            self.assertCode("ct-argument-invalid", "ct-recovery-set-retry-limit", bad)
        self.assertEqual(self.run_word("ct-recovery-get-retry-limit"), [5000])


class CliTests(RecoveryTestBase):
    def cli(self, source: str, *extra: str) -> subprocess.CompletedProcess:
        path = self.dir / "c.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact", "--no-daemon", *extra],
            cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
        )

    def test_no_events_still_fails_normally(self) -> None:
        proc = self.cli("""
            import stdlib.sl
            word main nosuchword 0 end
        """, "--no-events")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error[emission-unknown-word]", proc.stderr)

    def test_recovered_error_is_not_printed(self) -> None:
        proc = self.cli(ResumeTests.REPAIR + """
        word main helper drop 0 end
        """)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("error", proc.stderr)

    def test_abort_threshold(self) -> None:
        body = "\n".join("end" for _ in range(45))  # recovery skips every other end
        proc = self.cli(f"import stdlib.sl\n{body}\nword main 0 end\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("20 error(s)", proc.stderr)


if __name__ == "__main__":
    unittest.main()
