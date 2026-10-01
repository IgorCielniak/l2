#!/usr/bin/env python3
"""Focused daemon lifecycle and event forwarding tests."""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import threading
import tempfile
import time
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import l2_main


class DaemonTests(unittest.TestCase):
    def test_compile_time_native_output_is_captured_by_reused_worker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-daemon-stdio-") as raw:
            root = Path(raw)
            daemon = l2_main._IntegratedCompilerDaemon(str(root / "sock"), str(root / "pid"), 1, auto_reload=False)
            old_handlers = set(daemon.log.handlers)
            try:
                result = daemon._run(
                    ["tests/ct/event_handler.sl", "--no-artifact", "--ct-run-main"],
                    False,
                    None,
                )
                self.assertEqual(result.get("code"), 0, result)
                self.assertIn('"watched"\n"main"\n42', result.get("stdout", ""))
                self.assertEqual(result.get("stderr"), "")
                recent_event_names = [row["event"] for row in daemon.bus.recent()]
                self.assertIn("word.compile.begin", recent_event_names)
                worker_pid = daemon._compiler_workers[0].process.pid
                next_result = daemon._run(["tests/general/hello.sl", "--no-artifact"], False, None)
                self.assertEqual(next_result.get("code"), 0, next_result)
                self.assertEqual(len(daemon._compiler_workers), 1)
                self.assertEqual(daemon._compiler_workers[0].process.pid, worker_pid)

                stdin_result = daemon._run(
                    ["tests/io_read_stdin.sl", "--no-artifact", "--ct-run-main"],
                    False,
                    "stdin via daemon",
                )
                self.assertEqual(stdin_result.get("code"), 0, stdin_result)
                self.assertIn("stdin via daemon", stdin_result.get("stdout", ""))
                eof_result = daemon._run(
                    ["tests/io_read_stdin.sl", "--no-artifact", "--ct-run-main"],
                    False,
                    None,
                )
                self.assertEqual(eof_result.get("code"), 0, eof_result)
                self.assertIn("read_stdin failed", eof_result.get("stdout", ""))
                start_override = daemon._run(
                    ["tests/general/start_override.sl", "--no-artifact", "--ct-run-main"],
                    False,
                    None,
                )
                self.assertEqual(start_override.get("code"), 0, start_override)
                self.assertIn("hello world\n24\n", start_override.get("stdout", ""))
                after_start_override = daemon._run(["tests/general/hello.sl", "--no-artifact"], False, None)
                self.assertEqual(after_start_override.get("code"), 0, after_start_override)
                self.assertEqual(len(daemon._compiler_workers), 1)
                self.assertEqual(daemon._compiler_workers[0].process.pid, worker_pid)
            finally:
                daemon._shutdown_compiler_workers()
                for handler in list(set(daemon.log.handlers) - old_handlers):
                    daemon.log.removeHandler(handler)
                    handler.close()

    def test_daemon_lease_requires_matching_process_identity(self) -> None:
        import main

        with tempfile.TemporaryDirectory(prefix="l2-daemon-lease-") as raw:
            root = Path(raw)
            socket_path = str(root / "daemon.sock")
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "l2_main.py"),
                    "--daemon-serve",
                    "--socket",
                    socket_path,
                    "--pid",
                    str(root / "daemon.pid"),
                    "--workers",
                    "1",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if Path(socket_path).exists():
                        break
                    time.sleep(0.02)
                cmdline = Path(f"/proc/{proc.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
                stat_text = Path(f"/proc/{proc.pid}/stat").read_text(encoding="utf-8")
                if "--daemon-serve" not in cmdline:
                    self.skipTest("subprocess did not expose daemon-like cmdline for identity test")
                fields = stat_text.rsplit(")", 1)[1].strip().split()
                if len(fields) < 20:
                    self.skipTest("unexpected /proc stat format")
                wrong_start_time_ns = (int(fields[19]) + 1) * 1_000_000_000 // int(os.sysconf("SC_CLK_TCK"))
                lease_path = Path(socket_path + ".lease")
                lease_path.write_text(
                    json.dumps({"pid": proc.pid, "start_time_ns": wrong_start_time_ns, "ts_ns": time.time_ns()}, separators=(",", ":")),
                    encoding="utf-8",
                )
                self.assertIsNone(main._daemon_lease_pid(socket_path=socket_path, ttl=5.0))
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass

    def test_snapshot_restores_fresh_source_cache_entries(self) -> None:
        old_state_path = l2_main.DAEMON_STATE_PATH
        with tempfile.TemporaryDirectory(prefix="l2-daemon-state-") as raw:
            root = Path(raw)
            source = root / "module.sl"
            source.write_text("word main 0 end\n", encoding="utf-8")
            l2_main.DAEMON_STATE_PATH = str(root / "state.json")
            daemon = l2_main._IntegratedCompilerDaemon(str(root / "sock"), str(root / "pid"), 1, auto_reload=False)
            logger = daemon.log
            old_handlers = set(logger.handlers)
            try:
                expected_hash = daemon._touch_source_cache(source)
                daemon._snapshot_state()
                restored = l2_main._IntegratedCompilerDaemon(str(root / "sock2"), str(root / "pid2"), 1, auto_reload=False)
                self.assertEqual(len(restored._source_cache), 1)
                self.assertEqual(restored._touch_source_cache(source), expected_hash)
                self.assertEqual(restored._cache_hits, 1)
            finally:
                for handler in list(set(logger.handlers) - old_handlers):
                    logger.removeHandler(handler)
                    handler.close()
                l2_main.DAEMON_STATE_PATH = old_state_path

    def test_simultaneous_clients_start_one_daemon_and_event_file_is_written(self) -> None:
        old_reload_setting = os.environ.get("L2_DAEMON_NO_AUTO_RELOAD")
        os.environ["L2_DAEMON_NO_AUTO_RELOAD"] = "1"
        with tempfile.TemporaryDirectory(prefix="l2-daemon-start-") as raw:
            root = Path(raw)
            socket_path = str(root / "compiler.sock")
            pid_path = str(root / "compiler.pid")
            spawned = []
            original_popen = l2_main.subprocess.Popen

            def capture_popen(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                spawned.append(process)
                return process

            l2_main.subprocess.Popen = capture_popen
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    started = list(pool.map(lambda _index: l2_main.daemon_start(socket_path=socket_path, pid_path=pid_path, workers=2), range(8)))
                self.assertTrue(all(started))
                status = l2_main.daemon_status(socket_path=socket_path)
                self.assertIsNotNone(status)
                self.assertEqual(status["pid"], int(Path(pid_path).read_text(encoding="utf-8").strip()))

                event_path = root / "events.jsonl"
                result = l2_main.daemon_request(
                    ["tests/ct/event_objects.sl", "--no-artifact", "--events-stream", str(event_path)],
                    socket_path=socket_path,
                )
                self.assertEqual(result.get("code"), 0, result)
                rows = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(row.get("type") == "compile.end" for row in rows))
                self.assertEqual(len(rows), len({json.dumps(row, sort_keys=True) for row in rows}))
                missing_path = l2_main.daemon_request(
                    ["tests/ct/event_objects.sl", "--events-stream"],
                    socket_path=socket_path,
                )
                self.assertFalse(missing_path.get("ok"))
                self.assertIn("requires a file path", missing_path.get("error", ""))
                option_as_path = l2_main.daemon_request(
                    ["tests/ct/event_objects.sl", "--events-stream", "--force"],
                    socket_path=socket_path,
                )
                self.assertFalse(option_as_path.get("ok"))
                self.assertIn("requires a file path", option_as_path.get("error", ""))
            finally:
                l2_main.daemon_stop(socket_path=socket_path)
                l2_main.subprocess.Popen = original_popen
                for process in spawned:
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        pass
                if old_reload_setting is None:
                    os.environ.pop("L2_DAEMON_NO_AUTO_RELOAD", None)
                else:
                    os.environ["L2_DAEMON_NO_AUTO_RELOAD"] = old_reload_setting

    def test_slow_event_observer_does_not_duplicate_event_file_records(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-event-reader-") as raw:
            root = Path(raw)
            daemon = l2_main._IntegratedCompilerDaemon(str(root / "sock"), str(root / "pid"), 1, auto_reload=False)
            first_event = threading.Event()
            delayed = threading.Event()

            def slow_observer(_record):
                if first_event.is_set():
                    return
                first_event.set()
                time.sleep(2.0)
                delayed.set()

            handle = daemon.bus.subscribe("*", slow_observer)
            event_path = root / "slow-events.jsonl"
            old_daemon_flag = os.environ.get("L2_DAEMON")
            os.environ["L2_DAEMON"] = "0"
            old_handlers = set(daemon.log.handlers)
            try:
                result = daemon._run(
                    ["tests/ct/event_objects.sl", "--no-artifact", "--events-stream", str(event_path)],
                    False,
                    None,
                )
                self.assertEqual(result.get("code"), 0, result)
                self.assertTrue(delayed.is_set())
                records = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
                serialized = [json.dumps(record, sort_keys=True) for record in records]
                self.assertEqual(len(serialized), len(set(serialized)))
            finally:
                daemon.bus.unsubscribe(handle)
                daemon._shutdown_compiler_workers()
                for handler in list(set(daemon.log.handlers) - old_handlers):
                    daemon.log.removeHandler(handler)
                    handler.close()
                if old_daemon_flag is None:
                    os.environ.pop("L2_DAEMON", None)
                else:
                    os.environ["L2_DAEMON"] = old_daemon_flag


if __name__ == "__main__":
    unittest.main()
