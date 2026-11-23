#!/usr/bin/env python3
"""Daemon management: one daemon per checkout shared by every directory,
named instances, the --daemon-* flags of main.py, idle timeout, worker
scaling, replacing a hung daemon, old-layout daemons, and permissions.

Each test uses its own runtime and state directories (L2_DAEMON_RUNTIME_DIR,
L2_DAEMON_STATE_DIR), so no daemon outside the test is touched."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_client as client  # noqa: E402

PROGRAM = "import stdlib.sl\nimport {lib}\nword main libval puti cr 0 end\n"


class DaemonTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="l2dm-"))
        self.env = dict(os.environ)
        for name in list(self.env):
            if name.startswith("L2_DAEMON") or name in ("L2_NO_WARM_PREFIX", "L2_NO_ASM_PREWARM"):
                self.env.pop(name)
        self.env.update({
            "L2_DAEMON_RUNTIME_DIR": str(self.tmp / "run"),
            "L2_DAEMON_STATE_DIR": str(self.tmp / "state"),
            "L2_DAEMON_WORKERS": "2",
        })
        self.saved_env = {name: os.environ.get(name) for name in
                          ("L2_DAEMON_RUNTIME_DIR", "L2_DAEMON_STATE_DIR", "L2_DAEMON_NAME", "L2_DAEMON_SOCKET")}
        os.environ["L2_DAEMON_RUNTIME_DIR"] = self.env["L2_DAEMON_RUNTIME_DIR"]
        os.environ["L2_DAEMON_STATE_DIR"] = self.env["L2_DAEMON_STATE_DIR"]
        os.environ.pop("L2_DAEMON_NAME", None)
        os.environ.pop("L2_DAEMON_SOCKET", None)
        client._CURRENT = None

    def tearDown(self) -> None:
        for instance in client._instances_for_all():
            client._stop_instance(instance, graceful=False, timeout=2.0)
        # Anything still serving from this test's directories (for example a
        # daemon caught between re-execs by a failed assertion).
        for entry in os.listdir("/proc"):
            if entry.isdigit() and "--daemon-serve" in client._proc_cmdline(int(entry)) \
                    and str(self.tmp) in " ".join(client._proc_cmdline(int(entry))):
                client._terminate_tree(int(entry))
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        client._CURRENT = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def main(self, *args: str, cwd: Path | None = None, env: dict | None = None, check: bool = False,
             main_py: Path = ROOT / "main.py") -> subprocess.CompletedProcess:
        proc = subprocess.run([sys.executable, str(main_py), *args], cwd=cwd or ROOT, env=env or self.env,
                              capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
        if check:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def status(self, *extra: str) -> dict:
        proc = self.main("--daemon-status", "--json", *extra)
        return json.loads(proc.stdout)

    def project(self, name: str, value: int) -> Path:
        directory = self.tmp / name
        (directory / "lib").mkdir(parents=True)
        (directory / "lib" / "vals.sl").write_text(f"word libval {value} end\n")
        (directory / "p.sl").write_text(PROGRAM.format(lib="vals.sl"))
        return directory


class SharedDaemonTests(DaemonTestCase):
    def test_two_directories_share_one_daemon(self) -> None:
        a, b = self.project("a", 11), self.project("b", 22)
        for directory, value in ((a, "11"), (b, "22")):
            # Relative source, -I, -o and temp dir: resolved in the caller's directory.
            self.main("p.sl", "-I", "lib", "-o", "out/prog", "--temp-dir", "tmpbuild", cwd=directory, check=True)
            ran = subprocess.run([str(directory / "out" / "prog")], capture_output=True, text=True)
            self.assertEqual(ran.stdout.strip(), value)
            self.assertTrue((directory / "tmpbuild" / "p.asm").exists())
        status = self.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["total_requests"] >= 2, True)
        sources = [row["source"] for row in status["recent_compilations"]]
        self.assertIn(str(a / "p.sl"), sources)
        self.assertIn(str(b / "p.sl"), sources)
        listed = json.loads(self.main("--daemon-list", "--json").stdout)["instances"]
        self.assertEqual(len(listed), 1)
        # Nothing of the daemon's own lands in the projects.
        self.assertFalse((a / "build" / ".l2_daemon.sock").exists())

    def test_concurrent_clients_from_many_directories(self) -> None:
        dirs = [self.project(f"c{index}", index) for index in range(8)]

        def build(directory: Path) -> tuple:
            proc = self.main("p.sl", "-I", "lib", "-o", "prog", cwd=directory)
            ran = subprocess.run([str(directory / "prog")], capture_output=True, text=True) if proc.returncode == 0 else None
            return proc.returncode, ran.stdout.strip() if ran else proc.stderr

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(build, dirs))
        self.assertEqual(results, [(0, str(index)) for index in range(8)])
        listed = json.loads(self.main("--daemon-list", "--json").stdout)["instances"]
        self.assertEqual(len(listed), 1)
        serving = [pid for pid in os.listdir("/proc") if pid.isdigit()
                   and "--daemon-serve" in " ".join(client._proc_cmdline(int(pid)))
                   and str(self.tmp) in " ".join(client._proc_cmdline(int(pid)))]
        self.assertEqual(len(serving), 1)

    def test_different_checkouts_get_different_daemons(self) -> None:
        other = self.tmp / "other-checkout"
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        for rel in sorted(set(files) | {"main.py", "l2_main.py", "l2_client.py", "docs.py"}):
            if rel.split("/", 1)[0] in ("tests", "extra_tests", "examples", "docs", "tools"):
                continue
            (other / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, other / rel)
        project = self.project("p", 5)
        self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, check=True)
        self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, check=True, main_py=other / "main.py")
        listed = json.loads(self.main("--daemon-list", "--json").stdout)["instances"]
        checkouts = sorted(row["checkout"] for row in listed)
        self.assertEqual(checkouts, sorted([str(ROOT), str(other.resolve())]))

    def test_named_instances(self) -> None:
        self.main("--daemon-name", "second", "--daemon-start", check=True)
        project = self.project("n", 3)
        self.main("p.sl", "-I", "lib", "--no-artifact", "--daemon-name", "second", cwd=project, check=True)
        second = self.status("--daemon-name", "second")
        self.assertTrue(second["running"])
        self.assertEqual(second["name"], "second")
        self.assertGreaterEqual(second["total_requests"], 1)
        self.assertFalse(self.status()["running"])  # the default instance was never started
        names = sorted(row["name"] for row in json.loads(self.main("--daemon-list", "--json").stdout)["instances"])
        self.assertEqual(names, ["second"])
        bad = self.main("--daemon-name", "../x", "--daemon-status")
        self.assertNotEqual(bad.returncode, 0)

    def test_scope_directory_and_project(self) -> None:
        a, b = self.project("sa", 1), self.project("sb", 2)
        env = dict(self.env, L2_DAEMON_SCOPE="directory")
        for directory in (a, b):
            self.main("p.sl", "-I", "lib", "--no-artifact", cwd=directory, env=env, check=True)
        listed = json.loads(self.main("--daemon-list", "--json", env=env).stdout)["instances"]
        self.assertEqual(len(listed), 2)
        # Project scope: both directories inside one project root share a daemon.
        root = self.tmp / "proj"
        (root / ".git").mkdir(parents=True)
        for name in ("x", "y"):
            shutil.copytree(a, root / name)
        env = dict(self.env, L2_DAEMON_SCOPE="project")
        for name in ("x", "y"):
            self.main("p.sl", "-I", "lib", "--no-artifact", cwd=root / name, env=env, check=True)
        listed = json.loads(self.main("--daemon-list", "--json", env=env).stdout)["instances"]
        self.assertEqual(len(listed), 3)
        bad = self.main("--daemon-status", env=dict(self.env, L2_DAEMON_SCOPE="planet"))
        self.assertNotEqual(bad.returncode, 0)

    def test_explicit_socket(self) -> None:
        sock = self.tmp / "explicit" / "my.sock"
        env = dict(self.env, L2_DAEMON_SOCKET=str(sock))
        project = self.project("e", 9)
        self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, env=env, check=True)
        self.assertTrue(sock.exists())
        status = json.loads(self.main("--daemon-status", "--json", env=env).stdout)
        self.assertTrue(status["running"])
        self.main("--daemon-stop", env=env, check=True)
        self.assertFalse(sock.exists())

    def test_events_follow(self) -> None:
        self.main("--daemon-start", check=True)
        follower = subprocess.Popen([sys.executable, str(ROOT / "main.py"), "--daemon-events", "compile.*", "--follow"],
                                    env=self.env, stdout=subprocess.PIPE, text=True)
        try:
            time.sleep(0.5)
            project = self.project("f", 2)
            self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, check=True)
            line = follower.stdout.readline()
            self.assertTrue(json.loads(line)["event"].startswith("compile."))
        finally:
            follower.kill()
            follower.wait()
            follower.stdout.close()


class ManagementFlagTests(DaemonTestCase):
    def test_start_status_settings_and_config(self) -> None:
        self.main("--daemon-start", "--daemon-workers", "3", "--daemon-idle-timeout", "5", check=True)
        status = self.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["workers"], 3)
        self.assertEqual(status["idle_timeout_minutes"], 5.0)
        self.assertEqual(status["checkout_dir"], str(ROOT))
        self.assertIn("worker_details", status)
        text = self.main("--daemon-status").stdout
        self.assertIn("running, pid", text)
        again = self.main("--daemon-start", check=True)
        self.assertIn("already running", again.stdout)
        config = json.loads(self.main("--daemon-config", "--json").stdout)
        self.assertEqual(config["settings"]["workers"]["value"], 2)  # L2_DAEMON_WORKERS for new starts
        self.assertEqual(config["settings"]["workers"]["source"], "environment")
        self.assertIn("workers", config["running_differs"])  # the running daemon has 3

    def test_stop_kill_restart_reload_clear(self) -> None:
        self.main("--daemon-start", "--daemon-workers", "3", check=True)
        pid = self.status()["pid"]
        self.main("--daemon-reload", check=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self.status()
            # Answered by the daemon itself (not "running but unresponsive"
            # while it re-executes).
            if status.get("ok") and not status.get("stale") and status.get("pid") == pid:
                break
            time.sleep(0.2)
        self.assertEqual(self.status()["pid"], pid)  # re-exec in place
        project = self.project("r", 1)
        self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, check=True)
        workers_before = {w["pid"] for w in self.status()["worker_details"]}
        self.assertTrue(workers_before)
        self.main("--daemon-clear", check=True)
        self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, check=True)
        workers_after = {w["pid"] for w in self.status()["worker_details"]}
        self.assertTrue(workers_before.isdisjoint(workers_after))
        self.main("--daemon-restart", check=True)
        restarted = self.status()
        self.assertNotEqual(restarted["pid"], pid)
        self.assertEqual(restarted["workers"], 3)  # settings kept
        self.main("--daemon-kill", check=True)
        self.assertFalse(client._alive(restarted["pid"]) and "--daemon-serve" in " ".join(client._proc_cmdline(restarted["pid"])))
        self.assertFalse(self.status()["running"])
        self.main("--daemon-start", check=True)
        stopped = self.main("--daemon-stop", "--json", check=True)
        self.assertEqual(json.loads(stopped.stdout)[0]["result"], "stopped")
        self.assertFalse(Path(client._instance().info).exists())

    def test_stop_all_covers_every_instance(self) -> None:
        for name in ("one", "two"):
            self.main("--daemon-name", name, "--daemon-start", check=True)
        # --all would also stop old-layout daemons on this machine; the
        # instance part is checked through the client functions instead.
        for instance in client._instances_for_all():
            self.assertEqual(client._stop_instance(instance, graceful=True, timeout=30), "stopped")
        self.assertEqual(json.loads(self.main("--daemon-list", "--json").stdout)["instances"], [])

    def test_logs_events_and_prune(self) -> None:
        project = self.project("l", 4)
        self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, check=True)
        log = self.main("--daemon-logs", "20", check=True).stdout
        self.assertIn("finish code=0", log)
        req = self.status()["recent_compilations"][-1]["id"]
        one = self.main("--daemon-logs", req, check=True).stdout
        self.assertIn("--- stdout ---", one)
        events = json.loads(self.main("--daemon-events", "--json", check=True).stdout)
        self.assertIsInstance(events, list)
        # A crashed instance: instance.json of a dead process.
        dead = Path(client._instance("crashed").dir)
        dead.mkdir(parents=True)
        (dead / "instance.json").write_text(json.dumps({"pid": 999999, "name": "crashed", "checkout": str(ROOT)}))
        listed = self.main("--daemon-list").stdout
        self.assertIn("crashed (files left", listed)
        old_log = Path(client._instance().state_dir) / "req" / "old.log"
        old_log.write_text("x")
        os.utime(old_log, (time.time() - 30 * 86400,) * 2)
        pruned = json.loads(self.main("--daemon-prune", "--json", "--days", "7", check=True).stdout)
        self.assertTrue(any(str(dead) in row for row in pruned))
        self.assertIn(str(old_log), pruned)
        self.assertTrue(self.status()["running"])  # the live one is kept

    def test_require_daemon(self) -> None:
        blocked = dict(self.env)
        (self.tmp / "file").write_text("")
        blocked["L2_DAEMON_RUNTIME_DIR"] = str(self.tmp / "file" / "run")
        project = self.project("q", 6)
        proc = self.main("p.sl", "-I", "lib", "--no-artifact", "--daemon", cwd=project, env=blocked)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cannot be used", proc.stderr)
        local = self.main("p.sl", "-I", "lib", "--no-artifact", cwd=project, env=blocked)
        self.assertEqual(local.returncode, 0, local.stderr)  # without --daemon: compiled locally
        both = self.main("p.sl", "--daemon", "--no-daemon", cwd=project)
        self.assertNotEqual(both.returncode, 0)
        mixed = self.main("--daemon-status", "p.sl")
        self.assertNotEqual(mixed.returncode, 0)


class LocalCommandTests(DaemonTestCase):
    def test_help_runs_locally_and_lists_daemon_options(self) -> None:
        proc = self.main("--help", check=True)
        self.assertTrue(proc.stdout.startswith("usage: main.py"), proc.stdout[:80])
        for flag in ("--daemon-status", "--daemon-kill", "--no-daemon", "--daemon-name"):
            self.assertIn(flag, proc.stdout)
        self.assertFalse(Path(client._instance().socket).exists(), "--help contacted the daemon")

    def test_run_runs_locally(self) -> None:
        out = self.tmp / "hello"
        proc = self.main("tests/general/hello.sl", "--run", "-o", str(out), check=True)
        self.assertIn("hello world", proc.stdout)
        self.assertFalse(Path(client._instance().socket).exists(), "--run went through the daemon")

    def test_compiler_rejects_daemon_options(self) -> None:
        proc = self.main("--daemon-status", main_py=ROOT / "l2_main.py")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("handled by main.py", proc.stderr)


class LifecycleTests(DaemonTestCase):
    def test_idle_timeout_exits(self) -> None:
        env = dict(self.env, L2_DAEMON_IDLE_TIMEOUT="0.05")  # 3 seconds
        self.main("--daemon-start", env=env, check=True)
        pid = self.status()["pid"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and client._alive(pid):
            time.sleep(0.2)
        self.assertFalse(client._alive(pid))
        self.assertFalse(Path(client._instance().info).exists())
        self.assertIn("exiting", self.main("--daemon-logs", "5").stdout)

    def test_extra_idle_workers_are_stopped(self) -> None:
        env = dict(self.env, L2_DAEMON_WORKERS="3", L2_DAEMON_WORKER_IDLE="1")
        projects = [self.project(f"w{index}", index) for index in range(3)]
        (self.tmp / "slow.sl").write_text(
            "import stdlib.sl\nword spin 0 300000 for 1 + end drop end\ncompile-time spin\nword main 0 end\n")
        self.main("--daemon-start", env=env, check=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda directory: self.main(str(self.tmp / "slow.sl"), "--no-artifact", cwd=directory,
                                                      env=env, check=True), projects))
        peak = len(self.status()["worker_details"])
        self.assertGreaterEqual(peak, 2)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and len(self.status()["worker_details"]) > 1:
            time.sleep(0.5)
        self.assertEqual(len(self.status()["worker_details"]), 1)

    def test_hung_daemon_is_replaced(self) -> None:
        self.main("--daemon-start", check=True)
        pid = self.status()["pid"]
        os.kill(pid, signal.SIGSTOP)
        try:
            project = self.project("h", 8)
            start = time.monotonic()
            proc = self.main("p.sl", "-I", "lib", "-o", "prog", "--daemon", cwd=project)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertLess(time.monotonic() - start, 30)
            new_pid = self.status()["pid"]
            self.assertNotEqual(new_pid, pid)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.assertFalse(client._alive(pid))


class WarmPrefixMemoryTests(unittest.TestCase):
    def test_evicted_prefixes_are_freed(self) -> None:
        """A worker keeps a few prefixes and freezes them for the garbage
        collector; replacing one must not leave the old one frozen forever."""
        import gc
        if not hasattr(gc, "get_freeze_count"):
            self.skipTest("no gc.freeze on this interpreter")
        script = (
            "import gc, sys; sys.path.insert(0, sys.argv[1]); import l2_main as m\n"
            "counts = []\n"
            "for _ in range(3):\n"
            "    for d in 'ABC':\n"
            "        m._warm_stdlib_prepare(argv=['tests/general/hello.sl', '-D', d])\n"
            "    counts.append(gc.get_freeze_count())\n"
            "print(*counts)\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("L2_")}
        env["L2_DAEMON_WARM_PREFIXES"] = "2"
        result = subprocess.run([sys.executable, "-c", script, str(ROOT)], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second, third = map(int, result.stdout.split())
        self.assertLess(third, first * 1.2, (first, second, third))


class LegacyDaemonTests(DaemonTestCase):
    def fake_legacy(self, directory: Path, script: str, idle_seconds: float) -> subprocess.Popen:
        """A process that looks like an old-layout daemon (cmdline and
        build/logs/daemon.log), working in `directory`."""
        (directory / "build" / "logs").mkdir(parents=True)
        log = directory / "build" / "logs" / "daemon.log"
        log.write_text("x")
        os.utime(log, (time.time() - idle_seconds,) * 2)
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", script, "--daemon-serve",
                                 "--socket", "build/.l2_daemon.sock", "--pid", "build/.l2_daemon.pid"], cwd=directory)

    def test_old_ones_are_stopped_fresh_ones_kept(self) -> None:
        cases = {
            "other_old": (self.tmp / "o1", "l2_main.py", 3600, True),
            "other_recent": (self.tmp / "o2", "l2_main.py", 60, False),
            "same_quiet": (self.tmp / "s1", str(ROOT / "l2_main.py"), 120, True),
            "same_recent": (self.tmp / "s2", str(ROOT / "l2_main.py"), 5, False),
        }
        procs = {}
        for name, (directory, script, idle, _expected) in cases.items():
            directory.mkdir()
            procs[name] = self.fake_legacy(directory, script, idle)
        try:
            time.sleep(0.3)
            found = {row["cwd"] for row in client._legacy_daemons(str(self.tmp))}
            self.assertEqual(found, {str(directory.resolve()) for directory, *_ in cases.values()})
            stopped = {row["cwd"] for row in client._stop_old_legacy_daemons(str(self.tmp))}
            for name, (directory, _script, _idle, expected) in cases.items():
                self.assertEqual(str(directory.resolve()) in stopped, expected, name)
                if expected:
                    procs[name].wait(timeout=5)
                else:
                    self.assertIsNone(procs[name].poll(), name)
        finally:
            for proc in procs.values():
                proc.kill()
                proc.wait()


class PermissionTests(DaemonTestCase):
    def test_private_directories_and_socket(self) -> None:
        self.main("--daemon-start", check=True)
        instance = client._instance()
        for path in (client._runtime_root(), os.path.dirname(instance.dir), instance.dir):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700, path)
        self.assertEqual(os.stat(instance.socket).st_mode & 0o777, 0o600)

    def test_symlinked_runtime_dir_is_refused(self) -> None:
        target = self.tmp / "target"
        target.mkdir()
        link = self.tmp / "link"
        link.symlink_to(target)
        with self.assertRaises(client._DaemonUnavailable):
            client._private_dir(str(link))

    def test_open_runtime_dir_is_made_private(self) -> None:
        directory = self.tmp / "open"
        directory.mkdir(mode=0o755)
        os.chmod(directory, 0o755)
        client._private_dir(str(directory))
        self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)

    def test_peer_uid_is_this_user(self) -> None:
        import socket
        import l2_main
        left, right = socket.socketpair(socket.AF_UNIX)
        try:
            self.assertEqual(l2_main._peer_uid(left), os.getuid())
        finally:
            left.close()
            right.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
