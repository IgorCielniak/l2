#!/usr/bin/env python3
"""Every compile-time word main.c's VM implements behaves as in the
Python compiler.  Each snippet runs once in the Python CT VM (a word run with
`compile-time`) and once in the C VM (`eval_program`, which runs `main` at
compile time); the printed output must match.

Snippets are grouped per word; a snippet is (prelude, body): the prelude
holds helper definitions (immediate words for the parser words), the body
prints its results with puti/puts/cr."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import l2_main

PRINT_TOKEN = 'consume-print'
PARSER_PRELUDE = """
word consume-print ct-parser-consume token-lexeme puts cr end
immediate compile-only
"""

SNIPPETS = {
    # stack and return stack
    "dup": ("", "3 dup + puti cr"),
    "drop": ("", "1 2 drop puti cr"),
    "swap": ("", "1 2 swap - puti cr"),
    "over": ("", "1 2 over + + puti cr"),
    "rot": ("", "1 2 3 rot puti cr puti cr puti cr"),
    "pick": ("", "10 20 30 2 pick puti cr drop drop drop"),
    "rpick": ("", "5 >r 6 >r 1 rpick puti cr rdrop rdrop"),
    ">r": ("", "7 >r r> puti cr"),
    "r>": ("", "8 >r 9 >r r> r> - puti cr"),
    "rdrop": ("", "1 >r 2 >r rdrop r> puti cr"),
    # arithmetic and comparisons (edges included)
    "+": ("", "9223372036854775807 1 + puti cr 2 3 + puti cr"),
    "-": ("", "5 7 - puti cr"),
    "*": ("", "-6 7 * puti cr 4294967296 4294967296 * puti cr"),
    "/": ("", "-7 2 / puti cr 7 -2 / puti cr"),
    "%": ("", "-7 2 % puti cr 7 -2 % puti cr"),
    "==": ("", "3 3 == puti cr 3 4 == puti cr"),
    "!=": ("", "3 3 != puti cr 3 4 != puti cr"),
    ">": ("", "3 4 > puti cr 4 3 > puti cr"),
    "<": ("", "-1 0 < puti cr 0 -1 < puti cr"),
    ">=": ("", "3 3 >= puti cr 2 3 >= puti cr"),
    "<=": ("", "3 3 <= puti cr 4 3 <= puti cr"),
    "not": ("", "0 not puti cr 5 not puti cr"),
    # nil
    "nil": ("", "nil nil? puti cr"),
    "nil?": ("", "nil nil? puti cr 0 nil? puti cr"),
    # strings
    "string-equal?": ("", '"ab" "ab" string-equal? puti cr "ab" "ac" string-equal? puti cr'),
    "string-length": ("", '"größe" string-length puti cr "" string-length puti cr'),
    "string-append": ("", '"ab" "cd" string-append puts cr'),
    "try-string>int": ("", '"42" try-string>int puti cr puti cr "x" try-string>int puti cr drop'),
    "string-contains?": ("", '"hello" "ell" string-contains? puti cr "hello" "z" string-contains? puti cr'),
    "string-starts-with?": ("", '"hello" "he" string-starts-with? puti cr "hello" "lo" string-starts-with? puti cr'),
    "string-ends-with?": ("", '"hello" "lo" string-ends-with? puti cr "hello" "he" string-ends-with? puti cr'),
    "int>string": ("", '-15 int>string puts cr'),
    "identifier?": ("", '"a_b" identifier? puti cr "1a" identifier? puti cr "größe" identifier? puti cr'),
    # lists
    "list-new": ("", "list-new list-length puti cr"),
    "list-append": ("", "list-new 1 list-append 2 list-append list-length puti cr"),
    "list-pop": ("", "list-new 4 list-append list-pop puti cr puti cr list-length puti cr"),
    "list-pop-front": ("", "list-new 4 list-append 5 list-append list-pop-front puti cr puti cr list-length puti cr"),
    "list-peek-front": ("", "list-new 9 list-append list-peek-front puti cr"),
    "list-push-front": ("", "list-new 1 list-append 0 list-push-front 0 list-get puti cr"),
    "list-reverse": ("", "list-new 1 list-append 2 list-append list-reverse 0 list-get puti cr"),
    "list-clear": ("", "list-new 1 list-append list-clear list-length puti cr"),
    "list-length": ("", "list-new 1 list-append 1 list-append list-length puti cr"),
    "list-empty?": ("", "list-new list-empty? puti cr list-new 1 list-append list-empty? puti cr"),
    "list-get": ("", "list-new 1 list-append 2 list-append -1 list-get puti cr"),
    "list-set": ("", "list-new 1 list-append 0 7 list-set 0 list-get puti cr"),
    "list-extend": ("", "list-new 1 list-append list-new 2 list-append list-extend list-length puti cr"),
    "list-last": ("", "list-new 1 list-append 2 list-append list-last puti cr"),
    "list-clone": ("", "list-new 5 list-append list-clone list-length puti cr"),
    # maps
    "map-new": ("", "map-new map-length puti cr"),
    "map-set": ("", 'map-new "a" 1 map-set "a" 2 map-set map-length puti cr'),
    "map-get": ("", 'map-new "a" 10 map-set "a" map-get puti cr puti cr map-new "z" map-get puti cr nil? puti cr'),
    "map-has?": ("", 'map-new "k" 0 map-set "k" map-has? puti cr map-new "k" map-has? puti cr'),
    "map-length": ("", 'map-new "a" 1 map-set "b" 2 map-set map-length puti cr'),
    "map-empty?": ("", 'map-new map-empty? puti cr'),
    "map-clear": ("", 'map-new "a" 1 map-set map-clear map-length puti cr'),
    "map-keys": ("", 'map-new "b" 1 map-set "a" 2 map-set map-keys 0 list-get puts cr'),
    "map-values": ("", 'map-new "b" 1 map-set "a" 2 map-set map-values 1 list-get puti cr'),
    # tokens and the parser
    "token-lexeme": (PARSER_PRELUDE, f"{PRINT_TOKEN} word-x"),
    "token-with-lexeme": (PARSER_PRELUDE + """
word retoken ct-parser-consume "renamed" token-with-lexeme token-lexeme puts cr end
immediate compile-only
""", "retoken x"),
    "token-line": (PARSER_PRELUDE + """
word line-of ct-parser-consume token-line 0 > puti cr end
immediate compile-only
""", "line-of x"),
    "token-column": (PARSER_PRELUDE + """
word col-of ct-parser-consume token-column 0 > puti cr end
immediate compile-only
""", "col-of x"),
    "ct-parser-consume": (PARSER_PRELUDE, f"{PRINT_TOKEN} a {PRINT_TOKEN} b"),
    "ct-parser-peek": ("""
word peek0 0 ct-parser-peek token-lexeme puts cr end
immediate compile-only
""", "peek0 drop-me drop"),
    "ct-parser-eof?": ("""
word at-end ct-parser-eof? puti cr end
immediate compile-only
""", "at-end"),
    "ct-parser-inject": ("""
word inject-2 list-new ct-parser-current "2" token-with-lexeme list-append ct-parser-inject end
immediate compile-only
""", "inject-2 puti cr"),
    "ct-parser-current": ("""
word current ct-parser-current token-lexeme puts cr end
immediate compile-only
""", "current"),
    "ct-parser-error": ("""
word fail "boom" ct-parser-error end
immediate compile-only
""", "fail"),
    # output buffers: only their stack effects are visible here
    "ct-output-append-data": ("", '"x: dq 1" ct-output-append-data 1 puti cr'),
    "ct-output-clear-data": ("", 'ct-output-clear-data 1 puti cr'),
    "ct-output-set-data": ("", 'list-new "x: dq 1" list-append ct-output-set-data 1 puti cr'),
    "ct-output-append-text-preamble": ("", '"; nothing" ct-output-append-text-preamble 1 puti cr'),
    "ct-output-clear-text-preamble": ("", 'ct-output-clear-text-preamble 1 puti cr'),
    "ct-output-set-text-preamble": ("", 'list-new ct-output-set-text-preamble 1 puti cr'),
    "ct-output-append-bss": ("", '"y: resq 1" ct-output-append-bss 1 puti cr'),
    "ct-output-clear-bss": ("", 'ct-output-clear-bss 1 puti cr'),
    "ct-output-set-bss": ("", 'list-new "y: resq 1" list-append ct-output-set-bss 1 puti cr'),
    # token hook
    "ct-parser-set-token-hook": ("""
word hook token-lexeme "skipme" string-equal? end
compile-only
word install "hook" ct-parser-set-token-hook end
immediate compile-only
word remove ct-parser-clear-token-hook end
immediate compile-only
""", "install skipme 5 remove puti cr"),
    "ct-parser-clear-token-hook": ("""
word hook token-lexeme "skipme" string-equal? end
compile-only
word install "hook" ct-parser-set-token-hook end
immediate compile-only
word remove ct-parser-clear-token-hook end
immediate compile-only
""", "install remove 6 puti cr"),
    # quotations and Syntax values
    "emit": ("""
word put-7 `7` emit end
immediate compile-only
""", "put-7 puti cr"),
    "list>syntax": ("", "`a b` syntax>list list>syntax syntax-length puti cr"),
    "syntax>list": ("", "`a b c` syntax>list list-length puti cr"),
    "syntax-length": ("", "`a b` syntax-length puti cr"),
    # macro forms
    "macro: quotation with parameters": ("macro add3 (a b c) `$a $b + $c +` emit ;", "add3 1 2 39 puti cr"),
    "macro: general body": ('macro pick-one (x) $x token-lexeme "0" string-equal? if `100` emit else `$x 1 +` emit end ;',
                            "pick-one 0 puti cr pick-one 6 puti cr"),
    "macro: quotation argument": ("macro twice (code) `$code $code` emit ;", "twice `3 4 +` + puti cr"),
    "macro: unquote and pasting": ("macro getter (n) `word get-$( $n token-lexeme ) $( 6 7 * ) end` emit ;\ngetter x",
                                   "get-x puti cr"),
    "macro: called from a macro": ("macro one `1` emit ;\nmacro one-plus (x) one `$x +` emit ;",
                                   "one-plus 41 puti cr"),
    "macro: const": ("const ANSWER 42", "ANSWER puti cr"),
    "float literals are IEEE bits": ("import float.sl", "1.5 2.25 f+ float>int puti cr 0.1 0.2 f< puti cr"),
    "ct-definition-prefer-l2-by-name": ("""
word twice dup + end
""", '"twice" ct-definition-prefer-l2-by-name puti cr'),
}

# Words main.c implements that are not CT API words of their own here.
NOT_COMPARED = {"eval_program"}


def c_words() -> set:
    return set(re.findall(r'register_ct_intrinsic\(dict, "([^"]+)"', (ROOT / "main.c").read_text()))


def l2_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def python_output(prelude: str, body: str, tmp: Path) -> str:
    path = tmp / "py.sl"
    path.write_text(f"import stdlib.sl\n{prelude}\nword __p {body} end\ncompile-time __p\nword main end\n",
                    encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "main.py"), str(path), "--no-artifact"],
                            capture_output=True, text=True, cwd=ROOT)
    out = "".join(line + "\n" for line in result.stdout.splitlines() if not line.startswith("[info]"))
    return out if result.returncode == 0 else out + "<error>\n"


def c_outputs(snippets: dict, tmp: Path) -> dict:
    lines = ["extern eval_program 2 1", "import stdlib.sl", "word main"]
    for name, (prelude, body) in snippets.items():
        source = f"import stdlib.sl\n{prelude}\nword main {body} end\n"
        lines.append(f'  "@@{name}" puts')
        lines.append(f"  {l2_string(source)} eval_program 0 != if \"<error>\" puts end")
    lines += ['  "@@end" puts', "  0", "end"]
    host = tmp / "host.sl"
    host.write_text("\n".join(lines) + "\n", encoding="utf-8")
    exe = tmp / "host"
    subprocess.run([sys.executable, str(ROOT / "main.py"), "--no-daemon", str(host), "-o", str(exe),
                    "-l", str(ROOT / "build" / "libl2.so")], check=True, capture_output=True, cwd=ROOT)
    env = dict(os.environ, LD_LIBRARY_PATH=str(ROOT / "build"))
    result = subprocess.run([str(exe)], capture_output=True, text=True, env=env, cwd=ROOT)
    outputs = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("@@"):
            current = line[2:]
            outputs[current] = ""
        elif current is not None:
            outputs[current] += line + "\n"
    return outputs


@unittest.skipUnless((ROOT / "build" / "libl2.so").exists(), "build/libl2.so not built (tools/build_l2_lib.sh)")
class CVmParityTests(unittest.TestCase):
    def test_every_c_word_has_a_snippet(self) -> None:
        self.assertEqual(sorted(c_words() - set(SNIPPETS) - NOT_COMPARED), [])

    def test_stack_underflow_is_an_error_in_both(self) -> None:
        # An empty-stack pop fails before anything is printed (the Python VM
        # used to read past its stack for an :asm word such as puti; the C VM
        # used to pop nil, so `drop` passed).
        cases = {
            "puti on empty": ("", "puti cr"),
            "+ on empty": ("", "+ puti cr"),
            "+ on one": ("", "5 + puti cr"),
            "drop on empty": ("", "drop 1 puti cr"),
            "dup on empty": ("", "dup puti cr"),
            "valid": ("", "2 3 + puti cr"),
        }
        with tempfile.TemporaryDirectory(prefix="l2-parity-underflow-") as tmp:
            tmp_path = Path(tmp)
            c = c_outputs(cases, tmp_path)
            for name, (prelude, body) in cases.items():
                with self.subTest(case=name):
                    expected = "5\n" if name == "valid" else "<error>\n"
                    self.assertEqual(python_output(prelude, body, tmp_path), expected)
                    self.assertEqual(c.get(name), expected)

    def test_outputs_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="l2-parity-") as tmp:
            tmp_path = Path(tmp)
            c = c_outputs(SNIPPETS, tmp_path)
            self.assertIn("end", c, "the C host program stopped early")
            for name, (prelude, body) in SNIPPETS.items():
                with self.subTest(word=name):
                    self.assertEqual(c.get(name), python_output(prelude, body, tmp_path),
                                     f"{name}: C (first) vs Python (second)")


if __name__ == "__main__":
    unittest.main()
