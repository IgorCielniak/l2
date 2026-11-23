#!/usr/bin/env python3
"""Contract tests for events: canonical records and
publish-time snapshots, the event words, CT subscriptions and dispatch,
enable/disable and --no-events, and typed JSONL projections."""

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
from l2_main import EventBus, EventSubscriptionHandle, ParseError, SourceOrigin


class BusTests(unittest.TestCase):
    def test_canonical_record_and_snapshot(self) -> None:
        bus = EventBus()
        payload = {"items": [1, 2], "nested": {"a": 1}}
        record = bus.publish("build.note", payload)
        self.assertEqual(set(record), {"event", "timestamp_ns", "payload"})
        payload["items"].append(3)
        payload["nested"]["a"] = 2
        self.assertEqual(bus.recent("build.note")[0]["payload"], {"items": [1, 2], "nested": {"a": 1}})

    def test_snapshot_preserves_sharing_and_cycles(self) -> None:
        shared = [1]
        cyclic: dict = {}
        cyclic["self"] = cyclic
        record = EventBus().publish("e", {"x": shared, "y": shared, "c": cyclic})
        self.assertIs(record["payload"]["x"], record["payload"]["y"])
        self.assertIs(record["payload"]["c"]["self"], record["payload"]["c"])

    def test_disabled_bus_drops_publications(self) -> None:
        bus = EventBus()
        bus.enabled = False
        self.assertIsNone(bus.publish("e"))
        self.assertEqual(bus.recent(), [])

    def test_sinks_then_observers(self) -> None:
        bus = EventBus()
        order = []
        bus.subscribe("*", lambda record: order.append("observer"))
        bus.add_sink(lambda record: order.append("sink"))
        bus.publish("e")
        self.assertEqual(order, ["sink", "observer"])


class WordTests(unittest.TestCase):
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
        for name in ("event-subscribe", "event-unsubscribe", "event-emit", "event-object-create",
                     "event-object-prop-append", "event-object-prop-get", "event-object-emit",
                     "get-event-type", "get-event-name", "get-event-timestamp", "get-event-payload"):
            self.assertIsNone(self.compiler.dictionary.lookup(name), name)

    def test_draft_and_accessors(self) -> None:
        draft = self.run_word("event-new", "build.note")[0]
        self.assertEqual(draft, {"event": "build.note", "payload": {}})
        self.assertEqual(self.run_word("event-timestamp", draft), [None, 0])
        self.assertIs(self.run_word("event-payload-set", draft, "k", 5)[0], draft)
        self.assertEqual(self.run_word("event-payload-get", draft, "k"), [5, 1])
        self.assertEqual(self.run_word("event-payload-get", draft, "missing"), [None, 0])
        self.assertEqual(self.run_word("event-name", draft), ["build.note"])
        self.assertEqual(self.run_word("event-payload", draft), [{"k": 5}])
        self.assertCode("ct-argument-invalid", "event-new", "")
        self.assertCode("ct-argument-type", "event-name", {"type": "legacy"})

    def test_emit_object_publishes_fresh_copies(self) -> None:
        draft = self.run_word("event-new", "build.note")[0]
        draft["payload"]["n"] = 1
        self.assertEqual(self.run_word("ct-event-emit-object", draft), [1])
        draft["payload"]["n"] = 2
        self.run_word("ct-event-emit-object", draft)
        rows = self.parser.event_bus.recent("build.note")
        self.assertEqual([row["payload"]["n"] for row in rows], [1, 2])
        self.assertNotIn("timestamp_ns", draft)

    def test_emit_object_validation(self) -> None:
        for bad in ({"event": "e", "payload": {}, "type": "e"}, {"event": "", "payload": {}},
                    {"event": "e", "payload": {}, "timestamp_ns": True}, {"event": "e", "payload": []}):
            with self.assertRaises(ParseError, msg=repr(bad)):
                self.run_word("ct-event-emit-object", bad)

    def test_emit(self) -> None:
        self.assertEqual(self.run_word("ct-event-emit", "x.y", None), [1])
        self.assertEqual(self.parser.event_bus.recent("x.y")[0]["payload"], {})
        self.assertCode("ct-argument-type", "ct-event-emit", "x.y", [1])
        self.assertCode("ct-argument-invalid", "ct-event-emit", "", None)

    def test_subscribe_and_unsubscribe(self) -> None:
        handle = self.run_word("ct-event-subscribe", "a.*", "handler", None)[0]
        self.assertIsInstance(handle, EventSubscriptionHandle)
        other = self.run_word("ct-event-subscribe", "a.*", "handler", None)[0]
        self.assertNotEqual(handle.id, other.id)
        self.assertEqual(self.run_word("ct-event-unsubscribe", handle), [1])
        self.assertEqual(self.run_word("ct-event-unsubscribe", handle), [0])
        self.assertCode("ct-argument-type", "ct-event-unsubscribe", 1)
        self.assertCode("ct-argument-type", "ct-event-subscribe", "a", "h", "owner")
        self.assertCode("ct-argument-invalid", "ct-event-subscribe", "", "h", None)
        self.assertCode("ct-argument-invalid", "ct-event-subscribe", "a", "", None)

    def test_runtime_toggle(self) -> None:
        self.assertEqual(self.run_word("ct-event-enabled?"), [1])
        self.assertEqual(self.run_word("ct-event-set-enabled", 0), [1])
        self.assertEqual(self.run_word("ct-event-enabled?"), [0])
        self.assertCode("event-bus-disabled", "ct-event-emit", "e", None)
        self.assertCode("event-bus-disabled", "ct-event-subscribe", "e", "h", None)
        self.assertEqual(self.run_word("ct-event-set-enabled", True), [0])
        self.assertCode("ct-argument-type", "ct-event-set-enabled", "yes")

    def test_hard_disable(self) -> None:
        self.parser.events_hard_disabled = True
        self.assertEqual(self.run_word("ct-event-enabled?"), [0])
        self.assertEqual(self.run_word("ct-event-set-enabled", 0), [0])
        self.assertEqual(self.run_word("ct-event-set-enabled", 0), [0])
        self.assertCode("event-bus-disabled", "ct-event-set-enabled", 1)
        self.assertCode("event-bus-disabled", "ct-event-emit", "e", None)
        handle = EventSubscriptionHandle(EventBus(), "e", "h")
        self.assertEqual(self.run_word("ct-event-unsubscribe", handle), [0])


class DispatchTests(unittest.TestCase):
    def compile(self, source: str, *extra: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="l2-events-") as raw:
            path = Path(raw) / "e.sl"
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact", "--no-daemon", *extra],
                cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
            )

    def assertOk(self, proc: subprocess.CompletedProcess) -> str:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_order_copies_and_snapshot(self) -> None:
        out = self.assertOk(self.compile("""
            import stdlib.sl
            # first mutates its copy and subscribes "late"; second must see
            # the original payload; "late" must not run for this event.
            word first
              event-payload "n" 99 map-set drop
              "first" puts
              "t.e" "late" nil ct-event-subscribe drop
            end
            word second event-payload "n" map-get drop puti cr end
            word late drop "late" puts end
            word setup
              "t.e" "first" nil ct-event-subscribe drop
              "t.e" "second" nil ct-event-subscribe drop
              map-new "n" 1 map-set "t.e" swap ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """))
        self.assertIn("first\n1\n", out)
        self.assertNotIn("late", out)

    def test_reentrant_dispatch(self) -> None:
        out = self.assertOk(self.compile("""
            import stdlib.sl
            word outer1 drop "outer1" puts "inner" nil ct-event-emit drop end
            word inner drop "inner" puts end
            word outer2 drop "outer2" puts end
            word setup
              "outer" "outer1" nil ct-event-subscribe drop
              "inner" "inner" nil ct-event-subscribe drop
              "outer" "outer2" nil ct-event-subscribe drop
              "outer" nil ct-event-emit drop
            end
            compile-time setup
            word main 0 end
        """))
        self.assertIn("outer1\ninner\nouter2\n", out)

    def test_handler_must_consume_event(self) -> None:
        proc = self.compile("""
            import stdlib.sl
            word sloppy end
            word setup "t" "sloppy" nil ct-event-subscribe drop "t" nil ct-event-emit drop end
            compile-time setup
            word main 0 end
        """)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error[event-handler-failed]", proc.stderr)
        self.assertIn("left 1 value(s)", proc.stderr)

    def test_unavailable_handler(self) -> None:
        proc = self.compile("""
            import stdlib.sl
            word setup "t" "nosuchword" nil ct-event-subscribe drop "t" nil ct-event-emit drop end
            compile-time setup
            word main 0 end
        """)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error[event-handler-failed]", proc.stderr)

    def test_handler_diagnostic_is_preserved(self) -> None:
        proc = self.compile("""
            import stdlib.sl
            word picky drop "nope" ct-diagnostic-error end on-event t
            emit-event t
            word main 0 end
        """)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error[ct-diagnostic-raised]: nope", proc.stderr)
        self.assertNotIn("event-handler-failed", proc.stderr)

    def test_emit_event_source_origin(self) -> None:
        out = self.assertOk(self.compile("""
            import stdlib.sl
            word where event-payload "source-origin" map-get drop source-origin-start-line puti cr end on-event here
            emit-event here
            word main 0 end
        """))
        self.assertIn("3\n", out)

    def test_no_events(self) -> None:
        proc = self.compile("""
            import stdlib.sl
            word main 0 end
        """, "--no-events")
        self.assertOk(proc)
        proc = self.compile("word main 0 end\n", "--no-events", "--events-stream", "/dev/null")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--no-events", proc.stderr)
        proc = self.compile("""
            import stdlib.sl
            word h drop end on-event t
            word main 0 end
        """, "--no-events")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("event-bus-disabled", proc.stderr)

    def test_jsonl_typed_projection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-events-") as raw:
            stream = Path(raw) / "events.jsonl"
            proc = self.compile("""
                import stdlib.sl
                word setup
                  map-new 7 "seven" map-set "intkeys" swap ct-event-emit drop
                  "careful" ct-diagnostic-warning
                end
                compile-time setup
                emit-event here
                word main 0 end
            """, "--events-stream", str(stream))
            self.assertOk(proc)
            rows = [json.loads(line) for line in stream.read_text().splitlines()]
        by_name = {row["event"]: row for row in rows}
        self.assertTrue(all(set(row) == {"event", "timestamp_ns", "payload"} for row in rows))
        self.assertEqual(by_name["intkeys"]["payload"], {"kind": "Map", "entries": [[7, "seven"]]})
        origin = by_name["here"]["payload"]["source-origin"]
        self.assertEqual((origin["kind"], origin["start_line"]), ("SourceOrigin", 7))
        diag = by_name["compile.diagnostic"]["payload"]["diagnostic"]
        self.assertEqual((diag["kind"], diag["code"], diag["severity"]), ("Diagnostic", "ct-warning-raised", "warning"))

    def test_jsonl_unserializable_value_fails(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-events-") as raw:
            proc = self.compile("""
                import stdlib.sl
                word setup
                  map-new list-new dup list-append "cycle" swap map-set "loop" swap ct-event-emit drop
                end
                compile-time setup
                word main 0 end
            """, "--events-stream", str(Path(raw) / "e.jsonl"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("event-stream-output-failed", proc.stderr)


if __name__ == "__main__":
    unittest.main()
