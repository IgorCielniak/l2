#!/usr/bin/env python3
"""Machine code cache for JIT-assembled code: the pack file shared between
compiler processes (round trip, corrupt and incomplete records, ownership
checks, compaction), cache keys that follow changed :asm bodies, and daemon
workers that hand forked jobs keystone and ready code."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

_CACHE_DIR = tempfile.mkdtemp(prefix="l2-asm-cache-")
os.environ["L2_ASM_CACHE_DIR"] = _CACHE_DIR

import l2_main  # noqa: E402


def _fresh_store() -> "l2_main._CachePack":
    store = l2_main._CachePack()
    store.refresh()
    return store


def _digest(text: str) -> bytes:
    import hashlib
    return hashlib.blake2b(text.encode(), digest_size=16).digest()


def run_forked(argv, *, block_keystone: bool):
    """Run the CLI in a forked child, as a daemon job does.  With
    block_keystone, any attempt to assemble in the child fails the run.
    Returns (exit code, output)."""
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.dup2(write_fd, 1)
        os.dup2(write_fd, 2)
        if block_keystone:
            class _Blocked:
                def __init__(self, *_args):
                    raise RuntimeError("keystone used in the job")
            l2_main.Ks = _Blocked
            l2_main._KS_INSTANCE = None
        try:
            code = l2_main.cli(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except BaseException as exc:  # pragma: no cover - reported as output
            print(f"EXCEPTION {exc!r}")
            code = 99
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), b"".join(chunks).decode(errors="replace")


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="l2-asm-store-")
        os.environ["L2_ASM_CACHE_DIR"] = self.dir.name

    def tearDown(self) -> None:
        os.environ["L2_ASM_CACHE_DIR"] = _CACHE_DIR
        self.dir.cleanup()

    def test_round_trip_between_processes(self) -> None:
        writer = _fresh_store()
        writer.add(b"a" * 16, b"\x90\xc3")
        writer.add(b"b" * 16, b"\xc3")
        reader = _fresh_store()
        self.assertEqual(reader.get(b"a" * 16), b"\x90\xc3")
        self.assertEqual(reader.get(b"b" * 16), b"\xc3")
        # Later appends are picked up by refresh().
        writer.add(b"c" * 16, b"\xcc")
        reader.refresh()
        self.assertEqual(reader.get(b"c" * 16), b"\xcc")
        self.assertEqual(oct(os.stat(writer.path).st_mode & 0o777), "0o600")

    def test_corrupt_and_incomplete_records(self) -> None:
        writer = _fresh_store()
        writer.add(b"a" * 16, b"\x90\xc3")
        with open(writer.path, "ab") as handle:
            # A record cut short by a crash, then a valid record after it.
            handle.write(b"L2AC" + b"x" * 16 + (100).to_bytes(4, "little") + b"12345678" + b"\x90")
        writer2 = l2_main._CachePack()
        writer2.add(b"b" * 16, b"\xc3")
        reader = _fresh_store()
        self.assertEqual(reader.get(b"a" * 16), b"\x90\xc3")
        self.assertEqual(reader.get(b"b" * 16), b"\xc3")
        self.assertIsNone(reader.get(b"x" * 16))
        # An incomplete last record is read once it is complete.
        record = l2_main._get_struct().pack("<4s16sI8s", b"L2AC", b"d" * 16, 1, b"")
        import hashlib
        check = hashlib.blake2b(b"d" * 16 + b"\xcc", digest_size=8).digest()
        record = record[:24] + check + b"\xcc"
        with open(writer.path, "ab") as handle:
            handle.write(record[:-1])
        reader.refresh()
        self.assertIsNone(reader.get(b"d" * 16))
        with open(writer.path, "ab") as handle:
            handle.write(record[-1:])
        reader.refresh()
        self.assertEqual(reader.get(b"d" * 16), b"\xcc")

    def test_store_shared_with_others_is_ignored(self) -> None:
        writer = _fresh_store()
        writer.add(b"a" * 16, b"\xc3")
        os.chmod(writer.path, 0o666)
        reader = _fresh_store()
        self.assertIsNone(reader.get(b"a" * 16))
        reader.add(b"b" * 16, b"\xc3")
        os.chmod(writer.path, 0o600)
        self.assertIsNone(_fresh_store().get(b"b" * 16))

    def test_disabled_by_environment(self) -> None:
        os.environ["L2_ASM_CACHE"] = "0"
        try:
            store = _fresh_store()
            store.add(b"a" * 16, b"\xc3")
            self.assertEqual(os.listdir(self.dir.name), [])
        finally:
            del os.environ["L2_ASM_CACHE"]

    def test_new_pack_replaces_old_ones(self) -> None:
        directory = Path(self.dir.name)
        for index in range(5):
            old = directory / f"peephole-v1-old{index}.pack"
            old.write_bytes(b"")
            os.utime(old, (1000 + index, 1000 + index))
        (directory / "x86-code-v1-other.pack").write_bytes(b"")
        store = l2_main._CachePack("peephole", lambda: "a new compiler", 1024)
        store.add(b"a" * 16, b"\x90")
        names = sorted(p.name for p in directory.iterdir())
        self.assertIn(store.path.name, names)
        self.assertEqual([n for n in names if n.startswith("peephole-v1-old")],
                         ["peephole-v1-old3.pack", "peephole-v1-old4.pack"])
        self.assertIn("x86-code-v1-other.pack", names)  # other kinds are left alone

    def test_compaction_keeps_newest(self) -> None:
        store = _fresh_store()
        store.max_bytes = 2000
        for index in range(60):
            store.add(index.to_bytes(16, "little"), b"\x90" * 20)
        self.assertLessEqual(os.stat(store.path).st_size, 2000)
        reader = _fresh_store()
        self.assertEqual(reader.get((59).to_bytes(16, "little")), b"\x90" * 20)
        self.assertIsNone(reader.get((0).to_bytes(16, "little")))


class AssembleTests(unittest.TestCase):
    def test_addresses_stay_out_of_the_pack(self) -> None:
        if not l2_main._ensure_keystone():
            self.skipTest("keystone not installed")
        shared = "mov rax, 5\nret"
        private = "mov rax, 140737488347136\nret"
        self.assertTrue(l2_main._x86_assemble(shared))
        self.assertTrue(l2_main._x86_assemble(private))
        reader = _fresh_store()
        self.assertIsNotNone(reader.get(_digest(shared)))
        self.assertIsNone(reader.get(_digest(private)))

    def test_cached_code_needs_no_keystone(self) -> None:
        if not l2_main._ensure_keystone():
            self.skipTest("keystone not installed")
        text = "mov rax, 7\nadd rax, 1\nret"
        code = l2_main._x86_assemble(text)
        l2_main._ASM_CODE_MEMO.clear()
        saved = (l2_main.Ks, l2_main._KS_INSTANCE)
        l2_main.Ks, l2_main._KS_INSTANCE = None, None
        try:
            self.assertEqual(l2_main._x86_assemble(text), code)
        finally:
            l2_main.Ks, l2_main._KS_INSTANCE = saved

    def test_changed_body_runs_new_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-asm-body-") as raw:
            source = Path(raw) / "body.sl"
            template = (
                "import stdlib.sl\n"
                ":asm mix {{\n    mov rax, [r12]\n    add r12, 8\n    imul rax, {k}\n    add [r12], rax\n}} ;\n"
                "word check 2 3 mix {expected} == static-assert end\n"
                "compile-time check\n"
                "word main 0 end\n"
            )
            for k, expected in ((3, 11), (5, 17), (3, 11)):
                source.write_text(template.format(k=k, expected=expected))
                code, output = run_forked([str(source), "--no-artifact"], block_keystone=False)
                self.assertEqual(code, 0, output)


class DaemonWorkerTests(unittest.TestCase):
    def test_jobs_inherit_keystone_and_code(self) -> None:
        if not l2_main._ensure_keystone():
            self.skipTest("keystone not installed")
        l2_main._warm_stdlib_prepare()
        warm = l2_main._WARM_STDLIB
        if warm is None:
            self.skipTest("no warm stdlib prefix")
        l2_main._daemon_asm_prepare()
        self.assertTrue(warm.asm_mapped)
        with tempfile.TemporaryDirectory(prefix="l2-asm-daemon-") as raw:
            # Stdlib words at compile time: mapped in the worker.
            stdlib_job = Path(raw) / "stdlib_job.sl"
            stdlib_job.write_text(
                "import stdlib.sl\n"
                "word check 1 2 3 4 2swap 2 == static-assert 1 == static-assert"
                " 4 == static-assert 3 == static-assert 7 8 1 pick 7 == static-assert 2drop end\n"
                "compile-time check\n"
                "word main 0 end\n"
            )
            code, output = run_forked([str(stdlib_job), "--no-artifact", "--no-cache"], block_keystone=True)
            self.assertEqual(code, 0, output)

            # A word of the job's own: the first job assembles it, the
            # worker picks the code up, the next job only maps it.
            own_job = Path(raw) / "own_job.sl"
            own_job.write_text(
                "import stdlib.sl\n"
                ":asm triple-sum {\n    mov rax, [r12]\n    add r12, 8\n    lea rax, [rax + rax*2]\n"
                "    add [r12], rax\n} ;\n"
                "word check 1 2 triple-sum 7 == static-assert end\n"
                "compile-time check\n"
                "word main 0 end\n"
            )
            code, output = run_forked([str(own_job), "--no-artifact", "--no-cache"], block_keystone=True)
            self.assertNotEqual(code, 0, "the first job should have needed keystone")
            code, output = run_forked([str(own_job), "--no-artifact", "--no-cache"], block_keystone=False)
            self.assertEqual(code, 0, output)
            l2_main._daemon_asm_prepare()
            code, output = run_forked([str(own_job), "--no-artifact", "--no-cache"], block_keystone=True)
            self.assertEqual(code, 0, output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
