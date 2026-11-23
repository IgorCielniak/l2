"""Command-line client of the L2 compiler (what main.py runs).

One daemon serves every directory a checkout is used from: its socket, lock
and lease live in a per-user runtime directory keyed by the checkout, the
Python interpreter and an instance name (`--daemon-name`, default
"default"); requests carry the caller's working directory.  The --daemon-*
flags manage it (status, start, stop, kill, restart, reload, list, logs,
events, clear, prune, config).

This is a module rather than part of main.py so Python caches its bytecode:
the script a process starts with is compiled from source on every run.
"""

from __future__ import annotations

import os
import sys


def _read_text(path: str) -> str:
    # Plain open() rather than pathlib: the client imports as little as
    # possible, since every daemon compile pays for its start-up.
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# Environment variables that apply to one compile; the daemon runs each job
# with the client's values (set or unset).  Same list as l2_main._DAEMON_JOB_ENV.
_DAEMON_JOB_ENV = ("L2_NASM_PARALLEL", "L2_ASM_CACHE", "L2_ASM_CACHE_DIR", "L2_CT_MERGED_JIT")
# Settings a daemon takes when it starts (shown by --daemon-config and
# recorded in its instance.json).
_DAEMON_START_ENV = ("L2_NO_WARM_PREFIX", "L2_DAEMON_WARM_PREFIXES", "L2_NO_ASM_PREWARM",
                     "L2_DAEMON_NO_AUTO_RELOAD", "L2_DAEMON_LOG_LEVEL", "L2_DAEMON_DRAIN_TIMEOUT")
# Request/response format between this client and l2_main's daemon.
_DAEMON_PROTOCOL = 2

_DAEMON_CONNECT_TIMEOUT = 2.0
_DAEMON_PING_TIMEOUT = 1.0
_DAEMON_LEASE_TTL = 2.5
_DAEMON_DEFAULT_IDLE_MINUTES = 30.0
# Daemons of the old layout (one per working directory, under build/).
_LEGACY_SOCKET = "build/.l2_daemon.sock"
_LEGACY_IDLE_SECONDS = 30 * 60.0
_LEGACY_SAME_CHECKOUT_QUIET_SECONDS = 60.0

# Modes that execute `main` at compile time and may therefore read stdin.
_STDIN_EXECUTION_FLAGS = frozenset({"--ct-run-main", "--script", "--leak-check"})


# ---------------------------------------------------------------------------
# Where a daemon lives
# ---------------------------------------------------------------------------

class _DaemonUnavailable(Exception):
    """The daemon cannot be used (for example an unsafe runtime directory)."""


class _Instance:
    """Paths and identity of one daemon instance."""

    __slots__ = ("name", "scope", "checkout", "key", "dir", "socket", "lock", "lease", "pid", "info",
                 "state_dir", "explicit")

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}


def _checkout_dir() -> str:
    return os.path.realpath(os.path.dirname(os.path.abspath(__file__)))


def _crc(text: str) -> str:
    import zlib
    return format(zlib.crc32(text.encode("utf-8", "surrogateescape")) & 0xFFFFFFFF, "08x")


def _checkout_tag() -> str:
    """Instances of one checkout run by one interpreter share this tag: the
    checkout's directory name and a hash of its real path, the interpreter
    and its version (a daemon runs the code and Python it was started with)."""
    checkout = _checkout_dir()
    base = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in os.path.basename(checkout))[:16] or "l2"
    identity = "\0".join([checkout, os.path.realpath(sys.executable), sys.version])
    return f"{base}-{_crc(identity)}"


def _runtime_root() -> str:
    override = os.environ.get("L2_DAEMON_RUNTIME_DIR")
    if override:
        return override
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "l2")
    return os.path.join("/tmp", f"l2-{os.getuid()}")


def _state_root() -> str:
    override = os.environ.get("L2_DAEMON_STATE_DIR")
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "l2")


def _private_dir(path: str) -> None:
    """Create path (and parents) owned by this user and closed to others;
    raise _DaemonUnavailable when an existing directory is not."""
    if not os.path.isdir(path):
        try:
            os.makedirs(path, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise _DaemonUnavailable(f"cannot create {path}: {exc}") from exc
    st = os.lstat(path)
    import stat as _stat
    if not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise _DaemonUnavailable(f"{path} must be a directory owned by you (not a symlink)")
    if st.st_mode & 0o077:
        try:
            os.chmod(path, 0o700)  # ours: close it to other users
        except OSError as exc:
            raise _DaemonUnavailable(f"cannot make {path} private: {exc}") from exc


def _valid_name(name: str) -> bool:
    return 0 < len(name) <= 64 and all(ch.isalnum() or ch in "-_." for ch in name) and name[0] not in ".-"


def _project_root(start: str) -> str:
    current = start
    while True:
        if os.path.exists(os.path.join(current, ".git")) or os.path.exists(os.path.join(current, "l2.toml")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return start
        current = parent


def _scope_part(scope: str) -> str:
    if scope == "checkout":
        return ""
    cwd = os.path.realpath(os.getcwd())
    if scope == "project":
        return f"p{_crc(_project_root(cwd))}-"
    return f"d{_crc(cwd)}-"


def _instance(name: str | None = None, *, create: bool = False) -> _Instance:
    """The daemon instance a command talks to: L2_DAEMON_SOCKET (explicit
    socket path), else the named (--daemon-name / L2_DAEMON_NAME, default
    "default") instance of this checkout in the scope L2_DAEMON_SCOPE
    (checkout, project or directory; default checkout)."""
    inst = _Instance()
    inst.checkout = _checkout_dir()
    inst.name = name or os.environ.get("L2_DAEMON_NAME") or "default"
    if not _valid_name(inst.name):
        raise _DaemonUnavailable(f"invalid daemon name {inst.name!r} (letters, digits, '-', '_', '.')")
    scope = (os.environ.get("L2_DAEMON_SCOPE") or "checkout").strip().lower()
    if scope not in ("checkout", "project", "directory"):
        raise _DaemonUnavailable(f"L2_DAEMON_SCOPE must be checkout, project or directory, not {scope!r}")
    inst.scope = scope
    explicit = os.environ.get("L2_DAEMON_SOCKET")
    inst.explicit = bool(explicit)
    if explicit:
        inst.socket = os.path.abspath(explicit)
        inst.dir = os.path.dirname(inst.socket)
        inst.key = "explicit"
        inst.state_dir = os.path.join(inst.dir, "state")
    else:
        inst.key = os.path.join(_checkout_tag(), _scope_part(scope) + inst.name)
        root = _runtime_root()
        inst.dir = os.path.join(root, inst.key)
        inst.socket = os.path.join(inst.dir, "daemon.sock")
        inst.state_dir = os.path.join(_state_root(), inst.key)
        if create:
            _private_dir(root)
            _private_dir(os.path.dirname(inst.dir))
            _private_dir(inst.dir)
    inst.lock = inst.socket + ".lock"
    inst.lease = inst.socket + ".lease"
    inst.pid = os.path.join(inst.dir, "daemon.pid")
    inst.info = os.path.join(inst.dir, "instance.json")
    return inst


_CURRENT: _Instance | None = None


def _current() -> _Instance:
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = _instance()
    return _CURRENT


def _read_json(path: str):
    import json
    try:
        return json.loads(_read_text(path))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Talking to a daemon
# ---------------------------------------------------------------------------

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
    # Modes that run `main` at compile time read stdin interactively when it is
    # a terminal; the daemon cannot forward a TTY (it only forwards stdin read
    # to EOF up front), so such runs would see immediate EOF.  Run them locally.
    if any(tok in _STDIN_EXECUTION_FLAGS for tok in argv):
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                return True
        except Exception:
            pass
    return False


def _read_stdin_for_daemon(argv):
    """Return stdin data to forward to the daemon, or None.

    Only modes that execute `main` at compile time consume stdin, so only they
    read it to EOF (the same blocking behavior as a local run).  Otherwise a
    caller's idle stdin pipe would block the compile forever; a regular file is
    still forwarded because reading it can never block.
    """
    import stat

    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        if not any(tok in _STDIN_EXECUTION_FLAGS for tok in argv):
            if not stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
                return None
        return sys.stdin.read()
    except Exception:
        return None


def _restore_stdin_for_local(stdin_data) -> None:
    """Re-supply stdin already consumed for the daemon to a local fallback,
    on both sys.stdin and file descriptor 0 (CT code may read fd 0 directly)."""
    if stdin_data is None:
        return
    import io
    import tempfile

    data = tempfile.TemporaryFile()
    data.write(stdin_data.encode("utf-8"))
    data.seek(0)
    os.dup2(data.fileno(), 0)
    data.close()
    sys.stdin = io.TextIOWrapper(os.fdopen(os.dup(0), "rb"), encoding="utf-8")


def _daemon_default_enabled() -> bool:
    return os.environ.get("L2_DAEMON", "1").strip().lower() not in {"0", "false", "no", "off"}


class _DaemonNotDelivered(Exception):
    """The request never reached the daemon, so retrying it is safe."""


def _daemon_send(payload, timeout: float = _DAEMON_CONNECT_TIMEOUT, response_timeout=None, socket_path=None):
    """Send one request and read one response line.

    `timeout` bounds connecting and sending; `response_timeout` bounds waiting
    for the reply (None waits until the daemon answers or closes the
    connection, e.g. because it died).  Raises _DaemonNotDelivered when the
    request was not sent.
    """
    import json
    import socket

    path = socket_path or _current().socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        try:
            conn.settimeout(timeout)
            _connect_unix(conn, path)
            conn.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        except OSError as exc:
            raise _DaemonNotDelivered(str(exc)) from exc
        conn.settimeout(response_timeout)
        raw = conn.makefile("rb").readline()
    if not raw:
        raise RuntimeError("compiler daemon closed the connection without a response")
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("invalid compiler daemon response")
    return response


def _connect_unix(conn, path: str) -> None:
    """connect() to a Unix socket; a path longer than the 107 bytes a socket
    address holds is reached through /proc/self/fd/<directory>/<name>."""
    if len(os.fsencode(path)) < 100:
        conn.connect(path)
        return
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        conn.connect(f"/proc/self/fd/{fd}/{os.path.basename(path)}")
    finally:
        os.close(fd)


def _daemon_ping(socket_path=None) -> bool:
    try:
        response = _daemon_send({"cmd": "ping"}, _DAEMON_PING_TIMEOUT, _DAEMON_PING_TIMEOUT, socket_path)
    except Exception:
        return False
    return bool(response.get("ok")) and not response.get("stale")


def _proc_start_time_ns(pid: int) -> int | None:
    try:
        stat_text = _read_text(f"/proc/{pid}/stat")
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


def _proc_cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return [part.decode("utf-8", "replace") for part in handle.read().split(b"\0") if part]
    except OSError:
        return []


def _daemon_lease_pid(*, socket_path: str | None = None, ttl: float = _DAEMON_LEASE_TTL) -> int | None:
    """Return the live daemon PID only if the lease matches the current daemon instance.

    The lease is considered valid only when it is fresh, the process is still
    alive, the command line still marks it as a daemon of this checkout, and
    the recorded process start time matches /proc/<pid>/stat. This avoids
    accepting a stale PID that has been reused by another process.
    """
    import time

    payload = _read_json((socket_path or _current().socket) + ".lease")
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    ts_ns = payload.get("ts_ns")
    start_time_ns = payload.get("start_time_ns")
    if not isinstance(pid, int) or not isinstance(ts_ns, int) or not isinstance(start_time_ns, int):
        return None
    if time.time_ns() - ts_ns > int(ttl * 1_000_000_000):
        return None
    if not _is_daemon_process(pid, start_time_ns):
        return None
    return pid


def _is_daemon_process(pid: int, start_time_ns: int | None) -> bool:
    """pid is still the daemon of this checkout that was started at
    start_time_ns (a recycled pid has another start time)."""
    cmdline = " ".join(_proc_cmdline(pid))
    daemon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l2_main.py")
    if "--daemon-serve" not in cmdline or daemon_path not in cmdline:
        return False
    if start_time_ns is None:
        return True
    return _proc_start_time_ns(pid) == start_time_ns


def _wait_gone(pid: int, timeout: float) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not os.path.exists(f"/proc/{pid}") or _proc_state(pid) == "Z":
            return True
        time.sleep(0.02)
    return not os.path.exists(f"/proc/{pid}")


def _proc_state(pid: int) -> str:
    try:
        return _read_text(f"/proc/{pid}/stat").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return ""


def _shutdown_daemon(*, socket_path: str | None = None, pid_path: str | None = None, timeout: float = 2.0) -> None:
    """Ask the daemon to stop; terminate it (SIGTERM, then SIGKILL) when it
    is still running after `timeout` seconds; remove its files."""
    import signal

    inst = None if socket_path is not None else _current()
    socket_path = socket_path or inst.socket
    pid_path = pid_path or (inst.pid if inst is not None else os.path.join(os.path.dirname(socket_path), "daemon.pid"))
    # The pid first: the daemon removes its pid file as soon as it stops
    # serving, then still drains jobs and saves its state before exiting.
    try:
        pid = int(_read_text(pid_path).strip())
    except (OSError, ValueError):
        pid = None
    try:
        _daemon_send({"cmd": "shutdown"}, 1.0, 1.0, socket_path)
    except Exception:
        pass

    if pid is not None and _is_daemon_process(pid, None):
        if not _wait_gone(pid, timeout):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pid = None
            if pid is not None and not _wait_gone(pid, timeout):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    for artifact in (socket_path, pid_path, socket_path + ".lease"):
        try:
            os.unlink(artifact)
        except OSError:
            pass


def _daemon_settings() -> dict:
    """Settings for a daemon started now: worker limit and idle timeout from
    the --daemon-workers / --daemon-idle-timeout flags or the environment."""
    workers = _FLAG_SETTINGS.get("workers") or os.environ.get("L2_DAEMON_WORKERS")
    idle = _FLAG_SETTINGS.get("idle_timeout") or os.environ.get("L2_DAEMON_IDLE_TIMEOUT")
    try:
        workers = max(1, int(workers)) if workers else max(1, min(os.cpu_count() or 1, 8))
    except ValueError:
        raise _DaemonUnavailable(f"invalid worker count {workers!r}")
    try:
        idle = float(idle) if idle not in (None, "") else _DAEMON_DEFAULT_IDLE_MINUTES
    except ValueError:
        raise _DaemonUnavailable(f"invalid idle timeout {idle!r} (minutes)")
    return {"workers": workers, "idle_timeout": max(0.0, idle)}


_FLAG_SETTINGS: dict = {}


def _spawn_daemon(inst: _Instance, lock_fd: int, *, prewarm: bool = False) -> None:
    import subprocess

    settings = _daemon_settings()
    daemon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "l2_main.py")
    env = os.environ.copy()
    env["L2_DAEMON_LOCK_FD"] = str(lock_fd)
    # Per-compile settings come with each request, not from the daemon's start.
    for name in _DAEMON_JOB_ENV:
        env.pop(name, None)
    subprocess.Popen(
        [sys.executable, daemon_path, "--daemon-serve", "--socket", inst.socket, "--pid", inst.pid,
         "--state-dir", inst.state_dir, "--name", inst.name, "--scope", inst.scope,
         "--workers", str(settings["workers"]), "--idle-timeout", str(settings["idle_timeout"]),
         *(["--prewarm"] if prewarm else [])],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        pass_fds=(lock_fd,),
        start_new_session=True,
        cwd=inst.dir if os.path.isdir(inst.dir) else "/",
        env=env,
    )


def _ensure_daemon(inst: _Instance | None = None, *, prewarm: bool = False) -> bool:
    """A daemon answers on the instance's socket (started now if needed).
    Concurrent callers, from any directory, end up with one daemon: the start
    is serialised by a lock in the instance directory."""
    inst = inst or _current()
    if _daemon_ping(inst.socket):
        return True

    import fcntl
    import time

    if not inst.explicit:
        _instance(inst.name, create=True)  # private runtime directories
    else:
        os.makedirs(inst.dir, exist_ok=True)
    lock_fd = os.open(inst.lock, os.O_CREAT | os.O_RDWR, 0o600)
    owns_lock = False
    try:
        deadline = time.monotonic() + 0.35
        while time.monotonic() < deadline:
            if _daemon_ping(inst.socket):
                return True
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owns_lock = True
                break
            except BlockingIOError:
                time.sleep(0.01)
        if not owns_lock:
            if _daemon_ping(inst.socket):
                return True
            endpoint_missing = not os.path.exists(inst.socket) and not os.path.exists(inst.pid)
            stale_pid = _daemon_lease_pid(socket_path=inst.socket) if endpoint_missing else None
            if stale_pid is None:
                # Another client is starting it: wait for it to answer.
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if _daemon_ping(inst.socket):
                        return True
                    time.sleep(0.02)
                if not os.path.exists(inst.socket):
                    return False
                # A daemon that holds the lock and has its socket but has not
                # answered for seconds is hung (a draining daemon removes its
                # socket first): replace it.
                info = _read_json(inst.info)
                if not (isinstance(info, dict) and isinstance(info.get("pid"), int)
                        and _is_daemon_process(info["pid"], info.get("start_time_ns"))):
                    return False
                _terminate_tree(info["pid"])
                _remove_instance_files(inst)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    owns_lock = True
                except BlockingIOError:
                    return _daemon_ping(inst.socket)

            # Give the daemon one bounded grace period to finish a drain/reload
            # before we explicitly terminate the stale process.
            deadline = time.monotonic() + 11.0
            while time.monotonic() < deadline:
                if _daemon_ping(inst.socket):
                    return True
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    owns_lock = True
                    break
                except BlockingIOError:
                    time.sleep(0.01)
            if not owns_lock:
                if _daemon_ping(inst.socket):
                    return True
                _shutdown_daemon(socket_path=inst.socket, pid_path=inst.pid)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if _daemon_ping(inst.socket):
                        return True
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        owns_lock = True
                        break
                    except BlockingIOError:
                        time.sleep(0.01)
                if not owns_lock:
                    return _daemon_ping(inst.socket)
        if _daemon_ping(inst.socket):
            return True
        _unresponsive_cleanup(inst)
        try:
            os.unlink(inst.socket)
        except OSError:
            pass
        _spawn_daemon(inst, lock_fd, prewarm=prewarm)
        try:
            _stop_old_legacy_daemons()
        except Exception:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _daemon_ping(inst.socket):
                return True
            time.sleep(0.01)
        return False
    finally:
        os.close(lock_fd)


def _unresponsive_cleanup(inst: _Instance) -> None:
    """With the start lock held and no answer on the socket, a daemon process
    still recorded for this instance is hung: terminate it before starting a
    new one."""
    info = _read_json(inst.info)
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return
    pid = info["pid"]
    if pid != os.getpid() and _is_daemon_process(pid, info.get("start_time_ns")):
        _terminate_tree(pid)


def _run_via_daemon(argv, stdin_data, *, required: bool = False):
    """Run through the daemon; return the exit code, or None to compile locally."""
    response = None
    for _attempt in range(2):
        try:
            if not _ensure_daemon():
                continue
        except _DaemonUnavailable as exc:
            if required:
                raise
            _verbose_note(argv, f"daemon unavailable ({exc})")
            return None
        try:
            response = _daemon_send(_run_payload(argv, stdin_data))
            break
        except _DaemonNotDelivered:
            # A reload can remove the socket between the ping and the request;
            # the request was not sent, so reconnecting once is safe.
            continue
        except Exception:
            # Delivered but no usable answer (daemon died mid-request): do not
            # resend; compile locally instead.
            if required:
                raise _DaemonUnavailable("the daemon did not answer the request")
            return None
    if response is None:
        if required:
            raise _DaemonUnavailable("could not start or reach the daemon")
        return None

    if response.get("fallback"):
        # The daemon could not run the job (reloading after a code change, or
        # a worker crashed); compile locally so the result is still correct.
        if required:
            raise _DaemonUnavailable(str(response.get("error", "compiler daemon failed")))
        if not response.get("stale"):
            sys.stderr.write(f"[warn] {response.get('error', 'compiler daemon failed')}; compiling locally\n")
        else:
            _verbose_note(argv, "daemon is reloading after a code change; compiling locally")
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


def _run_payload(argv, stdin_data) -> dict:
    umask = os.umask(0)
    os.umask(umask)
    return {
        "cmd": "run",
        "protocol": _DAEMON_PROTOCOL,
        "checkout": _checkout_dir(),
        "argv": list(argv),
        "cwd": os.getcwd(),
        "umask": umask,
        "wants_color": bool(getattr(sys.stderr, "isatty", lambda: False)()),
        "stdin": stdin_data,
        "tool_path": os.environ.get("PATH", ""),
        "job_env": {name: os.environ.get(name) for name in _DAEMON_JOB_ENV},
        "trusted_dirs": os.environ.get("L2_DAEMON_TRUSTED_DIRS"),
    }


def _verbose_note(argv, text: str) -> None:
    if any(tok in ("-v", "--verbose") or tok.startswith("--verbose=") for tok in argv):
        sys.stderr.write(f"[info] {text}\n")


# ---------------------------------------------------------------------------
# Processes: listing, stopping, killing
# ---------------------------------------------------------------------------

def _children(pid: int) -> list[int]:
    found = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            fields = _read_text(f"/proc/{entry}/stat").rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 1 and fields[1] == str(pid):
            found.append(int(entry))
    return found


def _descendants(pid: int) -> list[int]:
    result = []
    queue = [pid]
    while queue:
        current = queue.pop()
        for child in _children(current):
            result.append(child)
            queue.append(child)
    return result


def _terminate_tree(pid: int, timeout: float = 2.0) -> None:
    """SIGTERM the daemon, its workers and their jobs; SIGKILL whatever is
    left after `timeout` seconds."""
    import signal
    import time

    tree = [pid] + _descendants(pid)
    for target in tree:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_alive(target) for target in tree):
        time.sleep(0.02)
    for target in tree:
        if _alive(target):
            try:
                os.kill(target, signal.SIGKILL)
            except OSError:
                pass
    for target in tree:
        _wait_gone(target, 1.0)


def _alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}") and _proc_state(pid) not in ("Z", "")


def _remove_instance_files(inst: _Instance) -> None:
    for path in (inst.socket, inst.lease, inst.pid, inst.info):
        try:
            os.unlink(path)
        except OSError:
            pass


def _registered_instances(*, include_stopped: bool = False) -> list[dict]:
    """Every instance of this user that has an instance.json (written while
    it runs, removed when it stops), live or crashed; with include_stopped,
    also the directories of stopped instances."""
    rows = []
    root = _runtime_root()
    try:
        tags = sorted(os.listdir(root))
    except OSError:
        return rows
    for tag in tags:
        tag_dir = os.path.join(root, tag)
        try:
            names = sorted(os.listdir(tag_dir))
        except OSError:
            continue
        for name in names:
            directory = os.path.join(tag_dir, name)
            info = _read_json(os.path.join(directory, "instance.json"))
            if not isinstance(info, dict):
                if not include_stopped or not os.path.isdir(directory):
                    continue
                info = {}
            pid = info.get("pid")
            live = isinstance(pid, int) and _alive(pid) and "--daemon-serve" in " ".join(_proc_cmdline(pid)) \
                and (info.get("start_time_ns") is None or _proc_start_time_ns(pid) == info.get("start_time_ns"))
            socket_path = os.path.join(directory, "daemon.sock")
            rows.append({
                "kind": "instance",
                "dir": directory,
                "key": os.path.join(tag, name),
                "name": info.get("name", name),
                "checkout": info.get("checkout"),
                "python": info.get("python"),
                "pid": pid if live else None,
                "live": live,
                "socket": socket_path,
                "info": info,
            })
    return rows


def _legacy_daemons(under: str | None = None) -> list[dict]:
    """Daemons of the old layout (started without --state-dir: one per
    working directory, socket under build/); with `under`, only those whose
    working directory is inside it."""
    rows = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return rows
    uid = os.getuid()
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if os.stat(f"/proc/{entry}").st_uid != uid:
                continue
        except OSError:
            continue
        args = _proc_cmdline(pid)
        if "--daemon-serve" not in args or "--state-dir" in args:
            continue
        script = next((arg for arg in args if arg.endswith("l2_main.py")), None)
        if script is None:
            continue
        try:
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except OSError:
            continue
        if under is not None and not (cwd + os.sep).startswith(os.path.realpath(under) + os.sep):
            continue
        socket_arg = _LEGACY_SOCKET
        if "--socket" in args and args.index("--socket") + 1 < len(args):
            socket_arg = args[args.index("--socket") + 1]
        socket_path = os.path.join(cwd, socket_arg)
        script_path = os.path.realpath(script if os.path.isabs(script) else os.path.join(cwd, script))
        log = os.path.join(cwd, "build", "logs", "daemon.log")
        try:
            last_activity = os.stat(log).st_mtime
        except OSError:
            started = _proc_start_time_ns(pid)
            last_activity = None
            if started is not None:
                try:
                    boot = float(_read_text("/proc/stat").split("btime", 1)[1].split()[0])
                    last_activity = boot + started / 1e9
                except (OSError, IndexError, ValueError):
                    pass
        rows.append({
            "kind": "legacy",
            "pid": pid,
            "cwd": cwd,
            "socket": socket_path,
            "checkout": os.path.dirname(script_path),
            "last_activity": last_activity,
            "live": True,
        })
    return rows


def _stop_old_legacy_daemons(under: str | None = None) -> list[dict]:
    """Stop old-layout daemons nothing will use again: one of this checkout
    once it has been quiet for a minute (this client never talks to it), one
    of another checkout after the idle timeout (that checkout's older client
    may still use it).  Busy or recently used ones are left alone."""
    import time

    stopped = []
    now = time.time()
    checkout = _checkout_dir()
    for row in _legacy_daemons(under):
        idle = None if row["last_activity"] is None else now - row["last_activity"]
        limit = _LEGACY_SAME_CHECKOUT_QUIET_SECONDS if row["checkout"] == checkout else _LEGACY_IDLE_SECONDS
        if idle is None or idle < limit:
            continue
        try:
            status = _daemon_send({"cmd": "status"}, 0.5, 1.0, row["socket"])
            if status.get("active_jobs") or status.get("queued_jobs"):
                continue
        except Exception:
            pass
        try:
            _daemon_send({"cmd": "shutdown"}, 0.5, 0.5, row["socket"])
        except Exception:
            import signal
            try:
                os.kill(row["pid"], signal.SIGTERM)
            except OSError:
                pass
        stopped.append(row)
    return stopped


# ---------------------------------------------------------------------------
# --daemon-* management commands
# ---------------------------------------------------------------------------

_MANAGEMENT_FLAGS = (
    "--daemon-status", "--daemon-start", "--daemon-stop", "--daemon-kill", "--daemon-restart",
    "--daemon-reload", "--daemon-list", "--daemon-logs", "--daemon-events", "--daemon-clear",
    "--daemon-prune", "--daemon-config",
)


def _parse_daemon_args(argv: list[str]):
    """Split the --daemon-* options off argv.  Returns (remaining argv,
    management command or None, options)."""
    rest: list[str] = []
    command = None
    command_arg = None
    opts = {"json": False, "all": False, "follow": False, "require": False, "name": None, "days": None}
    i = 0
    while i < len(argv):
        tok = argv[i]
        value = None
        if "=" in tok and tok.startswith("--daemon"):
            tok, value = tok.split("=", 1)

        def take() -> str:
            nonlocal i
            if value is not None:
                return value
            if i + 1 >= len(argv):
                raise SystemExit(f"{tok} needs a value")
            i += 1
            return argv[i]

        if tok in _MANAGEMENT_FLAGS:
            if command is not None and command != tok:
                raise SystemExit(f"{command} and {tok} cannot be combined")
            command = tok
            if tok in ("--daemon-logs", "--daemon-events"):
                if value is not None:
                    command_arg = value
                elif i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    i += 1
                    command_arg = argv[i]
        elif tok == "--daemon-name":
            opts["name"] = take()
        elif tok == "--daemon-workers":
            _FLAG_SETTINGS["workers"] = take()
        elif tok == "--daemon-idle-timeout":
            _FLAG_SETTINGS["idle_timeout"] = take()
        elif tok == "--daemon":
            opts["require"] = True
        elif command is not None and tok in ("--json", "--all", "--follow"):
            opts[tok[2:]] = True
        elif command is not None and tok == "--days":
            opts["days"] = take()
        else:
            rest.append(argv[i])
        i += 1
    return rest, command, command_arg, opts


def _print_json(data) -> None:
    import json
    sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _fmt_bytes(count) -> str:
    if not isinstance(count, (int, float)):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return "-"


def _status_of(inst: _Instance) -> dict:
    info = _read_json(inst.info)
    result = {"name": inst.name, "scope": inst.scope, "socket": inst.socket, "state_dir": inst.state_dir,
              "checkout": inst.checkout, "running": False}
    try:
        status = _daemon_send({"cmd": "status"}, 1.0, 5.0, inst.socket)
    except Exception:
        status = None
    if isinstance(status, dict) and status.get("ok"):
        result.update(status)
        result["running"] = True
    elif isinstance(info, dict) and isinstance(info.get("pid"), int) and _is_daemon_process(info["pid"], info.get("start_time_ns")):
        result["running"] = True
        result["unresponsive"] = True
        result["pid"] = info["pid"]
    if isinstance(info, dict):
        result["instance"] = info
    return result


def _cmd_status(inst: _Instance, opts) -> int:
    if opts["all"]:
        return _cmd_list(opts)
    status = _status_of(inst)
    if opts["json"]:
        _print_json(status)
        return 0 if status["running"] else 3
    if not status["running"]:
        print(f"daemon '{inst.name}': not running")
        print(f"  socket   {inst.socket}")
        return 3
    if status.get("unresponsive"):
        print(f"daemon '{inst.name}': pid {status['pid']} is running but not answering "
              f"(--daemon-kill stops it; the next compile replaces it)")
        return 4
    info = status.get("instance") or {}
    print(f"daemon '{inst.name}': running, pid {status.get('pid')}, up {_fmt_duration(status.get('uptime_seconds'))}"
          + ("  [stale: reloading for new code]" if status.get("stale") else ""))
    print(f"  checkout     {status.get('checkout_dir') or info.get('checkout')}")
    print(f"  python       {info.get('python', '-')}")
    print(f"  socket       {inst.socket}")
    print(f"  logs/state   {inst.state_dir}")
    workers = status.get("worker_details") or []
    busy = sum(1 for w in workers if w.get("busy"))
    print(f"  workers      {len(workers)} running ({busy} busy), at most {status.get('workers')}; "
          f"idle timeout {status.get('idle_timeout_minutes', '-')} min, idle for {_fmt_duration(status.get('idle_seconds'))}")
    for w in workers:
        print(f"    pid {w.get('pid')}: {'busy' if w.get('busy') else 'idle'}, {w.get('jobs', 0)} jobs, "
              f"{_fmt_bytes(w.get('rss_bytes'))}, up {_fmt_duration(w.get('uptime_seconds'))}")
    print(f"  jobs         {status.get('active_jobs', 0)} running, {status.get('queued_jobs', 0)} queued; "
          f"{status.get('total_requests', 0)} requests, {status.get('success', 0)} ok, {status.get('failure', 0)} failed")
    print(f"  memory       daemon {_fmt_bytes(status.get('memory_rss_bytes'))}, "
          f"workers {_fmt_bytes(sum(w.get('rss_bytes') or 0 for w in workers))}")
    settings = status.get("settings") or {}
    if settings:
        print("  settings     " + ", ".join(f"{k}={v}" for k, v in sorted(settings.items())))
    recent = status.get("recent_compilations") or []
    if recent:
        print("  recent")
        for row in recent[-5:]:
            print(f"    {row.get('id')}  code {row.get('code')}  {row.get('duration_ns', 0) / 1e6:7.1f} ms"
                  f"{'  cached' if row.get('cache_hit') else ''}  {row.get('source')}")
    return 0


def _cmd_start(inst: _Instance, opts) -> int:
    if _daemon_ping(inst.socket):
        print(f"daemon '{inst.name}' is already running")
        return 0
    if not _ensure_daemon(inst, prewarm=True):
        print(f"daemon '{inst.name}' did not start (see {os.path.join(inst.state_dir, 'daemon.log')})", file=sys.stderr)
        return 1
    status = _status_of(inst)
    print(f"daemon '{inst.name}' started, pid {status.get('pid')}")
    return 0


def _stop_instance(inst: _Instance, *, graceful: bool, timeout: float) -> str:
    """Stop one instance; returns what happened."""
    info = _read_json(inst.info)
    pid = info.get("pid") if isinstance(info, dict) else None
    start_ns = info.get("start_time_ns") if isinstance(info, dict) else None
    if not (isinstance(pid, int) and _is_daemon_process(pid, start_ns)):
        pid = _daemon_lease_pid(socket_path=inst.socket, ttl=3600.0)
    if pid is None and not _daemon_ping(inst.socket):
        _remove_instance_files(inst)
        return "not running"
    if graceful:
        try:
            _daemon_send({"cmd": "shutdown"}, 1.0, 5.0, inst.socket)
        except Exception:
            pass
        if pid is None or _wait_gone(pid, timeout):
            _remove_instance_files(inst)
            return "stopped"
        return "still draining"
    if pid is not None:
        _terminate_tree(pid)
    _remove_instance_files(inst)
    return "killed"


def _instances_for_all() -> list[_Instance]:
    result = []
    for row in _registered_instances():
        inst = _Instance()
        inst.name = row["name"]
        inst.scope = (row["info"] or {}).get("scope", "checkout")
        inst.checkout = row["checkout"]
        inst.key = row["key"]
        inst.dir = row["dir"]
        inst.socket = row["socket"]
        inst.lock = inst.socket + ".lock"
        inst.lease = inst.socket + ".lease"
        inst.pid = os.path.join(row["dir"], "daemon.pid")
        inst.info = os.path.join(row["dir"], "instance.json")
        inst.state_dir = (row["info"] or {}).get("state_dir", "")
        inst.explicit = False
        result.append(inst)
    return result


def _cmd_stop(inst: _Instance, opts, *, graceful: bool) -> int:
    import json  # noqa: F401  (json output below)
    targets = _instances_for_all() if opts["all"] else [inst]
    results = []
    for target in targets:
        outcome = _stop_instance(target, graceful=graceful, timeout=_drain_timeout() if graceful else 2.0)
        results.append({"name": target.name, "key": target.key, "result": outcome})
    if opts["all"]:
        for row in _legacy_daemons():
            if graceful:
                try:
                    _daemon_send({"cmd": "shutdown"}, 1.0, 5.0, row["socket"])
                except Exception:
                    pass
                outcome = "stopped" if _wait_gone(row["pid"], 10.0) else "still draining"
            else:
                _terminate_tree(row["pid"])
                outcome = "killed"
            results.append({"name": f"legacy:{row['cwd']}", "key": None, "result": outcome})
    if opts["json"]:
        _print_json(results)
    else:
        if not results:
            print("no daemons")
        for row in results:
            print(f"{row['name']}: {row['result']}")
    return 0 if all(row["result"] != "still draining" for row in results) else 1


def _drain_timeout() -> float:
    try:
        return float(os.environ.get("L2_DAEMON_DRAIN_TIMEOUT", "600"))
    except ValueError:
        return 600.0


def _cmd_restart(inst: _Instance, opts) -> int:
    info = _read_json(inst.info)
    if isinstance(info, dict):
        _FLAG_SETTINGS.setdefault("workers", str(info.get("workers", "")) or None)
        _FLAG_SETTINGS.setdefault("idle_timeout", str(info.get("idle_timeout_minutes", "")) or None)
        if _FLAG_SETTINGS.get("workers") in ("", "None"):
            _FLAG_SETTINGS.pop("workers")
        if _FLAG_SETTINGS.get("idle_timeout") in ("", "None"):
            _FLAG_SETTINGS.pop("idle_timeout")
    outcome = _stop_instance(inst, graceful=True, timeout=_drain_timeout())
    if outcome == "still draining":
        print(f"daemon '{inst.name}' is still finishing jobs; not restarted", file=sys.stderr)
        return 1
    return _cmd_start(inst, opts)


def _simple_command(inst: _Instance, opts, cmd: str, done: str) -> int:
    targets = _instances_for_all() if opts["all"] else [inst]
    code = 0
    results = []
    for target in targets:
        try:
            response = _daemon_send({"cmd": cmd}, 1.0, 10.0, target.socket)
            ok = bool(response.get("ok"))
            text = done if ok else str(response.get("error"))
        except Exception:
            ok, text = False, "not running"
        if not ok and not opts["all"]:
            code = 1
        results.append({"name": target.name, "key": target.key, "ok": ok, "result": text})
    if opts["json"]:
        _print_json(results)
    else:
        for row in results:
            print(f"daemon '{row['name']}': {row['result']}")
    return code


def _cmd_list(opts) -> int:
    rows = _registered_instances()
    for row in rows:
        if row["live"]:
            try:
                status = _daemon_send({"cmd": "status"}, 0.5, 2.0, row["socket"])
            except Exception:
                status = None
            row["responding"] = bool(isinstance(status, dict) and status.get("ok"))
            if row["responding"]:
                row["uptime_seconds"] = status.get("uptime_seconds")
                row["idle_seconds"] = status.get("idle_seconds")
                row["active_jobs"] = status.get("active_jobs")
                row["stale"] = status.get("stale")
                row["memory_rss_bytes"] = status.get("memory_rss_bytes", 0) + sum(
                    w.get("rss_bytes") or 0 for w in status.get("worker_details") or [])
    legacy = _legacy_daemons()
    if opts["json"]:
        _print_json({"instances": rows, "legacy": legacy})
        return 0
    if not rows and not legacy:
        print("no daemons")
        return 0
    here = _current().key if not _current().explicit else None
    for row in rows:
        mark = "*" if row["key"] == here else " "
        if not row["live"]:
            state = "crashed (files left; --daemon-prune removes them)"
        elif not row.get("responding"):
            state = f"pid {row['pid']}, not answering"
        else:
            state = (f"pid {row['pid']}, up {_fmt_duration(row.get('uptime_seconds'))}, "
                     f"idle {_fmt_duration(row.get('idle_seconds'))}, {row.get('active_jobs', 0)} jobs, "
                     f"{_fmt_bytes(row.get('memory_rss_bytes'))}" + (", stale" if row.get("stale") else ""))
        print(f"{mark} {row['name']:<12} {state}")
        print(f"    {row['checkout'] or row['key']}")
    import time
    for row in legacy:
        idle = None if row["last_activity"] is None else time.time() - row["last_activity"]
        print(f"  legacy       pid {row['pid']}, idle {_fmt_duration(idle)}, directory {row['cwd']}")
        print(f"    {row['checkout']}")
    return 0


def _cmd_logs(inst: _Instance, opts, arg) -> int:
    import time
    if arg and not arg.isdigit():
        path = os.path.join(inst.state_dir, "req", f"{os.path.basename(arg)}.log")
        lines = None
    else:
        path = os.path.join(inst.state_dir, "daemon.log")
        lines = int(arg) if arg else 50
    try:
        text = _read_text(path)
    except OSError:
        print(f"no log at {path}", file=sys.stderr)
        return 1
    if lines is not None:
        text = "\n".join(text.splitlines()[-lines:]) + ("\n" if text else "")
    sys.stdout.write(text)
    sys.stdout.flush()
    if not opts["follow"]:
        return 0
    position = os.path.getsize(path)
    try:
        while True:
            time.sleep(0.25)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < position:  # rotated
                position = 0
            if size > position:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    handle.seek(position)
                    sys.stdout.write(handle.read())
                    sys.stdout.flush()
                position = size
    except KeyboardInterrupt:
        return 0


def _cmd_events(inst: _Instance, opts, pattern) -> int:
    import json
    import socket

    pattern = pattern or "*"
    if not opts["follow"]:
        try:
            response = _daemon_send({"cmd": "events", "pattern": pattern, "limit": 200}, 1.0, 5.0, inst.socket)
        except Exception:
            print(f"daemon '{inst.name}' is not running", file=sys.stderr)
            return 1
        events = response.get("events") or []
        if opts["json"]:
            _print_json(events)
        else:
            for row in events:
                print(json.dumps(row, separators=(",", ":"), default=str))
        return 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            _connect_unix(conn, inst.socket)
            conn.sendall((json.dumps({"cmd": "subscribe_events", "pattern": pattern}) + "\n").encode("utf-8"))
            for raw in conn.makefile("rb"):
                row = json.loads(raw)
                event = row.get("event", row)
                if isinstance(event, dict) and event.get("event") == "subscription.ready":
                    continue
                print(json.dumps(event, separators=(",", ":"), default=str), flush=True)
    except KeyboardInterrupt:
        return 0
    except OSError:
        print(f"daemon '{inst.name}' is not running", file=sys.stderr)
        return 1
    return 0


def _cmd_prune(opts) -> int:
    import shutil
    import time

    try:
        days = float(opts["days"]) if opts["days"] is not None else 7.0
    except ValueError:
        raise SystemExit("--days needs a number")
    removed = []
    for row in _registered_instances(include_stopped=True):
        if row["live"] or _daemon_ping(row["socket"]):
            continue
        shutil.rmtree(row["dir"], ignore_errors=True)
        removed.append(f"instance files {row['dir']}")
    cutoff = time.time() - days * 86400
    state_root = _state_root()
    for base, dirs, files in os.walk(state_root):
        if os.path.basename(base) != "req":
            continue
        for name in files:
            path = os.path.join(base, name)
            try:
                if os.stat(path).st_mtime < cutoff:
                    os.unlink(path)
                    removed.append(path)
            except OSError:
                pass
    # State of checkouts that no longer exist.
    try:
        tags = os.listdir(state_root)
    except OSError:
        tags = []
    for tag in tags:
        tag_dir = os.path.join(state_root, tag)
        checkout = None
        for name in os.listdir(tag_dir) if os.path.isdir(tag_dir) else []:
            info = _read_json(os.path.join(tag_dir, name, "checkout"))
            if isinstance(info, str):
                checkout = info
        if checkout is not None and not os.path.isdir(checkout) and not os.path.isdir(os.path.join(_runtime_root(), tag)):
            shutil.rmtree(tag_dir, ignore_errors=True)
            removed.append(f"state of removed checkout {checkout}")
    if opts["json"]:
        _print_json(removed)
    else:
        print(f"removed {len(removed)} item(s)")
        for row in removed[:20]:
            print(f"  {row}")
        if len(removed) > 20:
            print(f"  ... and {len(removed) - 20} more")
    return 0


def _cmd_config(inst: _Instance, opts) -> int:
    def source(env_name, flag_key=None):
        if flag_key and _FLAG_SETTINGS.get(flag_key):
            return "flag"
        return "environment" if os.environ.get(env_name) else "default"

    try:
        settings = _daemon_settings()
    except _DaemonUnavailable as exc:
        raise SystemExit(str(exc))
    rows = [
        ("enabled", _daemon_default_enabled(), source("L2_DAEMON")),
        ("name", inst.name, "flag" if _FLAG_NAME else source("L2_DAEMON_NAME")),
        ("scope", inst.scope, source("L2_DAEMON_SCOPE")),
        ("socket", inst.socket, source("L2_DAEMON_SOCKET") if inst.explicit else "derived"),
        ("runtime_dir", inst.dir, source("L2_DAEMON_RUNTIME_DIR") if os.environ.get("L2_DAEMON_RUNTIME_DIR") else "derived"),
        ("state_dir", inst.state_dir, source("L2_DAEMON_STATE_DIR") if os.environ.get("L2_DAEMON_STATE_DIR") else "derived"),
        ("checkout", inst.checkout, "derived"),
        ("python", f"{os.path.realpath(sys.executable)} ({sys.version.split()[0]})", "derived"),
        ("workers", settings["workers"], source("L2_DAEMON_WORKERS", "workers")),
        ("idle_timeout_minutes", settings["idle_timeout"], source("L2_DAEMON_IDLE_TIMEOUT", "idle_timeout")),
        ("trusted_dirs", os.environ.get("L2_DAEMON_TRUSTED_DIRS", ""), source("L2_DAEMON_TRUSTED_DIRS")),
    ]
    for name in _DAEMON_START_ENV:
        rows.append((name, os.environ.get(name, ""), source(name)))
    running = _status_of(inst)
    differs = []
    if running.get("running") and isinstance(running.get("settings"), dict):
        actual = running["settings"]
        for key, value, _source in rows:
            if key in actual and str(actual[key]) != str(value):
                differs.append((key, actual[key]))
    if opts["json"]:
        _print_json({"settings": {k: {"value": v, "source": s} for k, v, s in rows},
                     "running": bool(running.get("running")),
                     "running_differs": {k: v for k, v in differs}})
        return 0
    for key, value, src in rows:
        print(f"{key:<24} {value!s:<50} ({src})")
    if differs:
        print("\nthe running daemon was started with other values (--daemon-restart applies these):")
        for key, value in differs:
            print(f"  {key} = {value}")
    return 0


_FLAG_NAME = False


def _run_management(command: str, arg, opts) -> int:
    global _FLAG_NAME
    _FLAG_NAME = bool(opts["name"])
    try:
        inst = _instance(opts["name"])
        global _CURRENT
        _CURRENT = inst
        if command == "--daemon-status":
            return _cmd_status(inst, opts)
        if command == "--daemon-start":
            return _cmd_start(inst, opts)
        if command == "--daemon-stop":
            return _cmd_stop(inst, opts, graceful=True)
        if command == "--daemon-kill":
            return _cmd_stop(inst, opts, graceful=False)
        if command == "--daemon-restart":
            return _cmd_restart(inst, opts)
        if command == "--daemon-reload":
            return _simple_command(inst, opts, "reload", "reloading (running jobs finish first)")
        if command == "--daemon-clear":
            return _simple_command(inst, opts, "clear", "caches cleared, workers will start fresh")
        if command == "--daemon-list":
            return _cmd_list(opts)
        if command == "--daemon-logs":
            return _cmd_logs(inst, opts, arg)
        if command == "--daemon-events":
            return _cmd_events(inst, opts, arg)
        if command == "--daemon-prune":
            return _cmd_prune(opts)
        if command == "--daemon-config":
            return _cmd_config(inst, opts)
    except _DaemonUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    raise SystemExit(f"unknown daemon command {command}")



def run_main() -> None:
    """main.py's entry: daemon management flags, a compile through the daemon, or
    a compile in this process.  Always ends with os._exit after flushing
    output: interpreter teardown is pure cost here, and Python 3.14's teardown
    occasionally crashed (SIGSEGV in Py_Exit) after a daemon compile had
    finished and printed its result."""
    try:
        _run_main()
        code = 0
    except SystemExit as exc:
        code = exc.code
    except KeyboardInterrupt:
        code = 130
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    if code is None:
        code = 0
    elif not isinstance(code, int):  # SystemExit("message"): print it, exit 1
        try:
            sys.stderr.write(f"{code}\n")
        except Exception:
            pass
        code = 1
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code & 0xFF if code >= 0 else 1)


def _run_main() -> None:
    global _CURRENT
    argv, force_local = _split_control_args(sys.argv[1:])
    argv, _command, _command_arg, _opts = _parse_daemon_args(argv)

    if _command is not None:
        if argv:
            raise SystemExit(f"{_command} does not take a source file or compiler options: {' '.join(argv)}")
        raise SystemExit(_run_management(_command, _command_arg, _opts))

    if _opts["name"]:
        try:
            _CURRENT = _instance(_opts["name"])
        except _DaemonUnavailable as exc:
            raise SystemExit(f"error: {exc}")

    if _opts["require"]:
        if force_local:
            raise SystemExit("error: --daemon and --no-daemon cannot be combined")
        if _requires_local_execution(argv):
            raise SystemExit("error: this command runs without the daemon (--docs, --repl, --run, --dbg, --help, "
                             "or an interactive --script/--ct-run-main); drop --daemon")

    if not force_local and (_opts["require"] or _daemon_default_enabled()) and not _requires_local_execution(argv):
        stdin_data = _read_stdin_for_daemon(argv)
        try:
            result = _run_via_daemon(argv, stdin_data, required=_opts["require"])
        except _DaemonUnavailable as exc:
            sys.stderr.write(f"error: the compile daemon cannot be used: {exc}\n")
            raise SystemExit(2)
        if result is not None:
            raise SystemExit(result)
        _restore_stdin_for_local(stdin_data)

    from l2_main import cli as _entry_cli, freeze_startup_objects

    freeze_startup_objects()
    _code = _entry_cli(argv)
    # Like l2_main.main(): flush, then skip interpreter teardown (freeing the
    # whole compiler state at exit is pure cost, and ctypes/native memory
    # finalization can crash during shutdown).
    if _code is None:
        _code = 0
    elif not isinstance(_code, int):  # SystemExit semantics: print it, exit 1
        print(_code, file=sys.stderr)
        _code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
