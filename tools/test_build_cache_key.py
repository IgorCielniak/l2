#!/usr/bin/env python3
"""The build caches notice every change a compile depends on.

* The whole-assembly cache (<temp>/.l2cache/asm) is keyed by every option the
  assembly depends on: a compile with a different -D, -I or code generation
  switch is a miss, an identical compile is a hit, and a hit returns what a
  forced compile produces.
* It and the source graph cache notice an edit that keeps the file's size
  and modification time, a new file that would shadow an import, and an
  imported symlink that now points elsewhere.
* Daemon jobs take the client's per-compile environment variables, and a
  daemon prefix notices the same kind of edit."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main  # noqa: E402

SOURCE = """import stdlib.sl
ifdef FOO
word msg "with FOO" puts cr end
elsedef
word msg "without FOO" puts cr end
endif
word main msg 0 end
"""


class BuildCacheKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="l2-cache-key-")
        self.root = Path(self._dir.name)
        self.source = self.root / "prog.sl"
        self.source.write_text(SOURCE)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def emit(self, *flags: str, force: bool = False) -> "tuple[str, str]":
        out = self.root / "out.asm"
        argv = [sys.executable, str(ROOT / "main.py"), str(self.source), "--no-daemon", "--emit-asm",
                "-o", str(out), "--temp-dir", str(self.root / "build"), "--verbose", "1", *flags]
        if force:
            argv.append("--force")
        proc = subprocess.run(argv, capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out.read_text(), proc.stdout

    def test_defines_are_part_of_the_key(self) -> None:
        with_foo, _ = self.emit("-D", "FOO")
        without_foo, log = self.emit()
        self.assertNotIn("cache hit for", log)
        self.assertNotEqual(with_foo, without_foo)
        self.assertEqual(without_foo, self.emit(force=True)[0])
        again, log = self.emit("-D", "FOO")
        self.assertEqual(again, with_foo)

    def test_identical_compile_hits(self) -> None:
        first, _ = self.emit("-D", "FOO")
        second, log = self.emit("-D", "FOO")
        self.assertIn("cache hit for", log)
        self.assertEqual(first, second)

    def test_other_options_are_part_of_the_key(self) -> None:
        self.emit()
        for flags in (["-I", str(self.root)], ["--no-stack-check"], ["--no-loop-unroll"],
                      ["--no-extern-type-check"], ["-W", "all"], ["--macro-expansion-limit", "500"]):
            _, log = self.emit(*flags)
            self.assertNotIn("cache hit for", log, flags)
        # One entry per source file: the last compile replaced it.
        self.emit()
        _, log = self.emit()
        self.assertIn("cache hit for", log)


class DependencyChangeTests(unittest.TestCase):
    """Built programs reflect every change to what they import."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="l2-deps-")
        self.root = Path(self._dir.name)
        self.build_dir = self.root / "build"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def run_program(self, source: Path, *flags: str) -> str:
        exe = self.root / "prog"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), str(source), "--no-daemon", "-o", str(exe),
             "--temp-dir", str(self.build_dir), *flags],
            capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return subprocess.run([str(exe)], capture_output=True, text=True, timeout=30).stdout.strip()

    def write_keep_mtime(self, path: Path, text: str) -> None:
        st = path.stat()
        path.write_text(text)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    def check_same_size_edit(self, ct_code: bool) -> None:
        lib = self.root / "lib.sl"
        lib.write_text("word val 10 end\n")
        prog = self.root / "prog.sl"
        ct = 'word note "ct" puts cr end\ncompile-time note\n' if ct_code else ""
        prog.write_text(f"import stdlib.sl\nimport lib.sl\n{ct}word main val puti cr 0 end\n")
        self.assertEqual(self.run_program(prog), "10")
        self.write_keep_mtime(lib, "word val 20 end\n")
        self.assertEqual(self.run_program(prog), "20")

    def test_same_size_edit_with_restored_mtime(self) -> None:
        self.check_same_size_edit(ct_code=False)

    def test_same_size_edit_source_graph_cache(self) -> None:
        # Compile-time code keeps the whole-assembly cache out of the way.
        self.check_same_size_edit(ct_code=True)

    def test_new_file_shadows_an_import(self) -> None:
        include = self.root / "inc"
        include.mkdir()
        (include / "shadowed.sl").write_text("word val 1 end\n")
        prog = self.root / "prog.sl"
        prog.write_text("import stdlib.sl\nimport shadowed.sl\nword main val puti cr 0 end\n")
        self.assertEqual(self.run_program(prog, "-I", str(include)), "1")
        (self.root / "shadowed.sl").write_text("word val 2 end\n")
        self.assertEqual(self.run_program(prog, "-I", str(include)), "2")
        (self.root / "shadowed.sl").unlink()
        self.assertEqual(self.run_program(prog, "-I", str(include)), "1")

    def test_retargeted_symlink(self) -> None:
        (self.root / "a.sl").write_text("word val 21 end\n")
        (self.root / "b.sl").write_text("word val 22 end\n")
        link = self.root / "link.sl"
        link.symlink_to(self.root / "a.sl")
        prog = self.root / "prog.sl"
        prog.write_text("import stdlib.sl\nimport link.sl\nword main val puti cr 0 end\n")
        self.assertEqual(self.run_program(prog), "21")
        link.unlink()
        link.symlink_to(self.root / "b.sl")
        self.assertEqual(self.run_program(prog), "22")


class DaemonEnvironmentTests(unittest.TestCase):
    def test_job_env_is_filtered(self) -> None:
        self.assertEqual(
            l2_main._sanitize_job_env({"L2_NASM_PARALLEL": "0", "L2_ASM_CACHE": None, "PATH": "/x",
                                       "L2_ASM_CACHE_DIR": "bad\x00"}),
            {"L2_NASM_PARALLEL": "0", "L2_ASM_CACHE": None})
        self.assertEqual(l2_main._sanitize_job_env(["L2_ASM_CACHE"]), {})

    def test_job_env_applies_and_resets_caches(self) -> None:
        saved = {name: os.environ.get(name) for name in l2_main._DAEMON_JOB_ENV}
        saved_stores = (l2_main._ASM_CODE_STORE, l2_main._PEEPHOLE_STORE)
        try:
            os.environ["L2_NASM_PARALLEL"] = "force"
            os.environ.pop("L2_ASM_CACHE", None)
            l2_main._ASM_CODE_MEMO["probe"] = b"\xc3"
            l2_main._apply_job_env({"L2_NASM_PARALLEL": None, "L2_ASM_CACHE": "0"})
            self.assertNotIn("L2_NASM_PARALLEL", os.environ)
            self.assertEqual(os.environ.get("L2_ASM_CACHE"), "0")
            self.assertNotIn("probe", l2_main._ASM_CODE_MEMO)
            self.assertIsNot(l2_main._ASM_CODE_STORE, saved_stores[0])
            self.assertIsNone(l2_main._ASM_CODE_STORE.get(b"x" * 16))
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            l2_main._ASM_CODE_STORE, l2_main._PEEPHOLE_STORE = saved_stores

    def test_warm_prefix_notices_same_size_edit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-warm-fresh-") as raw:
            path = Path(raw) / "lib.sl"
            path.write_text("word val 10 end\n")
            prefix = l2_main._WarmPrefix(None, (), {path: l2_main._file_identity(path.stat())}, [], set())
            self.assertTrue(prefix.fresh())
            st = path.stat()
            path.write_text("word val 20 end\n")
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
            self.assertFalse(prefix.fresh())


if __name__ == "__main__":
    unittest.main(verbosity=2)
