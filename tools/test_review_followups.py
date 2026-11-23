#!/usr/bin/env python3
"""Regression tests: fn duplicate parameters and error locations, quotation-
built globals, --macro-preview, import resolution, generated-code error
locations, the CT store, rewrite tracing, and compile-time munmap."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main
from l2_main import CompileError, ParseError


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-followups-")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, source: str, name: str = "t.sl") -> Path:
        path = self.dir / name
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return path

    def compile(self, source: str):
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT, ROOT / "stdlib"])
        try:
            emission = compiler.compile_file(self.write(source))
            return compiler, emission, None
        except (ParseError, CompileError) as exc:
            return compiler, None, exc

    def first_error(self, compiler):
        return next(d for d in compiler.parser.reportable_diagnostics() if d.severity == "error")


FN_PRELUDE = """
    import stdlib/stdlib.sl
    import stdlib/io.sl
    import libs/fn.sl
    use-fn-dsl
"""


class FnDslTests(Base):
    def test_duplicate_parameters_are_rejected(self) -> None:
        compiler, _, error = self.compile(FN_PRELUDE + """
            fn twice(int x, int x){
                x + x
            }
            word main 0 end
        """)
        self.assertIsNotNone(error)
        self.assertIn("duplicate parameter names", self.first_error(compiler).message)

    def test_distinct_parameters_still_compile(self) -> None:
        _, _, error = self.compile(FN_PRELUDE + """
            fn add3(int a, int b, int c){
                a + b + c
            }
            word main 1 2 3 add3 drop 0 end
        """)
        self.assertIsNone(error, error)

    def test_body_errors_point_into_the_body(self) -> None:
        compiler, _, error = self.compile(FN_PRELUDE + """
            fn bad(int x){
                x +
                nosuchword
            }

            word main 1 bad drop 0 end
        """)
        self.assertIsNotNone(error)
        primary = self.first_error(compiler).primary
        self.assertEqual(Path(primary.path).name, "t.sl")
        self.assertEqual((primary.start_line, primary.start_column), (8, 13))  # `nosuchword`


class GlobalsTests(Base):
    def test_global_and_sized_global(self) -> None:
        compiler, emission, error = self.compile("""
            import stdlib.sl
            import gvars.sl
            global counter
            sized_global buf 32
            word main 5 counter ! counter @ drop buf drop 0 end
        """)
        self.assertIsNone(error, error)
        text = emission.snapshot()
        self.assertIn("counter_global: dq 0", text)
        self.assertIn("buf_global: times 32 db 0", text)
        counter = compiler.dictionary.lookup("counter")
        self.assertIn("counter_global", counter.definition.body)
        self.assertEqual(Path(l2_main._word_source_origin(counter).path).name, "t.sl")


class CliTests(Base):
    def run_cli(self, path: Path, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "main.py"), str(path), "--emit-asm", "--no-daemon",
                               "--no-cache", "-o", str(self.dir / "out.asm"), *extra],
                              cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)

    def test_macro_preview_shows_macro_expansions(self) -> None:
        path = self.write("""
            import stdlib.sl
            macro twice (x) `$x $x` emit ;
            word main 3 twice 4 drop drop 0 end
        """)
        proc = self.run_cli(path, "--macro-preview")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"[macro-preview] twice at {path}:3:13", proc.stderr)
        self.assertIn("expand: 4 4", proc.stderr)

    def test_unresolved_import_lists_each_path_once(self) -> None:
        path = self.write('import "missing_lib.sl"\nword main 0 end\n')
        proc = subprocess.run([sys.executable, str(ROOT / "main.py"), str(path), "--emit-asm", "--no-daemon",
                               "--no-cache", "-I", str(self.dir)],
                              cwd=self.dir, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=300)
        self.assertNotEqual(proc.returncode, 0)
        tried = [line for line in proc.stdout.splitlines() + proc.stderr.splitlines() if line.startswith("  - ")]
        self.assertTrue(tried)
        self.assertEqual(len(tried), len(set(tried)), tried)


class CompileTimeMunmapTests(Base):
    def test_only_own_mappings_can_be_unmapped(self) -> None:
        # A program run inside the compiler (--script, --ct-run-main) may unmap
        # what it mapped; anything else gets -EINVAL instead of unmapping the
        # compiler's own memory.
        path = self.write("""
            import stdlib.sl
            word main
              4096 alloc dup 4096 munmap puti cr
              "abc" drop 4096 munmap puti cr
            end
        """)
        result = subprocess.run([sys.executable, str(ROOT / "main.py"), "--no-daemon", str(path), "--script",
                                 "-I", str(ROOT / "stdlib")], capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:2], ["0", "-22"])


class SelfNamedImportTests(Base):
    def test_a_file_does_not_import_itself(self) -> None:
        # `import ct_store.sl` in a file named ct_store.sl means the stdlib's.
        path = self.write("""
            import stdlib.sl
            import ct_store.sl
            word set "k" 5 ct-store-set end
            compile-time set
            word main 0 end
        """, name="ct_store.sl")
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"])
        compiler.compile_file(path)
        self.assertIsNotNone(compiler.dictionary.lookup("ct-store-set"))

    def test_sibling_still_imports_the_local_file(self) -> None:
        self.write("word local-marker 1 end\n", name="helper.sl")
        self.write("import helper.sl\nword main 0 end\n", name="helper_user.sl")
        compiler = l2_main.Compiler(include_paths=[self.dir])
        compiler.compile_file(self.dir / "helper_user.sl")
        self.assertIsNotNone(compiler.dictionary.lookup("local-marker"))


class CtStoreTests(Base):
    def test_equal_looking_token_from_elsewhere_replaces_the_value(self) -> None:
        compiler, _, error = self.compile("""
            import stdlib.sl
            import ct_store.sl
            word keep ct-parser-current "t" swap ct-store-set end
            word check "t" ct-store-get drop token-line 7 == static-assert end
            compile-time keep
            # padding
            compile-time keep
            compile-time check
            word main 0 end
        """)
        self.assertIsNone(error, error)


class RewriteTraceTests(Base):
    def test_only_rewrites_in_the_traced_region_are_reported(self) -> None:
        compiler, _, error = self.compile("""
            import stdlib.sl
            import rewrite_trace.sl
            word setup
              "tw-r" list-new "tw" list-append list-new "2" list-append ct-rewrite-add-grammar-named drop
            end
            compile-time setup
            rewrite-trace-on
            word a tw drop end
            rewrite-trace-off
            word main a tw drop 0 end
        """)
        self.assertIsNone(error, error)
        notes = [d.message for d in compiler.parser.diagnostics if d.severity == "note"]
        self.assertEqual(len(notes), 1, notes)
        self.assertRegex(notes[0], r"^rewrite grammar 'tw-r' at .*t\.sl:8:\d+: tw -> 2$")


class GeneratedCodeLocationTests(Base):
    """Errors in code generated by library DSLs point at the user's code."""

    def assertErrorAt(self, source: str, word: str, line: int) -> None:
        compiler, _, error = self.compile(source)
        self.assertIsNotNone(error)
        diag = self.first_error(compiler)
        self.assertIn(word, diag.message)
        self.assertEqual(Path(str(diag.path)).name, "t.sl", diag.format(color=False))
        self.assertEqual(diag.line, line, diag.format(color=False))

    def test_lambda_body(self) -> None:
        self.assertErrorAt("""
            import stdlib.sl
            import lambda.sl
            word main
              lambda(
                1 no-such-word
              ) call
            end
        """, "no-such-word", 5)

    def test_fn_operator_pieces_have_exact_columns(self) -> None:
        compiler, _, error = self.compile(FN_PRELUDE + """
            fn f(int a, int b){
                return a+nosuch;
            }
            word main 1 2 f drop end
        """)
        self.assertIsNotNone(error)
        diag = self.first_error(compiler)
        self.assertIn("nosuch", diag.message)
        line = (self.dir / "t.sl").read_text().splitlines()[diag.line - 1]
        self.assertIn("a+nosuch", line)
        self.assertEqual(diag.column, line.index("nosuch") + 1, diag.format(color=False))

    def test_meta_define_const(self) -> None:
        self.assertErrorAt("""
            import stdlib.sl
            import meta.sl
            word setup "gen" "missing-target" meta-define-const end
            compile-time setup
            word main gen end
        """, "missing-target", 4)

    def test_meta_generated_macro(self) -> None:
        self.assertErrorAt("""
            import stdlib.sl
            import meta.sl
            word setup "combine" "no-such-op" meta-macro-register-binary-op end
            compile-time setup
            word main combine 1 2 drop end
        """, "no-such-op", 4)


if __name__ == "__main__":
    unittest.main()
