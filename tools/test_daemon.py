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
            daemon = l2_main._IntegratedCompilerDaemon(str(root / "sock"), str(root / "pid"), 1, state_dir=str(root / "state"), auto_reload=False)
            old_handlers = set(daemon.log.handlers)
            # Events are forwarded only while something observes the daemon.
            observed: list = []
            daemon.bus.subscribe("word.compile.*", observed.append)
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
                self.assertIn("input eof", eof_result.get("stdout", ""))
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
        import l2_client as main

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
                    "--state-dir",
                    str(root / "state"),
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
        with tempfile.TemporaryDirectory(prefix="l2-daemon-state-") as raw:
            root = Path(raw)
            source = root / "module.sl"
            source.write_text("word main 0 end\n", encoding="utf-8")
            daemon = l2_main._IntegratedCompilerDaemon(str(root / "sock"), str(root / "pid"), 1, state_dir=str(root / "state"), auto_reload=False)
            logger = daemon.log
            old_handlers = set(logger.handlers)
            try:
                expected_hash = daemon._touch_source_cache(source)
                daemon._snapshot_state()
                restored = l2_main._IntegratedCompilerDaemon(str(root / "sock2"), str(root / "pid2"), 1, state_dir=str(root / "state"), auto_reload=False)
                self.assertEqual(len(restored._source_cache), 1)
                self.assertEqual(restored._touch_source_cache(source), expected_hash)
                self.assertEqual(restored._cache_hits, 1)
            finally:
                for handler in list(set(logger.handlers) - old_handlers):
                    logger.removeHandler(handler)
                    handler.close()

    def test_simultaneous_clients_start_one_daemon_and_event_file_is_written(self) -> None:
        import l2_client as main

        saved_env = {name: os.environ.get(name) for name in
                     ("L2_DAEMON_NO_AUTO_RELOAD", "L2_DAEMON_RUNTIME_DIR", "L2_DAEMON_STATE_DIR", "L2_DAEMON_WORKERS")}
        with tempfile.TemporaryDirectory(prefix="l2-daemon-start-") as raw:
            root = Path(raw)
            os.environ.update({"L2_DAEMON_NO_AUTO_RELOAD": "1", "L2_DAEMON_RUNTIME_DIR": str(root / "run"),
                               "L2_DAEMON_STATE_DIR": str(root / "state"), "L2_DAEMON_WORKERS": "2"})
            instance = main._instance()
            spawned = []
            original_popen = subprocess.Popen

            def capture_popen(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                if "--daemon-serve" in (args[0] if args else kwargs.get("args", [])):
                    spawned.append(process)
                return process

            subprocess.Popen = capture_popen
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    started = list(pool.map(lambda _index: main._ensure_daemon(instance), range(8)))
                self.assertTrue(all(started))
                self.assertEqual(len(spawned), 1)
                status = main._daemon_send({"cmd": "status"}, socket_path=instance.socket)
                self.assertEqual(status["pid"], int(Path(instance.pid).read_text(encoding="utf-8").strip()))

                def request(argv):
                    return main._daemon_send(main._run_payload(argv, None), socket_path=instance.socket)

                event_path = root / "events.jsonl"
                result = request(["tests/ct/event_objects.sl", "--no-artifact", "--events-stream", str(event_path)])
                self.assertEqual(result.get("code"), 0, result)
                rows = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(row.get("event") == "compile.end" for row in rows))
                self.assertEqual(len(rows), len({json.dumps(row, sort_keys=True) for row in rows}))
                missing_path = request(["tests/ct/event_objects.sl", "--events-stream"])
                self.assertFalse(missing_path.get("ok"))
                self.assertIn("requires a file path", missing_path.get("error", ""))
                option_as_path = request(["tests/ct/event_objects.sl", "--events-stream", "--force"])
                self.assertFalse(option_as_path.get("ok"))
                self.assertIn("requires a file path", option_as_path.get("error", ""))
            finally:
                main._shutdown_daemon(socket_path=instance.socket, pid_path=instance.pid)
                subprocess.Popen = original_popen
                for process in spawned:
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        pass
                for name, value in saved_env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

    def test_slow_event_observer_does_not_duplicate_event_file_records(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-event-reader-") as raw:
            root = Path(raw)
            daemon = l2_main._IntegratedCompilerDaemon(str(root / "sock"), str(root / "pid"), 1, state_dir=str(root / "state"), auto_reload=False)
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


class DaemonReliabilityTests(unittest.TestCase):
    """Regression tests for daemon client/server reliability fixes."""

    def _make_daemon(self, root: Path, *, auto_reload: bool) -> "l2_main._IntegratedCompilerDaemon":
        daemon = l2_main._IntegratedCompilerDaemon(str(root / "sock"), str(root / "pid"), 1, state_dir=str(root / "state"), auto_reload=auto_reload)
        self.addCleanup(self._close_daemon, daemon, set(daemon.log.handlers))
        return daemon

    @staticmethod
    def _close_daemon(daemon, old_handlers) -> None:
        daemon._shutdown_compiler_workers()
        for handler in list(set(daemon.log.handlers) - old_handlers):
            daemon.log.removeHandler(handler)
            handler.close()

    def test_client_does_not_block_on_idle_stdin_pipe(self) -> None:
        # The parent keeps stdin open and never writes to it; a plain compile
        # must not wait for EOF on it.
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen(
            [sys.executable, str(root / "main.py"), "tests/general/hello.sl", "--no-artifact"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("main.py blocked waiting for EOF on an idle stdin pipe")
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self.assertEqual(proc.returncode, 0)

    def test_stdin_forwarding_rules(self) -> None:
        import l2_client as main

        saved = sys.stdin
        read_fd, write_fd = os.pipe()
        try:
            # idle pipe, plain compile: not read at all
            sys.stdin = os.fdopen(read_fd, "r", encoding="utf-8")
            self.assertIsNone(main._read_stdin_for_daemon(["x.sl"]))
            # execution mode: read to EOF
            os.write(write_fd, b"piped data")
            os.close(write_fd)
            write_fd = -1
            self.assertEqual(main._read_stdin_for_daemon(["x.sl", "--ct-run-main"]), "piped data")
            sys.stdin.close()
            # regular file: always forwarded (cannot block)
            with tempfile.TemporaryFile("w+", encoding="utf-8") as handle:
                handle.write("file data")
                handle.seek(0)
                sys.stdin = handle
                self.assertEqual(main._read_stdin_for_daemon(["x.sl"]), "file data")
        finally:
            sys.stdin = saved
            if write_fd != -1:
                os.close(write_fd)

    def test_stale_code_requests_local_fallback_and_reload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-daemon-stale-") as raw:
            daemon = self._make_daemon(Path(raw), auto_reload=True)
            daemon.code_fingerprint = (("l2_main.py", 0, 0),)  # pretend sources changed
            with daemon._lock:
                daemon._queued += 1  # as the socket handler does for run requests
            response = daemon.handle({"cmd": "run", "argv": ["tests/general/hello.sl"]}, None, queued_registered=True)
            self.assertFalse(response.get("ok"))
            self.assertTrue(response.get("fallback"))
            self.assertTrue(response.get("stale"))
            self.assertTrue(daemon.reload_requested.is_set())
            self.assertEqual(daemon._queued, 0)  # drain must not wait for it
            self.assertTrue(daemon.handle({"cmd": "ping"}, None).get("stale"))

    def test_worker_failure_requests_local_fallback(self) -> None:
        class BrokenWorker:
            busy = True

            def run(self, *_args, **_kwargs):
                raise RuntimeError("worker crashed")

        with tempfile.TemporaryDirectory(prefix="l2-daemon-worker-") as raw:
            daemon = self._make_daemon(Path(raw), auto_reload=False)
            daemon._acquire_compiler_worker = lambda: BrokenWorker()
            daemon._release_compiler_worker = lambda worker: None
            response = daemon._run(["tests/general/hello.sl"], False, None)
            self.assertFalse(response.get("ok"))
            self.assertTrue(response.get("fallback"))
            self.assertIn("worker crashed", response.get("error", ""))

    def test_client_falls_back_and_never_resends_delivered_request(self) -> None:
        import io
        import l2_client as main

        saved = (main._ensure_daemon, main._daemon_send, sys.stderr)
        calls = []
        try:
            main._ensure_daemon = lambda: True

            def fallback_send(payload, *args, **kwargs):
                calls.append(payload["cmd"])
                return {"ok": False, "fallback": True, "error": "compiler worker failed: boom"}

            main._daemon_send = fallback_send
            sys.stderr = io.StringIO()
            self.assertIsNone(main._run_via_daemon(["x.sl"], None))
            self.assertIn("compiling locally", sys.stderr.getvalue())

            calls.clear()

            def lost_reply(payload, *args, **kwargs):
                calls.append(payload["cmd"])
                raise RuntimeError("connection closed")

            main._daemon_send = lost_reply
            self.assertIsNone(main._run_via_daemon(["x.sl"], None))
            self.assertEqual(calls, ["run"])  # delivered once, not resent

            calls.clear()

            def not_delivered(payload, *args, **kwargs):
                calls.append(payload["cmd"])
                raise main._DaemonNotDelivered("no socket")

            main._daemon_send = not_delivered
            self.assertIsNone(main._run_via_daemon(["x.sl"], None))
            self.assertEqual(calls, ["run", "run"])  # safe to retry once
        finally:
            main._ensure_daemon, main._daemon_send, sys.stderr = saved

    def test_shutdown_helper_sends_shutdown_request(self) -> None:
        import l2_client as main

        saved = main._daemon_send
        sent = []
        try:
            main._daemon_send = lambda payload, *args, **kwargs: sent.append(payload) or {"ok": True}
            with tempfile.TemporaryDirectory(prefix="l2-daemon-shutdown-") as raw:
                root = Path(raw)
                main._shutdown_daemon(socket_path=str(root / "s"), pid_path=str(root / "p"))
            self.assertEqual(sent, [{"cmd": "shutdown"}])
        finally:
            main._daemon_send = saved


if __name__ == "__main__":
    unittest.main()
