#!/usr/bin/env python3
"""Focused Python-side EventBus integration checks for tooling authors."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import l2_main
from l2_main import Compiler, EventBus, optimize_emitted_asm_text


class EventBusToolingTests(unittest.TestCase):
    def test_glob_replay_and_compiler_events(self) -> None:
        bus = EventBus(8)
        seen = []
        handle = bus.subscribe("word.compile.*", seen.append)
        compiler = Compiler(event_bus=bus)
        compiler.compile_source("word main 0 end\n")
        self.assertIn("word.compile.begin", [item["event"] for item in seen])
        self.assertTrue(bus.unsubscribe(handle))
        self.assertGreater(bus.stats()["events_published"], 0)

    def test_recent_zero_limit_is_empty(self) -> None:
        bus = EventBus(8)
        bus.publish("one")
        bus.publish("two")
        self.assertEqual(bus.recent(limit=0), [])

    def test_emit_section_reports_incremental_bytes(self) -> None:
        bus = EventBus(8)
        payloads = []
        bus.subscribe("emit.section", payloads.append)
        compiler = Compiler(event_bus=bus)
        compiler.compile_source(
            "word foo 1 2 3 4 5 6 7 8 9 10 end\nword main foo end\n"
        )

        self.assertEqual(len(payloads), 2)
        self.assertLess(payloads[1]["payload"]["bytes_appended"], payloads[0]["payload"]["bytes_appended"])

    def test_asm_postopt_keeps_referenced_labels(self) -> None:
        asm = """
start:
    jmp end
unused:
    nop
end:
    ret
"""
        optimized, stats, _ = optimize_emitted_asm_text(asm)
        self.assertIn("end:", optimized)
        self.assertNotIn("unused:", optimized)
        self.assertGreater(stats["removed_redundant_labels"], 0)

    def test_compile_source_skips_preview_rendering_by_default(self) -> None:
        original = l2_main._render_transformed_module_preview

        def fail(*_args, **_kwargs):
            raise AssertionError("preview rendering should be skipped when render_preview=False")

        l2_main._render_transformed_module_preview = fail
        try:
            Compiler().compile_source("word main 0 end\n")
        finally:
            l2_main._render_transformed_module_preview = original

    def test_main_daemon_is_opt_in(self) -> None:
        self.assertFalse(main._daemon_default_enabled())

    def test_event_macros_work_without_explicit_event_bus(self) -> None:
        compiler = Compiler()
        compiler.compile_source(
            """
word inspect_build_event
  get-event-name
  "build.request" string=
  static_assert
end on-event build.request

emit-event build.request
word main 0 end
"""
        )
        self.assertIsNotNone(compiler.parser.event_bus)

    def test_observer_failures_are_isolated_but_extension_failures_propagate(self) -> None:
        bus = EventBus()
        bus.subscribe("observer.*", lambda _event: (_ for _ in ()).throw(RuntimeError("observer")))
        bus.publish("observer.event")

        bus.subscribe(
            "extension.*",
            lambda _event: (_ for _ in ()).throw(RuntimeError("extension")),
            propagate_errors=True,
        )
        with self.assertRaisesRegex(RuntimeError, "extension"):
            bus.publish("extension.event")

    def test_replay_observer_failures_are_isolated_and_extension_failure_unsubscribes(self) -> None:
        bus = EventBus()
        bus.publish("replay.event")

        def fail(_event):
            raise RuntimeError("replay observer")

        bus.subscribe("replay.*", fail, replay=True)
        self.assertEqual(bus.stats()["subscribers"], 1)

        with self.assertRaisesRegex(RuntimeError, "replay observer"):
            bus.subscribe("replay.*", fail, replay=True, propagate_errors=True)
        self.assertEqual(bus.stats()["subscribers"], 1)


if __name__ == "__main__":
    unittest.main()
