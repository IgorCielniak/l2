#!/usr/bin/env python3
"""Contract tests for parser-driven imports and custom frontends:
the canonical `import "path"` request, the `frontend` bootstrap header,
frontend inheritance, implementation loading, entry-point results, and
frontend expansion frames."""

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
from l2_main import ParseError

# A toy frontend: default tokenization, header skipped, `plus` lowered to
# `+` and `say` to `puti cr`.  Extra entry points misbehave on purpose.
CALC = """
import stdlib.sl

# calc-strip-header [* | tokens] -> [* | tokens]
word calc-strip-header
  dup list-length 0 == if exit end
  dup 0 list-get token-lexeme "frontend" string-equal? 0 == if exit end
  dup list-length 2 > if
    dup 2 list-get token-line over 0 list-get token-line == if 3 else 2 end
  else 2 end
  over list-length list-slice
end
compile-only

# calc-emit [*, out | token] -> [* | out]
word calc-emit
  dup token-lexeme "plus" string-equal? if "+" token-with-lexeme list-append exit end
  dup token-lexeme "say" string-equal? if
    dup "puti" token-with-lexeme rot swap list-append
    swap "cr" token-with-lexeme list-append exit
  end
  list-append
end
compile-only

# frontend-parse [*, text | path] -> [* | tokens]
word frontend-parse
  ct-tokenizer-run calc-strip-header
  list-new swap
  dup list-length for
    dup i list-get rot swap calc-emit swap
  end
  drop
end
compile-only

word calc-parse frontend-parse end
compile-only

word bad-two drop drop 1 2 end
compile-only
word bad-item drop drop list-new "x" list-append end
compile-only
word bad-fail drop drop "boom" ct-parser-error end
compile-only
word bad-runtime drop drop list-new end
runtime-only
"""


class FrontendTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="l2-frontend-")
        self.dir = Path(self._tmp.name)
        self.write("calc.sl", CALC)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return path

    def run_program(self, name: str) -> subprocess.CompletedProcess:
        out = self.dir / "prog"
        build = subprocess.run(
            [sys.executable, str(ROOT / "main.py"), str(self.dir / name), "--no-daemon", "--no-cache",
             "-o", str(out)],
            cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180,
        )
        if build.returncode != 0:
            return build
        return subprocess.run([str(out)], capture_output=True, text=True, timeout=30)

    def assertOutput(self, name: str, expected: str) -> None:
        proc = self.run_program(name)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, expected)

    def assertCompileError(self, name: str, code: str) -> str:
        proc = self.run_program(name)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(f"[{code}]", proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def parse(self, name: str) -> l2_main.Compiler:
        compiler = l2_main.Compiler(include_paths=[self.dir, ROOT / "stdlib"])
        compiler.parse_file(self.dir / name)
        return compiler


class ParserDrivenImportTests(FrontendTestBase):
    def test_tokenizer_config_applies_to_later_imports(self) -> None:
        # The separator added at compile time splits `2+` in lib.sl, which
        # is tokenized only when the parser reaches its import.
        self.write("lib.sl", "word three 1 2+ end\n")
        self.write("main.sl", """
            import stdlib.sl
            word add-plus
              ct-tokenizer-get-config dup "separators" map-get drop
              "+" list-append "separators" swap map-set ct-tokenizer-set-config
            end
            compile-time add-plus
            import lib.sl
            word main three puti cr 0 end
        """)
        self.assertOutput("main.sl", "3\n")

    def test_define_in_import_controls_later_ifdef(self) -> None:
        self.write("config.sl", "define FAST\n")
        self.write("main.sl", """
            import stdlib.sl
            import config.sl
            ifdef FAST
            word speed 1 end
            elsedef
            word speed 0 end
            endif
            word main speed puti cr 0 end
        """)
        self.assertOutput("main.sl", "1\n")

    def test_import_line_with_trailing_statements(self) -> None:
        self.write("lib.sl", "word one 1 end\n")
        self.write("main.sl", """
            import stdlib.sl
            import lib.sl word two one one + end
            word main two puti cr 0 end
        """)
        self.assertOutput("main.sl", "2\n")

    def test_import_is_deduplicated(self) -> None:
        self.write("lib.sl", "word one 1 end\n")
        self.write("main.sl", """
            import stdlib.sl
            import lib.sl
            import "lib.sl"
            word main one puti cr 0 end
        """)
        compiler = self.parse("main.sl")
        self.assertIn((self.dir / "lib.sl").resolve(), compiler._loaded_files)
        self.assertOutput("main.sl", "1\n")

    def test_imported_definition_location_names_its_file(self) -> None:
        self.write("lib.sl", "# one\n# two\nword broken nosuchword end\n")
        self.write("main.sl", "import stdlib.sl\nimport lib.sl\nword main broken 0 end\n")
        out = self.run_program("main.sl")
        text = out.stdout + out.stderr
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("lib.sl:3:13", text)

    def test_unresolved_import(self) -> None:
        self.write("main.sl", "import stdlib.sl\nimport missing.sl\nword main 0 end\n")
        self.assertCompileError("main.sl", "import-unresolved")


class FrontendHeaderTests(FrontendTestBase):
    def test_root_file_with_frontend(self) -> None:
        self.write("prog.sl", """
            frontend "calc.sl"
            word main 2 3 plus say 0 end
        """)
        self.assertOutput("prog.sl", "5\n")

    def test_explicit_entry_and_trailing_comment(self) -> None:
        self.write("prog.sl", """

            frontend "calc.sl" calc-parse   # selected entry
            word main 4 4 plus say 0 end
        """)
        self.assertOutput("prog.sl", "8\n")

    def test_header_in_imported_file(self) -> None:
        self.write("part.sl", """
            frontend "calc.sl"
            word five 2 3 plus end
        """)
        self.write("main.sl", """
            import stdlib.sl
            import part.sl
            word main five puti cr 0 end
        """)
        self.assertOutput("main.sl", "5\n")

    def test_headerless_import_inherits_frontend(self) -> None:
        self.write("sub.sl", "word seven 3 4 plus end\n")
        self.write("prog.sl", """
            frontend "calc.sl"
            import "sub.sl"
            word main seven say 0 end
        """)
        self.assertOutput("prog.sl", "7\n")

    def test_implementation_loaded_once(self) -> None:
        self.write("a.sl", 'frontend "calc.sl"\nword a 1 1 plus end\n')
        self.write("b.sl", 'frontend "calc.sl"\nword b 2 2 plus end\n')
        self.write("main.sl", """
            import stdlib.sl
            import a.sl
            import b.sl
            word main a b + puti cr 0 end
        """)
        compiler = self.parse("main.sl")
        self.assertEqual(compiler._frontends_loaded, {(self.dir / "calc.sl").resolve()})
        self.assertOutput("main.sl", "6\n")

    def test_frontend_frame_and_origin(self) -> None:
        self.write("prog.sl", 'frontend "calc.sl"\nword main 2 3 plus say 0 end\n')
        compiler = self.parse("prog.sl")
        prog = str((self.dir / "prog.sl").resolve())
        plus = next(tok for tok in compiler.parser.tokens if tok.lexeme == "+" and tok.origin.path == prog)
        self.assertEqual(plus.origin.path, str((self.dir / "prog.sl").resolve()))
        self.assertEqual((plus.origin.start_line, plus.origin.start_column), (2, 15))
        frame = plus.expansion_chain[0]
        self.assertEqual((frame.kind, frame.name), ("frontend", "frontend-parse"))
        self.assertEqual((frame.call_site.start_line, frame.call_site.start_column), (1, 1))

    def test_frontend_output_skips_reader_rewrites(self) -> None:
        self.write("rules.sl", """
            import stdlib.sl
            word add-rule
              list-new "seven" list-append list-new "7" list-append ct-rewrite-add-reader drop
            end
            compile-time add-rule
        """)
        self.write("calc_rw.sl", "import rules.sl\nimport calc.sl\n")
        self.write("prog.sl", 'frontend "calc_rw.sl"\nword main seven say 0 end\n')
        out = self.run_program("prog.sl")
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("seven", out.stdout + out.stderr)

    def test_not_a_header(self) -> None:
        self.write("lib.sl", "word frontends 1 end\n")
        self.write("main.sl", """
            import stdlib.sl
            import lib.sl
            word main frontends puti cr 0 end
        """)
        self.assertOutput("main.sl", "1\n")


class FrontendErrorTests(FrontendTestBase):
    def check(self, header: str, code: str) -> str:
        self.write("prog.sl", f"{header}\nword main 0 end\n")
        return self.assertCompileError("prog.sl", code)

    def test_malformed_headers(self) -> None:
        for header in ('frontend calc.sl', 'frontend "calc.sl" a b', 'frontend', 'frontend ""',
                       'frontend "calc.sl" 9x', 'frontend "calc.sl" (', 'frontend "unterminated'):
            with self.subTest(header=header):
                self.check(header, "frontend-header-invalid")

    def test_missing_implementation(self) -> None:
        self.check('frontend "nope.sl"', "frontend-implementation-unavailable")

    def test_missing_or_runtime_only_entry(self) -> None:
        self.check('frontend "calc.sl" no-such-entry', "frontend-entry-unavailable")
        self.check('frontend "calc.sl" bad-runtime', "frontend-entry-unavailable")

    def test_invalid_results(self) -> None:
        self.check('frontend "calc.sl" bad-two', "frontend-invalid-result")
        self.check('frontend "calc.sl" bad-item', "frontend-invalid-result")

    def test_entry_failure(self) -> None:
        out = self.check('frontend "calc.sl" bad-fail', "frontend-entry-failed")
        self.assertIn("boom", out)

    def test_malformed_import_request(self) -> None:
        self.write("sub.sl", "word x 1 end\n")
        self.write("prog.sl", 'frontend "calc.sl"\nimport sub.sl\nword main 0 end\n')
        self.assertCompileError("prog.sl", "import-malformed")


if __name__ == "__main__":
    unittest.main()
