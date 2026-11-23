#!/usr/bin/env python3
"""Contract tests for the extensions: ring capacity 0,
isolated tooling observers, recoverable event-handler-failed with the
nested exclusion lineage, definition-owned subscriptions, recoverable
event-stream-output-failed with JSONL bypass, the event-stream controls,
--no-events-json, and the ct-parser-error rename."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, EventBus, ParseError


class BusTests(unittest.TestCase):
    def test_capacity_zero_keeps_dispatch(self) -> None:
        bus = EventBus(0)
        seen = []
        bus.subscribe("*", seen.append)
        bus.add_sink(lambda record: seen.append("sink"))
        bus.publish("e")
        self.assertEqual(bus.recent(), [])
        self.assertEqual(len(seen), 2)
        two = EventBus(2)
        for name in ("a", "b", "c"):
            two.publish(name)
        self.assertEqual([row["event"] for row in two.recent()], ["b", "c"])

    def test_failing_tooling_observer_is_detached(self) -> None:
        bus = EventBus()
        calls = []

        def broken(record):
            calls.append(record["event"])
            raise RuntimeError("observer bug")

        bus.subscribe("*", broken)
        bus.publish("a")
        bus.publish("b")
        self.assertEqual(calls, ["a"])
        self.assertFalse(bus.has_subscribers())


class CompileTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-events-ext-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, source: str, *, expect_error: bool = False, bus: EventBus = None):
        path = self.dir / "e.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"], event_bus=bus or EventBus())
        error = None
        try:
            compiler.compile_file(path)
        except (ParseError, CompileError) as exc:
            error = exc
        if expect_error:
            self.assertIsNotNone(error)
        else:
            self.assertIsNone(error, error)
        return compiler

    def notes(self, compiler):
        return [d.message for d in compiler.parser.diagnostics if d.severity == "note"]

    def errors(self, compiler):
        return [d for d in compiler.parser.reportable_diagnostics() if d.severity == "error"]

    def run_cli(self, source: str, *extra: str) -> subprocess.CompletedProcess:
        path = self.dir / "c.sl"
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return subprocess.run([sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact", "--no-daemon", *extra],
                              cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


class HandlerFailureTests(CompileTestBase):
    def test_resumed_failure_continues_the_dispatch(self) -> None:
        compiler = self.compile("""
            import stdlib.sl
            word bad drop "bad handler" ct-parser-error end
            word good drop "good ran" ct-diagnostic-note end
            word fixer
              event-payload dup "event-handler" map-get static-assert
              "handler" map-get drop "bad" string-equal? static-assert
              "diagnostic" map-get drop diagnostic-code "ct-parser-error-raised" string-equal? static-assert
              ct-recovery-resume drop
            end
            word setup
              "app.evt" "bad" nil ct-event-subscribe drop
              "app.evt" "good" nil ct-event-subscribe drop
              "compiler.error" "fixer" nil ct-event-subscribe drop
              "app.evt" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """)
        self.assertIn("good ran", self.notes(compiler))
        self.assertEqual(self.errors(compiler), [])

    def test_wrapper_error_for_stack_imbalance_is_recoverable(self) -> None:
        compiler = self.compile("""
            import stdlib.sl
            word leaky drop 1 end
            word fixer
              event-payload "diagnostic" map-get drop diagnostic-code
              "event-handler-failed" string-equal? static-assert
              ct-recovery-resume drop
            end
            word setup
              "compiler.error.recovery_error" "fixer" nil ct-event-subscribe drop
              "app.evt" "leaky" nil ct-event-subscribe drop
              "app.evt" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """)
        self.assertEqual(self.errors(compiler), [])

    def test_failing_subscription_is_excluded_from_its_nested_dispatch(self) -> None:
        # `bad` also listens to error events: its first failure reaches its
        # second subscription, whose failure joins the lineage; with both
        # excluded the nested dispatch ends instead of recursing.
        compiler = self.compile("""
            import stdlib.sl
            word bad drop "always fails" ct-parser-error end
            word setup
              "app.evt" "bad" nil ct-event-subscribe drop
              "compiler.error*" "bad" nil ct-event-subscribe drop
              "app.evt" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """, expect_error=True)
        attempts = [d for d in compiler.parser.diagnostics if d.code == "ct-parser-error-raised"]
        self.assertEqual(len(attempts), 2)

    def test_exclusions_unwind_after_recovery(self) -> None:
        compiler = self.compile("""
            import stdlib.sl
            word flaky drop "flaky" ct-parser-error end
            word fixer drop ct-recovery-resume drop end
            word setup
              "app.*" "flaky" nil ct-event-subscribe drop
              "compiler.error" "fixer" nil ct-event-subscribe drop
              "app.one" nil ct-event-emit drop
              "app.two" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """)
        attempts = [d for d in compiler.parser.diagnostics if d.code == "ct-parser-error-raised"]
        self.assertEqual(len(attempts), 2)   # flaky ran (and failed) for both events


class ExecutionEventTests(CompileTestBase):
    def test_wildcard_handler_does_not_reenter_on_its_own_calls(self) -> None:
        compiler = self.compile("""
            import stdlib.sl
            word watcher event-name "app." string-starts-with? if "saw app event" ct-diagnostic-note end end
            word setup "*" "watcher" nil ct-event-subscribe drop "app.evt" nil ct-event-emit drop end
            compile-time setup
            word main 0 end
        """)
        self.assertIn("saw app event", self.notes(compiler))

    def test_other_subscribers_still_see_a_handlers_calls(self) -> None:
        compiler = self.compile("""
            import stdlib.sl
            word marker 0 drop end
            word caller drop marker end
            word counter
              event-payload "word" map-get drop "marker" string-equal? if "marker call seen" ct-diagnostic-note end
            end
            word setup
              "ct.execute.begin" "counter" nil ct-event-subscribe drop
              "app.evt" "caller" nil ct-event-subscribe drop
              "app.evt" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """)
        self.assertIn("marker call seen", self.notes(compiler))


    def test_execution_events_only_when_wanted(self) -> None:
        source = """
            import stdlib.sl
            word watcher drop end
            word setup "{pattern}" "watcher" nil ct-event-subscribe drop end
            compile-time setup
            word later 1 drop end
            compile-time later
            word main 0 end
        """
        quiet = self.compile(source.replace("{pattern}", "compiler.error*"))
        self.assertEqual(quiet.parser.event_bus.recent("ct.execute.*"), [])
        watched = self.compile(source.replace("{pattern}", "ct.execute.end"))
        ends = [row["payload"]["word"] for row in watched.parser.event_bus.recent("ct.execute.end")]
        self.assertIn("later", ends)
        self.assertEqual(watched.parser.event_bus.recent("ct.execute.begin"), [])


class WantsCacheTests(unittest.TestCase):
    def test_cache_follows_every_change(self) -> None:
        bus = EventBus()
        self.assertFalse(bus.wants("a.b"))
        tooling = bus.subscribe("a.*", lambda record: None)
        self.assertTrue(bus.wants("a.b"))
        bus.unsubscribe(tooling)
        self.assertFalse(bus.wants("a.b"))
        handle = bus.ct_subscribe("a.b", "h")
        self.assertTrue(bus.wants("a.b"))
        self.assertFalse(bus.wants("a.c"))
        bus.ct_unsubscribe(handle)
        self.assertFalse(bus.wants("a.b"))
        bus.add_sink(lambda record: None)
        self.assertTrue(bus.wants("a.c"))
        bus.stream_enabled = False
        self.assertFalse(bus.wants("a.c"))
        bus.enabled = False
        bus.subscribe("*", lambda record: None)
        self.assertFalse(bus.wants("a.c"))


class OwnedSubscriptionTests(CompileTestBase):
    def test_owned_subscription_lifecycle(self) -> None:
        bus = EventBus()
        events = []
        bus.subscribe("definition.candidates.changed", events.append)
        compiler = self.compile("""
            import stdlib.sl
            word owner_word 1 end
            word listener drop "heard" ct-diagnostic-note end
            word setup
              "app.evt" "listener" "owner_word" ct-definition-candidates 0 list-get "handle" map-get drop
              ct-event-subscribe drop
              "app.evt" nil ct-event-emit drop
              "owner_word" ct-definition-unregister-by-name static-assert
              "app.evt" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """, bus=bus)
        self.assertEqual(self.notes(compiler).count("heard"), 1)
        self.assertEqual(bus._ct_subscriptions, {})
        updates = [e for e in events if e["payload"].get("operation") == "update" and e["payload"].get("name") == "owner_word"]
        self.assertEqual(len(updates), 1)   # attaching the owned subscription

    def test_ineligible_owner_skips_and_bad_owners_fail(self) -> None:
        compiler = self.compile("""
            import stdlib.sl
            word owned 1 end
            word listener drop "heard" ct-diagnostic-note end
            word setup
              "p" ct-lang-create drop "p" "owned" ct-lang-register-word drop
              "app.evt" "listener" "owned" ct-definition-candidates 0 list-get "handle" map-get drop
              ct-event-subscribe drop
              "app.evt" nil ct-event-emit drop
              "p" ct-lang-activate drop
              "app.evt" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """)
        self.assertEqual(self.notes(compiler).count("heard"), 1)
        compiler = self.compile("""
            import stdlib.sl
            word setup "e" "x" 5 ct-event-subscribe drop end
            compile-time setup
            word main 0 end
        """, expect_error=True)
        self.assertIn("ct-argument-type", [d.code for d in self.errors(compiler)])
        compiler = self.compile("""
            import stdlib.sl
            word gone 1 end
            word setup
              "gone" ct-definition-candidates 0 list-get "handle" map-get drop
              "gone" ct-definition-unregister-by-name drop
              "e" "x" rot ct-event-subscribe drop
            end
            compile-time setup
            word main 0 end
        """, expect_error=True)
        diag = next(d for d in self.errors(compiler) if d.code == "definition-argument-invalid")
        self.assertEqual((diag.category, diag.phase), ("definition_error", "event-subscription"))


class StreamTests(CompileTestBase):
    CYCLE = 'map-new list-new dup list-append "cycle" swap map-set "bad.row" swap ct-event-emit drop'

    def test_resumed_stream_failure_continues_and_bypasses_jsonl(self) -> None:
        stream = self.dir / "e.jsonl"
        proc = self.run_cli(f"""
            import stdlib.sl
            word fixer drop ct-recovery-resume drop end
            word setup
              "compiler.error.event_stream_error" "fixer" nil ct-event-subscribe drop
              {self.CYCLE}
              "after.row" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """, "--events-stream", str(stream))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = [json.loads(line)["event"] for line in stream.read_text().splitlines()]
        self.assertIn("after.row", names)
        self.assertNotIn("bad.row", names)
        self.assertNotIn("compiler.error.event_stream_error", names)   # bypassed during its dispatch

    def test_unresolved_stream_failure_is_an_error(self) -> None:
        proc = self.run_cli(f"""
            import stdlib.sl
            word setup {self.CYCLE} end
            compile-time setup
            word main 0 end
        """, "--events-stream", str(self.dir / "e.jsonl"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("event-stream-output-failed", proc.stderr)

    def test_stream_controls(self) -> None:
        stream = self.dir / "s.jsonl"
        proc = self.run_cli("""
            import stdlib.sl
            word setup
              ct-event-stream-enabled? static-assert
              0 ct-event-stream-set-enabled static-assert
              ct-event-stream-enabled? 0 == static-assert
              "hidden.row" nil ct-event-emit drop
              1 ct-event-stream-set-enabled 0 == static-assert
              "shown.row" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """, "--events-stream", str(stream))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = [json.loads(line)["event"] for line in stream.read_text().splitlines()]
        self.assertIn("shown.row", names)
        self.assertNotIn("hidden.row", names)

    def test_stream_controls_without_a_sink(self) -> None:
        compiler = self.compile("""
            import stdlib.sl
            word check
              ct-event-stream-enabled? 0 == static-assert
              0 ct-event-stream-set-enabled 0 == static-assert
            end
            compile-time check
            word main 0 end
        """)
        compiler = self.compile("""
            import stdlib.sl
            word check 1 ct-event-stream-set-enabled drop end
            compile-time check
            word main 0 end
        """, expect_error=True)
        self.assertIn("event-bus-disabled", [d.code for d in self.errors(compiler)])

    def test_no_events_json_cli(self) -> None:
        proc = self.run_cli("word main 0 end\n", "--no-events-json", "--events-stream", str(self.dir / "x.jsonl"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--no-events-json", proc.stderr)
        proc = self.run_cli("""
            import stdlib.sl
            word h drop "handled" puts end
            word setup "app.evt" "h" nil ct-event-subscribe drop "app.evt" nil ct-event-emit drop end
            compile-time setup
            word main 0 end
        """, "--no-events-json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("handled", proc.stdout)


class RenameTests(unittest.TestCase):
    def test_parser_error_rename(self) -> None:
        dictionary = l2_main.Compiler().dictionary
        self.assertIsNotNone(dictionary.lookup("ct-parser-error"))
        self.assertIsNone(dictionary.lookup("parse-error"))


class EventDefaultsTests(CompileTestBase):
    """Definition.* gating, --events-filter, and the ring size."""

    SOURCE = """
        import stdlib.sl
        word watcher drop end
        word setup "{pattern}" "watcher" nil ct-event-subscribe drop end
        compile-time setup
        word later 1 drop end
        word main 0 end
    """

    def test_definition_events_only_when_wanted(self) -> None:
        quiet = self.compile(self.SOURCE.replace("{pattern}", "compiler.error*"))
        self.assertEqual(quiet.parser.event_bus.recent("definition.*"), [])
        watched = self.compile(self.SOURCE.replace("{pattern}", "definition.candidates.changed"))
        names = [row["payload"].get("name") for row in watched.parser.event_bus.recent("definition.candidates.changed")]
        self.assertIn("later", names)
        self.assertEqual(watched.parser.event_bus.recent("definition.selection.changed"), [])

    def test_default_ring_capacity(self) -> None:
        self.assertEqual(EventBus()._events.maxlen, l2_main.DEFAULT_EVENT_RING_CAPACITY)
        self.assertEqual(l2_main.DEFAULT_EVENT_RING_CAPACITY, 8192)
        self.assertEqual(EventBus(16)._events.maxlen, 16)

    def test_sink_filter(self) -> None:
        bus = EventBus()
        seen = []
        bus.add_sink(seen.append, ["compile.*"])
        self.assertTrue(bus.wants("compile.begin"))
        self.assertFalse(bus.wants("definition.candidates.changed"))
        bus.publish("compile.begin", {})
        bus.publish("other.event", {})
        self.assertEqual([row["event"] for row in seen], ["compile.begin"])

    def test_cli_events_filter(self) -> None:
        src = self.dir / "f.sl"
        src.write_text("import stdlib.sl\nword main 0 end\n", encoding="utf-8")
        out = self.dir / "events.jsonl"
        result = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), "--no-daemon", str(src), "--no-artifact",
             "-I", str(ROOT / "stdlib"), "--events-stream", str(out), "--events-filter", "compile.*"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        events = {json.loads(line)["event"] for line in out.read_text().splitlines()}
        self.assertEqual(events, {"compile.begin", "compile.end"})


if __name__ == "__main__":
    unittest.main()
