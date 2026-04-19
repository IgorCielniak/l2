#!/usr/bin/env python3
"""Compatibility wrapper for the L2 compiler implementation.

This module re-exports symbols from l2_main so imports like `import main`
continue to work, while script execution uses a small entrypoint that avoids
re-parsing the full compiler source on every invocation.
"""

import os
import sys


_FORCE_WORKER_TOKEN = "--__l2-force-worker"
_FORCE_WORKER_SOCKET = os.path.join("build", ".l2_force_worker.sock")
_FORCE_WORKER_PID = os.path.join("build", ".l2_force_worker.pid")
_FORCE_WORKER_DEBUG = os.environ.get("L2_FORCE_WORKER_DEBUG", "0") not in ("0", "", "false", "False")


def _force_worker_log(message: str) -> None:
    if _FORCE_WORKER_DEBUG:
        sys.stderr.write(f"[force-worker] {message}\n")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_force_worker_pid() -> int:
    try:
        with open(_FORCE_WORKER_PID, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return 0
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _write_force_worker_pid(pid: int) -> None:
    try:
        with open(_FORCE_WORKER_PID, "w", encoding="utf-8") as fh:
            fh.write(str(int(pid)))
            fh.write("\n")
    except OSError:
        pass


def _remove_force_worker_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _decode_worker_message(payload: str) -> str:
    return payload.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")


def _encode_worker_message(message: str) -> str:
    return message.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def _parse_strict_force_source(argv):
    source_token = None
    saw_force = False
    for tok in argv:
        if tok == "--force":
            saw_force = True
            continue
        if tok.startswith("-"):
            return None
        if source_token is None:
            source_token = tok
            continue
        return None
    if not saw_force or source_token is None:
        return None
    if os.path.splitext(source_token)[1].lower() != ".sl":
        return None
    return source_token


def _parse_strict_no_cache_source(argv):
    source_token = None
    saw_no_cache = False
    for tok in argv:
        if tok == "--force":
            return None
        if tok == "--no-cache":
            saw_no_cache = True
            continue
        if tok.startswith("-"):
            return None
        if source_token is None:
            source_token = tok
            continue
        return None
    if not saw_no_cache or source_token is None:
        return None
    if os.path.splitext(source_token)[1].lower() != ".sl":
        return None
    return source_token


def _request_force_worker_line(line):
    import socket

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(_FORCE_WORKER_SOCKET)
        sock.sendall((line + "\n").encode("utf-8"))
        chunks = b""
        while not chunks.endswith(b"\n"):
            block = sock.recv(256)
            if not block:
                break
            chunks += block

    if not chunks:
        raise RuntimeError("empty force-worker response")

    return chunks.decode("utf-8", errors="replace").split("\n", 1)[0]


def _ping_force_worker(*, require_v2=False):
    if require_v2:
        return _request_force_worker_line("PING2") == "PONG2"
    return _request_force_worker_line("PING") == "PONG"


def _run_force_worker_request(source_token):
    color_flag = "1" if sys.stderr.isatty() else "0"
    response = _request_force_worker_line(f"RUN\t{color_flag}\t{source_token}")
    if not (response.startswith("RC\t") or response.startswith("RC2\t")):
        raise RuntimeError("invalid force-worker response")
    parts = response.split("\t", 2)
    code = int(parts[1])
    detail = _decode_worker_message(parts[2]) if len(parts) >= 3 else ""
    return code, detail


def _run_no_cache_worker_request(source_token):
    color_flag = "1" if sys.stderr.isatty() else "0"
    response = _request_force_worker_line(f"RUN_NC\t{color_flag}\t{source_token}")
    if not (response.startswith("RC\t") or response.startswith("RC2\t")):
        raise RuntimeError("invalid no-cache worker response")
    parts = response.split("\t", 2)
    code = int(parts[1])
    detail = _decode_worker_message(parts[2]) if len(parts) >= 3 else ""
    return code, detail


def _start_force_worker():
    import subprocess
    import signal
    import time

    try:
        if _ping_force_worker(require_v2=True):
            return True
    except (OSError, RuntimeError, ValueError) as exc:
        _force_worker_log(f"pre-spawn ping failed: {exc}")

    os.makedirs("build", exist_ok=True)
    pid = _read_force_worker_pid()
    if pid > 0 and _pid_is_alive(pid):
        _force_worker_log(f"terminating stale worker pid={pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

        grace_deadline = time.monotonic() + 0.25
        while time.monotonic() < grace_deadline and _pid_is_alive(pid):
            time.sleep(0.01)
        if _pid_is_alive(pid):
            _force_worker_log(f"worker pid={pid} did not exit after SIGTERM")

    _remove_force_worker_file(_FORCE_WORKER_SOCKET)
    _remove_force_worker_file(_FORCE_WORKER_PID)

    proc = subprocess.Popen(
        [sys.executable, __file__, _FORCE_WORKER_TOKEN],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    _write_force_worker_pid(proc.pid)

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if os.path.exists(_FORCE_WORKER_SOCKET):
            try:
                if _ping_force_worker(require_v2=True):
                    return True
            except (OSError, RuntimeError, ValueError):
                continue
        time.sleep(0.01)

    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
    return False


def _try_ultra_fast_force(argv):
    if os.environ.get("L2_FORCE_WORKER", "1") in ("0", "false", "False"):
        return None

    source_token = _parse_strict_force_source(argv)
    if source_token is None:
        return None

    for _attempt in range(2):
        try:
            rc, detail = _run_force_worker_request(source_token)
            if rc == 0:
                print("[info] built a.out")
                return 0
            if detail:
                if not detail.endswith("\n"):
                    detail += "\n"
                sys.stderr.write(detail)
                return 1
            return None
        except (OSError, RuntimeError, ValueError) as exc:
            _force_worker_log(f"worker request failed: {exc}")
            if not _start_force_worker():
                continue
    return None


def _try_ultra_fast_no_cache(argv):
    # Preserve interactive output behavior in TTY sessions.
    if sys.stdout.isatty():
        return None
    if os.environ.get("L2_NO_CACHE_WORKER", "1") in ("0", "false", "False"):
        return None

    source_token = _parse_strict_no_cache_source(argv)
    if source_token is None:
        return None

    for _attempt in range(2):
        try:
            rc, detail = _run_no_cache_worker_request(source_token)
            if rc == 0:
                return 0
            if detail:
                if not detail.endswith("\n"):
                    detail += "\n"
                sys.stderr.write(detail)
                return 1
            return None
        except (OSError, RuntimeError, ValueError) as exc:
            _force_worker_log(f"no-cache worker request failed: {exc}")
            if not _start_force_worker():
                continue
    return None


def _run_force_worker():
    import io
    import socket
    from contextlib import redirect_stderr, redirect_stdout
    from l2_main import cli as _worker_cli
    from l2_main import _try_quick_compile_force as _worker_quick_force

    def _parse_worker_run_payload(payload: str):
        # Backward-compatible protocol parsing:
        # legacy payload: "<source>"
        # current payload: "<0|1>\t<source>"
        wants_color = False
        source_token = payload
        if "\t" in payload:
            maybe_color, rest = payload.split("\t", 1)
            if maybe_color in ("0", "1"):
                wants_color = maybe_color == "1"
                source_token = rest
        return source_token, wants_color

    os.makedirs("build", exist_ok=True)
    _remove_force_worker_file(_FORCE_WORKER_SOCKET)
    _write_force_worker_pid(os.getpid())

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(_FORCE_WORKER_SOCKET)
        server.listen(8)
        server.settimeout(300.0)
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                break
            except OSError:
                break

            with conn:
                chunks = []
                while True:
                    block = conn.recv(65536)
                    if not block:
                        break
                    chunks.append(block)
                    if b"\n" in block:
                        break

                try:
                    line = b"".join(chunks).decode("utf-8", errors="replace").split("\n", 1)[0]
                except Exception:
                    line = ""

                if line == "PING":
                    conn.sendall(b"PONG\n")
                    continue
                if line == "PING2":
                    conn.sendall(b"PONG2\n")
                    continue

                rc = 1
                detail = ""
                try:
                    mode = "force"
                    wants_color = False
                    if line.startswith("RUN\t"):
                        source_token, wants_color = _parse_worker_run_payload(line.split("\t", 1)[1])
                    elif line.startswith("RUN_NC\t"):
                        mode = "no-cache"
                        source_token, wants_color = _parse_worker_run_payload(line.split("\t", 1)[1])
                    else:
                        raise RuntimeError("invalid force-worker request")
                    out_buf = io.StringIO()
                    err_buf = io.StringIO()
                    prev_force_color = os.environ.get("L2_FORCE_COLOR")
                    os.environ["L2_FORCE_COLOR"] = "1" if wants_color else "0"
                    try:
                        with redirect_stdout(out_buf), redirect_stderr(err_buf):
                            if mode == "force":
                                result = _worker_quick_force([source_token, "--force"], emit_status=False)
                                if result is None:
                                    result = _worker_cli([source_token, "--force"])
                            else:
                                result = _worker_cli([source_token, "--no-cache"])
                    finally:
                        if prev_force_color is None:
                            os.environ.pop("L2_FORCE_COLOR", None)
                        else:
                            os.environ["L2_FORCE_COLOR"] = prev_force_color
                    rc = int(result) if result is not None else 0
                    if rc != 0:
                        detail = err_buf.getvalue().strip() or out_buf.getvalue().strip()
                except SystemExit as exc:
                    code = exc.code
                    if isinstance(code, int):
                        rc = code
                    elif code is None:
                        rc = 0
                    else:
                        rc = 1
                except Exception as exc:
                    rc = 1
                    detail = f"force worker exception: {exc}"
                payload = f"RC2\t{int(rc)}"
                if detail:
                    payload += f"\t{_encode_worker_message(detail)}"
                conn.sendall((payload + "\n").encode("utf-8"))

    _remove_force_worker_file(_FORCE_WORKER_SOCKET)
    _remove_force_worker_file(_FORCE_WORKER_PID)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == _FORCE_WORKER_TOKEN:
        _run_force_worker()
        raise SystemExit(0)

    quick_no_cache = _try_ultra_fast_no_cache(sys.argv[1:])
    if quick_no_cache is not None:
        raise SystemExit(quick_no_cache)

    quick_force = _try_ultra_fast_force(sys.argv[1:])
    if quick_force is not None:
        raise SystemExit(quick_force)

    from l2_main import main as _entry_main

    _entry_main()
else:
    from l2_main import *  # noqa: F401,F403
    from l2_main import main as _entry_main
