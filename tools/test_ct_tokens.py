#!/usr/bin/env python3
"""Contract tests for immutable Tokens, SourceOrigin provenance, and the
Token API words (token-from-origin, token-with-lexeme, token-origin,
token-expansion-chain, source-origin-*, expansion-frame-*)."""

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
from l2_main import ExpansionFrame, ParseError, SourceOrigin, Token


def tokens_for(main_text: str, files: dict) -> tuple:
    """Write files into a temp dir, load main.sl's source units like the
    compiler does, tokenize every unit, and return (tokens, dir)."""
    tmp = tempfile.TemporaryDirectory(prefix="l2-tokens-")
    root = Path(tmp.name)
    for name, text in files.items():
        (root / name).write_bytes(text.encode("utf-8"))
    main = root / "main.sl"
    main.write_bytes(main_text.encode("utf-8"))
    compiler = l2_main.Compiler(include_paths=[root])
    compiler._load_source_graph(main.resolve())
    toks = [tok for unit in compiler._units.values() for tok in compiler._tokens_for_unit(unit)]
    return toks, root, tmp


def find(tokens, lexeme: str) -> Token:
    for tok in tokens:
        if tok.lexeme == lexeme:
            return tok
    raise AssertionError(f"token {lexeme!r} not found")


class ProvenanceTests(unittest.TestCase):
    def test_imported_token_reports_its_own_file_and_offsets(self) -> None:
        lib = "# helper library\n\n  word helper 1 end\n"
        toks, root, tmp = tokens_for("import lib.sl\nword main helper end\n", {"lib.sl": lib})
        with tmp:
            origin = find(toks, "helper").origin
            self.assertEqual(origin.path, str((root / "lib.sl").resolve()))
            self.assertEqual(origin.start, lib.index("word helper") + 5)
            self.assertEqual(origin.end, lib.index("word helper") + 5 + len("helper"))
            self.assertEqual((origin.start_line, origin.start_column), (3, 8))
            self.assertEqual((origin.end_line, origin.end_column), (3, 14))
            main_origin = find(toks, "main").origin
            self.assertEqual(main_origin.path, str((root / "main.sl").resolve()))
            self.assertEqual((main_origin.start_line, main_origin.start_column), (2, 6))

    def test_crlf_and_lone_cr_line_breaks(self) -> None:
        lib = "word a 1 end\r\nword b 2 end\rword c 3 end\n"
        toks, root, tmp = tokens_for("import lib.sl\n", {"lib.sl": lib})
        with tmp:
            for name, line in (("a", 1), ("b", 2), ("c", 3)):
                origin = find(toks, name).origin
                self.assertEqual(origin.start, lib.index(f"word {name}") + 5, name)
                self.assertEqual(origin.start_line, line, name)
                self.assertEqual(origin.start_column, 6, name)

    def test_columns_count_code_points_and_tabs_as_one(self) -> None:
        lib = "\té x\n"
        toks, root, tmp = tokens_for("import lib.sl\n", {"lib.sl": lib})
        with tmp:
            origin = find(toks, "x").origin
            self.assertEqual(origin.start, 3)
            self.assertEqual(origin.start_column, 4)

    def test_multiline_string_span(self) -> None:
        lib = 'word s "a\nb" end\n'
        toks, root, tmp = tokens_for("import lib.sl\n", {"lib.sl": lib})
        with tmp:
            origin = find(toks, '"a\nb"').origin
            self.assertEqual((origin.start, origin.end), (7, 12))
            self.assertEqual((origin.start_line, origin.end_line, origin.end_column), (1, 2, 3))

    def test_in_memory_source_uses_default_path(self) -> None:
        reader = l2_main.Reader()
        tok = reader.tokenize("  hello\n", default_path="<memory>")[0]
        self.assertEqual(tok.origin.path, "<memory>")
        self.assertEqual((tok.origin.start, tok.origin.start_column), (2, 3))


class ImmutabilityTests(unittest.TestCase):
    def test_token_origin_and_frame_are_immutable(self) -> None:
        tok = l2_main.Reader().tokenize("x")[0]
        with self.assertRaises(AttributeError):
            tok.lexeme = "y"
        with self.assertRaises(AttributeError):
            tok.origin.start = 5
        frame = ExpansionFrame("macro", "m", tok)
        with self.assertRaises(AttributeError):
            frame.kind = "other"
        self.assertIs(frame.call_site, tok.origin)


class TokenWordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compiler = l2_main.Compiler()
        cls.vm = cls.compiler.parser.compile_time_vm

    def run_word(self, name: str, *args):
        word = self.compiler.dictionary.lookup(name)
        self.assertIsNotNone(word, name)
        self.vm.stack = list(args)
        word.compile_time_intrinsic(self.vm)
        return list(self.vm.stack)

    def source_token(self) -> Token:
        return l2_main.Reader().tokenize("  base", default_path="<memory>")[0]

    def test_retired_words_are_gone(self) -> None:
        for name in ("token-from-lexeme", "token-clone", "token-shift-column"):
            self.assertIsNone(self.compiler.dictionary.lookup(name), name)

    def test_token_with_lexeme_keeps_origin_and_chain(self) -> None:
        base = self.source_token()
        framed = l2_main._derive_token(base, "base", frame=ExpansionFrame("reader", "r", base))
        out = self.run_word("token-with-lexeme", framed, "renamed")[0]
        self.assertEqual(out.lexeme, "renamed")
        self.assertIs(out.origin, base.origin)
        self.assertEqual(out.expansion_chain, framed.expansion_chain)
        self.assertEqual(framed.lexeme, "base")  # template unchanged

    def test_token_from_origin(self) -> None:
        origin = SourceOrigin("/x/y.sl", 4, 7, 2, 1, 2, 4)
        tok = self.run_word("token-from-origin", "abc", origin)[0]
        self.assertEqual(tok.lexeme, "abc")
        self.assertIs(tok.origin, origin)
        self.assertEqual(tok.expansion_chain, ())
        self.assertEqual(self.run_word("token-line", tok), [2])
        self.assertEqual(self.run_word("token-column", tok), [1])

    def test_empty_lexeme_rejected(self) -> None:
        origin = SourceOrigin("/x", 0, 0, 1, 1, 1, 1)
        with self.assertRaises(ParseError):
            self.run_word("token-from-origin", "", origin)
        with self.assertRaises(ParseError):
            self.run_word("token-with-lexeme", self.source_token(), "")

    def test_accessors_and_strictness(self) -> None:
        tok = self.source_token()
        origin = self.run_word("token-origin", tok)[0]
        self.assertIsInstance(origin, SourceOrigin)
        self.assertEqual(self.run_word("source-origin-path", origin), ["<memory>"])
        self.assertEqual(self.run_word("source-origin-start", origin), [2])
        self.assertEqual(self.run_word("source-origin-end", origin), [6])
        self.assertEqual(self.run_word("source-origin-start-column", origin), [3])
        self.assertEqual(self.run_word("source-origin-end-column", origin), [7])
        self.assertEqual(self.run_word("token-expansion-chain", tok), [[]])
        frame = ExpansionFrame("macro", "twice", tok)
        self.assertEqual(self.run_word("expansion-frame-kind", frame), ["macro"])
        self.assertEqual(self.run_word("expansion-frame-name", frame), ["twice"])
        self.assertIs(self.run_word("expansion-frame-call-site", frame)[0], tok.origin)
        self.assertEqual(self.run_word("expansion-frame-definition-origin", frame), [None])
        for name, arg in (("token-lexeme", "text"), ("token-origin", "text"),
                          ("source-origin-path", tok), ("expansion-frame-kind", origin)):
            with self.assertRaises(ParseError, msg=name):
                self.run_word(name, arg)


class ExpansionFrameIntegrationTests(unittest.TestCase):
    """Frames appended by real transformations, observed from L2 code."""

    def compile_output(self, source: str) -> str:
        with tempfile.TemporaryDirectory(prefix="l2-frames-") as raw:
            path = Path(raw) / "frames.sl"
            path.write_text(textwrap.dedent(source), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact", "--no-daemon"],
                cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    INSPECT = """
        import stdlib.sl
        # Print the innermost frame of the next token: kind, name, call-site line.
        word inspect_next
          0 ct-parser-peek token-expansion-chain
          dup list-length puti cr
          list-last
          dup expansion-frame-kind puts
          dup expansion-frame-name puts
          expansion-frame-call-site source-origin-start-line puti cr
        end
        immediate
        compile-only
        word target 0 end
    """

    def test_grammar_rewrite_frame(self) -> None:
        out = self.compile_output(self.INSPECT + """
        word setup
          "frame_rule"
          list-new "frame_trigger" list-append
          list-new "inspect_next" list-append "target" list-append
          ct-rewrite-add-grammar-named drop
        end
        compile-time setup
        word main
          frame_trigger drop
        end
        """)
        self.assertIn("1\ngrammar\nframe_rule\n", out)

    def test_text_macro_frame(self) -> None:
        out = self.compile_output(self.INSPECT + """
        macro probe_macro `inspect_next target` emit ;
        word main
          probe_macro drop
        end
        """)
        self.assertIn("1\nmacro\nprobe_macro\n", out)

    def test_token_hook_frame(self) -> None:
        out = self.compile_output(self.INSPECT + """
        word hook_probe
          dup token-lexeme "hook_trigger" string-equal? if
            list-new
            over "inspect_next" token-with-lexeme list-append
            over "target" token-with-lexeme list-append
            ct-parser-inject
            drop 1
          else
            drop 0
          end
        end
        compile-only
        word install_hook "hook_probe" ct-parser-set-token-hook end
        compile-time install_hook
        word main
          hook_trigger drop
        end
        """)
        self.assertIn("1\ntoken-hook\nhook_probe\n", out)


if __name__ == "__main__":
    unittest.main()
