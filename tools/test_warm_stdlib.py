#!/usr/bin/env python3
"""Daemon warm stdlib: a job forked from a worker's pre-parsed stdlib
must produce the same result as a cold compile, be used only when its parse
settings and first import match, and be rebuilt when a stdlib file changes
or on --force-stdlib (not on --force)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import l2_main


def run_forked(argv, *, warm: bool):
    """Run the CLI in a forked child (as a daemon job does); return
    (exit code, output, whether the warm prefix was resumed)."""
    sys.stdout.flush()
    sys.stderr.flush()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.dup2(write_fd, 1)
        os.dup2(write_fd, 2)
        activated = []
        original = l2_main._WarmPrefix.activate

        def activate(self, compiler):
            activated.append(True)
            return original(self, compiler)

        l2_main._WarmPrefix.activate = activate
        if not warm:
            l2_main._WARM_STDLIB = None
        try:
            code = l2_main.cli(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except BaseException as exc:  # pragma: no cover - reported as output
            print(f"EXCEPTION {exc!r}")
            code = 99
        print("@@WARM@@" if activated else "@@COLD@@")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(int(code or 0))
    os.close(write_fd)
    chunks = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    output = b"".join(chunks).decode("utf-8", errors="replace")
    used = "@@WARM@@" in output
    output = output.replace("@@WARM@@", "").replace("@@COLD@@", "")
    return os.WEXITSTATUS(status), output, used


class WarmStdlibTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        l2_main._warm_stdlib_prepare(force=True)
        if l2_main._WARM_STDLIB is None:
            raise unittest.SkipTest("warm stdlib could not be built")
        cls._tmp = tempfile.TemporaryDirectory(prefix="l2-warm-", dir=ROOT / "build")
        cls.dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def emit(self, source: Path, extra=(), *, warm: bool):
        out = self.dir / f"{source.stem}.{'warm' if warm else 'cold'}.asm"
        code, output, used = run_forked([str(source), "--emit-asm", "--force", "-o", str(out), *extra], warm=warm)
        asm = out.read_text() if out.exists() else None
        return code, output.replace(str(out), "<out>"), asm, used

    def test_warm_matches_cold(self) -> None:
        src = self.write("same.sl", "# header comment\nimport stdlib.sl\n"
                         "macro twice (x) `$x $x +` emit ;\n"
                         "word main 21 twice puti cr 4294967296 puti cr end\n")
        cold = self.emit(src, warm=False)
        warm = self.emit(src, warm=True)
        self.assertFalse(cold[3])
        self.assertTrue(warm[3], "warm prefix was not used")
        self.assertEqual(cold[:3], warm[:3])

    def test_errors_match_cold(self) -> None:
        src = self.write("bad.sl", "import stdlib.sl\nword main undefined-word-xyz end\n")
        cold = self.emit(src, warm=False)
        warm = self.emit(src, warm=True)
        self.assertTrue(warm[3])
        self.assertNotEqual(cold[0], 0)
        self.assertEqual(cold[:2], warm[:2])

    def test_not_used_when_first_form_is_not_the_stdlib_import(self) -> None:
        src = self.write("late.sl", "word helper 1 end\nimport stdlib.sl\nword main helper puti cr end\n")
        self.assertFalse(self.emit(src, warm=True)[3])

    def test_not_used_with_other_settings(self) -> None:
        # The default prefix is not used by a job with -D (its settings differ).
        src = self.write("defs.sl", "import stdlib.sl\nword main 1 puti cr end\n")
        l2_main._warm_stdlib_prepare()
        self.assertFalse(self.emit(src, ["-D", "SOMETHING"], warm=True)[3])

    def test_prefix_built_for_warning_and_define_settings(self) -> None:
        src = self.write("warns.sl", "import stdlib.sl\nword dup 1 end\nword main dup puti cr end\n")
        for extra in (["-W", "all"], ["-D", "SOMETHING"], ["-Werror"]):
            with self.subTest(extra=extra):
                l2_main._warm_stdlib_prepare(argv=[str(src), *extra])
                self.assertIsNotNone(l2_main._WARM_STDLIB)
                cold = self.emit(src, extra, warm=False)
                hot = self.emit(src, extra, warm=True)
                self.assertTrue(hot[3], "settings-specific prefix was not used")
                self.assertEqual(cold[:3], hot[:3])
        l2_main._warm_stdlib_prepare()

    def test_library_prefix_for_leading_imports(self) -> None:
        src = self.write("fnprog.sl", "import stdlib/stdlib.sl\nimport stdlib/io.sl\nimport libs/fn.sl\n"
                         "use-fn-dsl\n\nfn add(int a, int b){\n    return a + b;\n}\n\n"
                         "word main 2 3 add puti cr end\n")
        l2_main._warm_stdlib_prepare(argv=[str(src), "-o", "x"])
        warm = l2_main._WARM_STDLIB
        self.assertIsNotNone(warm)
        self.assertEqual([p.name for p in warm.targets], ["stdlib.sl", "io.sl", "fn.sl"])
        cold = self.emit(src, warm=False)
        hot = self.emit(src, warm=True)
        self.assertTrue(hot[3], "library prefix was not used")
        self.assertEqual(cold[:3], hot[:3])
        # The stdlib-only prefix is kept too.
        l2_main._warm_stdlib_prepare()
        self.assertEqual([p.name for p in l2_main._WARM_STDLIB.targets], ["stdlib.sl"])

    def test_trusted_dirs_extend_prefixes(self) -> None:
        self.write("projlib.sl", "word proj-helper 41 end\n")
        src = self.write("proj.sl", "import stdlib.sl\nimport projlib.sl\nword main proj-helper 1 + puti cr end\n")
        old = os.environ.get("L2_DAEMON_TRUSTED_DIRS")
        os.environ["L2_DAEMON_TRUSTED_DIRS"] = str(self.dir)
        try:
            self.assertEqual([p.name for p in l2_main._warm_job_targets([str(src)])], ["stdlib.sl", "projlib.sl"])
            l2_main._warm_stdlib_prepare(argv=[str(src)])
            cold = self.emit(src, warm=False)
            hot = self.emit(src, warm=True)
            self.assertTrue(hot[3], "trusted prefix was not used")
            self.assertEqual(cold[:3], hot[:3])
            # A change to the project file rebuilds its prefix.
            warm = l2_main._WARM_STDLIB
            (self.dir / "projlib.sl").write_text("word proj-helper 42 end\n", encoding="utf-8")
            os.utime(self.dir / "projlib.sl", ns=(1, 1))
            self.assertFalse(warm.fresh())
        finally:
            if old is None:
                os.environ.pop("L2_DAEMON_TRUSTED_DIRS", None)
            else:
                os.environ["L2_DAEMON_TRUSTED_DIRS"] = old
            l2_main._warm_stdlib_prepare()

    def test_untrusted_imports_are_not_prebuilt(self) -> None:
        self.write("mylib.sl", "word helper 1 end\n")
        src = self.write("uses.sl", "import stdlib.sl\nimport mylib.sl\nword main helper drop end\n")
        self.assertEqual([p.name for p in l2_main._warm_job_targets([str(src)])], ["stdlib.sl"])

    def test_rebuild_on_change_and_force_stdlib_only(self) -> None:
        warm = l2_main._WARM_STDLIB
        l2_main._warm_stdlib_prepare()
        self.assertIs(l2_main._WARM_STDLIB, warm, "rebuilt without a change")
        l2_main._warm_stdlib_prepare(force=True)
        rebuilt = l2_main._WARM_STDLIB
        self.assertIsNot(rebuilt, warm, "--force-stdlib did not rebuild")
        # A changed mtime of any stdlib file invalidates the prefix.
        path = next(iter(rebuilt.files))
        mtime_ns, size, ctime_ns, inode = rebuilt.files[path]
        rebuilt.files[path] = (mtime_ns - 1, size, ctime_ns, inode)
        self.assertFalse(rebuilt.fresh())
        l2_main._warm_stdlib_prepare()
        self.assertIsNot(l2_main._WARM_STDLIB, rebuilt)
        self.assertTrue(l2_main._WARM_STDLIB.fresh())


if __name__ == "__main__":
    unittest.main()
