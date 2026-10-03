#!/usr/bin/env python3
"""Thin daemon client for the integrated L2 compiler.

Imports continue to re-export l2_main symbols. Script execution routes requests
through the background daemon by default, with a direct compile fallback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


_DAEMON_SOCKET = "build/.l2_daemon.sock"
_DAEMON_PID = "build/.l2_daemon.pid"
_DAEMON_TIMEOUT = 120.0
_DAEMON_LEASE_TTL = 2.5


def _split_control_args(argv: Sequence[str]) -> tuple[list[str], bool]:
    passthrough: list[str] = []
    force_local = False

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--no-daemon":
            force_local = True
            i += 1
            continue
        passthrough.append(tok)
        i += 1

    return passthrough, force_local


# Flags whose semantics require direct stdio access (TTY, long-running server,
# child process pass-through) and must therefore bypass the daemon subprocess.
_LOCAL_ONLY_FLAGS = frozenset(
    {
        "--docs",
        "--docs-serve",
        "--repl",
        "--run",
        "--dbg",
    }
)


def _requires_local_execution(argv) -> bool:
    for tok in argv:
        if tok in _LOCAL_ONLY_FLAGS:
            return True
        if tok == "--help" or tok == "-h":
            # argparse help is trivial; still fine through daemon, but keep
            # it local so `python main.py --help` never depends on daemon health.
            return True
    return False


def _read_stdin_for_daemon():
    try:
        if sys.stdin.isatty():
            return None
    except Exception:
        return None
    try:
        return sys.stdin.read()
    except Exception:
        return None


def _daemon_default_enabled() -> bool:
    return os.environ.get("L2_DAEMON", "1").strip().lower() not in {"0", "false", "no", "off"}


def _daemon_send(payload, timeout: float = _DAEMON_TIMEOUT):
    import json
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(_DAEMON_SOCKET)
        conn.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        raw = conn.makefile("rb").readline(4 * 1024 * 1024)
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("invalid compiler daemon response")
    return response


def _daemon_ping() -> bool:
    try:
        return bool(_daemon_send({"cmd": "ping"}, 0.2).get("ok"))
    except Exception:
        return False


def _proc_start_time_ns(pid: int) -> Optional[int]:
    import os

    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    if ")" not in stat_text:
        return None
    rest = stat_text.rsplit(")", 1)[1].strip()
    fields = rest.split()
    if len(fields) < 20:
        return None
    try:
        start_ticks = int(fields[19])
    except ValueError:
        return None
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (AttributeError, OSError, ValueError):
        clk_tck = 100
    if clk_tck <= 0:
        clk_tck = 100
    return int(start_ticks * (1_000_000_000 / clk_tck))


def _daemon_lease_pid(*, socket_path: str = _DAEMON_SOCKET, ttl: float = _DAEMON_LEASE_TTL) -> Optional[int]:
    """Return the live daemon PID only if the lease matches the current daemon instance.

    The lease is considered valid only when it is fresh, the process is still
    alive, the command line still marks it as a daemon, and the recorded process
    start time matches /proc/<pid>/stat. This avoids accepting a stale PID that
    has been reused by another process.
    """
    import json
    import os
    import time

    lease_path = Path(socket_path + ".lease")
    try:
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    ts_ns = payload.get("ts_ns")
    start_time_ns = payload.get("start_time_ns")
    if not isinstance(pid, int) or not isinstance(ts_ns, int) or not isinstance(start_time_ns, int):
        return None
    if time.time_ns() - ts_ns > int(ttl * 1_000_000_000):
        return None
    try:
        proc_path = Path(f"/proc/{pid}")
        if not proc_path.exists():
            return None
        cmdline = proc_path.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except OSError:
        return None
    daemon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l2_main.py")
    if "--daemon-serve" not in cmdline or daemon_path not in cmdline:
        return None
    proc_start_ns = _proc_start_time_ns(pid)
    if proc_start_ns is None or proc_start_ns != start_time_ns:
        return None
    return pid


def _shutdown_daemon(*, socket_path: str = _DAEMON_SOCKET, pid_path: str = _DAEMON_PID, timeout: float = 2.0) -> None:
    import errno
    import os
    import signal
    import time

    try:
        _daemon_send({"cmd": "shutdown"}, socket_path, 1.0)
    except Exception:
        pass

    try:
        pid_text = Path(pid_path).read_text(encoding="utf-8").strip()
        pid = int(pid_text)
    except (OSError, ValueError):
        pid = None

    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pid = None
        if pid is not None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except OSError as exc:
                    if exc.errno in {errno.ESRCH}:
                        break
                time.sleep(0.02)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    for artifact in (Path(socket_path), Path(pid_path), Path(socket_path + ".lease")):
        try:
            artifact.unlink()
        except OSError:
            pass


def _ensure_daemon() -> bool:
    if _daemon_ping():
        return True

    import fcntl
    import os
    import signal
    import subprocess
    import time

    os.makedirs(os.path.dirname(_DAEMON_SOCKET) or ".", exist_ok=True)
    lock_fd = os.open(_DAEMON_SOCKET + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    owns_lock = False
    try:
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            if _daemon_ping():
                return True
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owns_lock = True
                break
            except BlockingIOError:
                time.sleep(0.01)
        if not owns_lock:
            if _daemon_ping():
                return True
            endpoint_missing = not os.path.exists(_DAEMON_SOCKET) and not os.path.exists(_DAEMON_PID)
            stale_pid = _daemon_lease_pid(socket_path=_DAEMON_SOCKET) if endpoint_missing else None
            if stale_pid is None:
                return False

            # Give the daemon one bounded grace period to finish a drain/reload
            # before we explicitly terminate the stale process.
            deadline = time.monotonic() + 11.0
            while time.monotonic() < deadline:
                if _daemon_ping():
                    return True
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    owns_lock = True
                    break
                except BlockingIOError:
                    time.sleep(0.01)
            if not owns_lock:
                if _daemon_ping():
                    return True
                _shutdown_daemon(socket_path=_DAEMON_SOCKET, pid_path=_DAEMON_PID)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if _daemon_ping():
                        return True
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        owns_lock = True
                        break
                    except BlockingIOError:
                        time.sleep(0.01)
                if not owns_lock:
                    return _daemon_ping()
        if _daemon_ping():
            return True
        try:
            os.unlink(_DAEMON_SOCKET)
        except OSError:
            pass
        daemon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l2_main.py")
        env = os.environ.copy()
        env["L2_DAEMON_LOCK_FD"] = str(lock_fd)
        subprocess.Popen(
            [sys.executable, daemon_path, "--daemon-serve", "--socket", _DAEMON_SOCKET, "--pid", _DAEMON_PID],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(lock_fd,),
            start_new_session=True,
            env=env,
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if _daemon_ping():
                return True
            time.sleep(0.01)
        return False
    finally:
        os.close(lock_fd)


def _run_via_daemon(argv):
    stdin_data = _read_stdin_for_daemon()
    response = None
    for _attempt in range(2):
        if not _ensure_daemon():
            continue
        try:
            response = _daemon_send({
                "cmd": "run",
                "argv": list(argv),
                "wants_color": bool(getattr(sys.stderr, "isatty", lambda: False)()),
                "stdin": stdin_data,
                "tool_path": os.environ.get("PATH", ""),
            })
            break
        except Exception:
            # A self-reload can close the socket between start and request.
            # Reconnect once so the normal client path remains transparent.
            continue
    if response is None:
        return None

    if not response.get("ok"):
        err = str(response.get("error", "daemon request failed"))
        if err:
            if not err.endswith("\n"):
                err += "\n"
            sys.stderr.write(err)
        return 1

    out = str(response.get("stdout", ""))
    err = str(response.get("stderr", ""))
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)

    code = response.get("code", 1)
    try:
        return int(code)
    except Exception:
        return 1


if __name__ == "__main__":
    argv, force_local = _split_control_args(sys.argv[1:])

    if _requires_local_execution(argv):
        force_local = True

    if not force_local:
        daemon_enabled = _daemon_default_enabled()
        if daemon_enabled:
            result = _run_via_daemon(argv)
            if result is not None:
                raise SystemExit(result)

    from l2_main import cli as _entry_cli

    raise SystemExit(_entry_cli(argv))
else:
    from l2_main import *  # noqa: F401,F403
    from l2_main import main as _entry_main
