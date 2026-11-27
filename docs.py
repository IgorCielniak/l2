"""Structured compile-time docs helpers for the L2 docs explorer.

This module is intentionally lightweight and is imported lazily by l2_main.py
only when docs or integrity features need it.
"""

from __future__ import annotations

import ast
import html
import json
import os
import re

_CT_REF_SECTION_RE = re.compile(r"^\s*§\s*\d+[A-Za-z]?\s+(.+?)\s*$")
_CT_REF_WORD_RE = re.compile(r"^\s{2,}[A-Za-z0-9_?.:+\-*/<>=!&]+(?:\s{2,}|\s+\[)")
_CT_REF_ENTRY_LINE_RE = re.compile(r"^\s{2,}([A-Za-z0-9][A-Za-z0-9_?.:+\-*/<>=!&]*)(?:\s{2,}|\s+\[|$)")
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

_ENTRY_RE = re.compile(r"^\s{2,}([A-Za-z0-9][A-Za-z0-9_?.:+\-*/>=!&]*)(?:\s{2,}(.*))?$")
_SECTION_RE = re.compile(r"^\s*(?:\u00a7|SECTION)\s*\d+[A-Za-z]?\s+(.+?)\s*$", re.IGNORECASE)

_GENERIC_HINTS = (
    "helpers for",
    "toolkit",
    "controls",
    "metadata",
    "introspection",
    "for metaprogramming",
)


# ---- Migrated docs explorer surface from l2_main.py ----

class DocEntry:
    __slots__ = ('name', 'stack_effect', 'description', 'kind', 'path', 'line')

    def __init__(self, name: str, stack_effect: str, description: str, kind: str, path: Path, line: int) -> None:
        self.name = name
        self.stack_effect = stack_effect
        self.description = description
        self.kind = kind
        self.path = path
        self.line = line


_DOC_STACK_RE = re.compile(r"^\s*#\s*([^\s]+)\s*(.*)$")
_DOC_UNNAMED_STACK_RE = re.compile(
    r"^\s*#\s*(\[[^\]]*\]\s*->\s*\[[^\]]*\](?:\s*\|\|\s*\[[^\]]*\])*)\s*$"
)
_DOC_WORD_RE = re.compile(r"^\s*(?:inline\s+)?word\s+([^\s]+)")
_DOC_ASM_RE = re.compile(r"^\s*:asm\s+([^\s{]+)")
_DOC_PY_RE = re.compile(r"^\s*:py\s+([^\s{]+)")
_DOC_MACRO_RE = re.compile(r"^\s*macro\s+([^\s]+)(?:\s+(\d+))?")


def _extract_stack_comment(text: str) -> Optional[Tuple[Optional[str], str]]:
    unnamed = _DOC_UNNAMED_STACK_RE.match(text)
    if unnamed is not None:
        return None, unnamed.group(1).strip()
    match = _DOC_STACK_RE.match(text)
    if match is None:
        return None
    name = match.group(1).strip()
    tail = match.group(2).strip()
    if "->" not in tail:
        return None
    return name, tail


def _extract_definition_name(text: str, *, include_macros: bool = False) -> Optional[Tuple[str, str, int]]:
    for kind, regex in (("word", _DOC_WORD_RE), ("asm", _DOC_ASM_RE), ("py", _DOC_PY_RE)):
        match = regex.match(text)
        if match is not None:
            return kind, match.group(1), -1
    if include_macros:
        match = _DOC_MACRO_RE.match(text)
        if match is not None:
            arg_count = int(match.group(2)) if match.group(2) is not None else 0
            return "macro", match.group(1), arg_count
    return None


def _is_doc_symbol_name(name: str, *, include_private: bool = False) -> bool:
    if not name:
        return False
    if not include_private and name.startswith("__"):
        return False
    return True


def _collect_leading_doc_comments(lines: Sequence[str], def_index: int, name: str) -> Tuple[str, str]:
    comments: List[str] = []
    stack_effect = ""

    idx = def_index - 1
    while idx >= 0:
        raw = lines[idx]
        stripped = raw.strip()
        if not stripped:
            break
        if not stripped.startswith("#"):
            break

        parsed = _extract_stack_comment(raw)
        if parsed is not None:
            comment_name, effect = parsed
            if (comment_name is None or comment_name == name) and not stack_effect:
                stack_effect = effect
            idx -= 1
            continue

        text = stripped[1:].strip()
        if text:
            comments.append(text)
        idx -= 1

    comments.reverse()
    return stack_effect, " ".join(comments)


def _parse_stack_effect_counts(effect: str) -> Tuple[int, int]:
    """Count fixed input/output items, using -1 for variable or unknown arity."""
    if not effect or "->" not in effect:
        return (-1, -1)
    main = effect.split("||", 1)[0].strip()
    parts = main.split("->", 1)
    if len(parts) != 2:
        return (-1, -1)

    def count_items(side: str) -> int:
        if "..." in side:
            return -1
        side = side.strip()
        if side.startswith("["):
            side = side[1:]
        if side.endswith("]"):
            side = side[:-1]
        if not side.strip():
            return 0
        return sum(
            1
            for item in side.replace("|", ",").split(",")
            if item.strip() and item.strip() != "*"
        )

    return count_items(parts[0]), count_items(parts[1])


def _scan_doc_file(
    path: Path,
    *,
    include_undocumented: bool = False,
    include_private: bool = False,
    include_macros: bool = False,
) -> List[DocEntry]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    lines = text.splitlines()
    entries: List[DocEntry] = []
    defined_names: Set[str] = set()

    for idx, line in enumerate(lines):
        parsed = _extract_definition_name(line, include_macros=include_macros)
        if parsed is None:
            continue
        kind, name, macro_args = parsed
        if not _is_doc_symbol_name(name, include_private=include_private):
            continue
        defined_names.add(name)
        stack_effect, description = _collect_leading_doc_comments(lines, idx, name)
        if not stack_effect and "#" in line:
            inline_comment = line[line.find("#"):]
            parsed_effect = _extract_stack_comment(inline_comment)
            if parsed_effect is not None:
                comment_name, effect = parsed_effect
                if comment_name is None or comment_name == name:
                    stack_effect = effect
        # Auto-generate stack effect for macros from arg count
        if kind == "macro" and not stack_effect:
            if macro_args > 0:
                params = " ".join(f"${i}" for i in range(macro_args))
                stack_effect = f"macro({macro_args}): {params} -> expanded"
            else:
                stack_effect = "macro(0): -> expanded"
        if not include_undocumented and not stack_effect and not description:
            continue
        entries.append(
            DocEntry(
                name=name,
                stack_effect=stack_effect,
                description=description,
                kind=kind,
                path=path,
                line=idx + 1,
            )
        )

    return entries


def _iter_doc_files(
    roots: Sequence[Path],
    *,
    include_tests: bool = False,
    include_libs: bool = False,
) -> List[Path]:
    seen: Set[Path] = set()
    files: List[Path] = []
    skip_parts = {"build", ".git", ".venv", "raylib-5.5_linux_amd64"}
    if not include_libs:
        skip_parts.add("libs")
    if not include_tests:
        skip_parts.update({"tests", "extra_tests"})

    def _should_skip(candidate: Path) -> bool:
        parts = set(candidate.parts)
        return any(part in parts for part in skip_parts)

    for root in roots:
        resolved = root.expanduser().resolve()
        if not resolved.exists():
            continue
        if resolved.is_file() and resolved.suffix == ".sl":
            if _should_skip(resolved):
                continue
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
            continue
        if not resolved.is_dir():
            continue
        root_files: List[Path] = []
        # Skipped directories (matched below the root) are pruned from the
        # walk: rglob would descend into each of them (build/, .git, ...)
        # and drop their files afterwards, which from a large working
        # directory walks a whole tree.
        for dirpath, dirnames, filenames in os.walk(resolved):
            dirnames[:] = [name for name in dirnames if name not in skip_parts]
            for filename in filenames:
                if filename.endswith(".sl"):
                    root_files.append(Path(dirpath, filename).resolve())
        root_files.sort()
        for candidate in root_files:
            if candidate in seen:
                continue
            seen.add(candidate)
            files.append(candidate)
    return files


def collect_docs(
    roots: Sequence[Path],
    *,
    include_undocumented: bool = False,
    include_private: bool = False,
    include_macros: bool = False,
    include_tests: bool = False,
    include_libs: bool = False,
    files: Optional[Sequence[Path]] = None,
) -> List[DocEntry]:
    entries: List[DocEntry] = []
    if files is None:
        files = _iter_doc_files(roots, include_tests=include_tests, include_libs=include_libs)
    for doc_file in files:
        entries.extend(
            _scan_doc_file(
                doc_file,
                include_undocumented=include_undocumented,
                include_private=include_private,
                include_macros=include_macros,
            )
        )
    # Deduplicate by symbol name; keep first (roots/files are stable-sorted)
    dedup: Dict[str, DocEntry] = {}
    for entry in entries:
        dedup.setdefault(entry.name, entry)
    entries = list(dedup.values())
    entries.sort(key=lambda item: (item.name.lower(), str(item.path), item.line))
    return entries


def _filter_docs(entries: Sequence[DocEntry], query: str) -> List[DocEntry]:
    q = query.strip().lower()
    if not q:
        return list(entries)

    try:
        import shlex
        raw_terms = [term.lower() for term in shlex.split(q) if term]
    except Exception:
        raw_terms = [term.lower() for term in q.split() if term]
    terms = raw_terms
    if not terms:
        return list(entries)

    positive_terms: List[str] = []
    negative_terms: List[str] = []
    field_terms: Dict[str, List[str]] = {"name": [], "effect": [], "desc": [], "path": [], "kind": []}
    for term in terms:
        if term.startswith("-") and len(term) > 1:
            negative_terms.append(term[1:])
            continue
        if ":" in term:
            prefix, value = term.split(":", 1)
            if prefix in field_terms and value:
                field_terms[prefix].append(value)
                continue
        positive_terms.append(term)

    ranked: List[Tuple[int, DocEntry]] = []
    for entry in entries:
        name = entry.name.lower()
        effect = entry.stack_effect.lower()
        desc = entry.description.lower()
        path_text = entry.path.as_posix().lower()
        kind = entry.kind.lower()
        all_text = " ".join([name, effect, desc, path_text, kind])

        if any(term in all_text for term in negative_terms):
            continue

        if any(term not in name for term in field_terms["name"]):
            continue
        if any(term not in effect for term in field_terms["effect"]):
            continue
        if any(term not in desc for term in field_terms["desc"]):
            continue
        if any(term not in path_text for term in field_terms["path"]):
            continue
        if any(term not in kind for term in field_terms["kind"]):
            continue

        score = 0
        matches_all = True
        for term in positive_terms:
            term_score = 0
            if name == term:
                term_score = 400
            elif name.startswith(term):
                term_score = 220
            elif term in name:
                term_score = 140
            elif term in effect:
                term_score = 100
            elif term in desc:
                term_score = 70
            elif term in path_text:
                term_score = 40
            if term_score == 0:
                matches_all = False
                break
            score += term_score

        if not matches_all:
            continue
        if len(positive_terms) == 1 and positive_terms[0] in effect and positive_terms[0] not in name:
            score -= 5
        if field_terms["name"]:
            score += 60
        if field_terms["kind"]:
            score += 20
        ranked.append((score, entry))

    ranked.sort(key=lambda item: (-item[0], item[1].name.lower(), str(item[1].path), item[1].line))
    return [entry for _, entry in ranked]


def _run_docs_tui(
    entries: Sequence[DocEntry],
    initial_query: str = "",
    *,
    reload_fn: Optional[Callable[..., List[DocEntry]]] = None,
) -> int:
    if not entries:
        print("[info] no documentation entries found")
        return 0

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        filtered = _filter_docs(entries, initial_query)
        print(f"[info] docs entries: {len(filtered)}/{len(entries)}")
        for entry in filtered[:200]:
            effect = entry.stack_effect if entry.stack_effect else "(no stack effect)"
            print(f"{entry.name:24} {effect}  [{entry.path}:{entry.line}]")
        if len(filtered) > 200:
            print(f"[info] ... {len(filtered) - 200} more entries")
        return 0

    import curses

    _MODE_BROWSE = 0
    _MODE_SEARCH = 1
    _MODE_DETAIL = 2
    _MODE_FILTER = 3
    _MODE_LANG_REF = 4
    _MODE_LANG_DETAIL = 5
    _MODE_LICENSE = 6
    _MODE_PHILOSOPHY = 7
    _MODE_CT_REF = 8
    _MODE_QA = 9
    _MODE_HOW = 10
    _MODE_CT_REF_SEARCH = 11
    _MODE_CT_REF_FILTER = 12
    _MODE_CT_REF_RESULTS = 13
    _MODE_CT_REF_DETAIL = 14

    _TAB_LIBRARY = 0
    _TAB_LANG_REF = 1
    _TAB_CT_REF = 2
    _TAB_NAMES = ["Library Docs", "Language Reference", "Compile-Time Reference"]

    _FILTER_KINDS = ["all", "word", "asm", "py", "macro"]

    _L2_MACRO_CANONICAL_TEXT = (
        "A macro is an L2 program run at compile time:\n"
        "  macro <name> [(<params...>)] <body...> ;\n"
        "The body ends at `;` at its own top level (not inside a quotation,\n"
        "a match pattern, or a nested block). A call `name a b` consumes the\n"
        "macro name and exactly as many input values as it has parameters:\n"
        "each value is one token, or a whole backtick quotation. Arguments are\n"
        "taken as written, not evaluated or expanded; commas are ordinary\n"
        "tokens. Too few values before end of input is\n"
        "macro-insufficient-arguments and consumes none of them.\n\n"
        "Parameters are bindings: in the body `$name` pushes the value (a Token\n"
        "or Syntax) and `$name` inside a quotation splices it. The body runs on\n"
        "its own CT data stack. `emit` appends Syntax to the expansion's output,\n"
        "which is inserted only when the body succeeds; a body with no emit\n"
        "expands to nothing. A macro named in another macro's body is called\n"
        "directly: it reads its arguments from the input and its output joins\n"
        "the caller's. `match` dispatches on the following input. A body that\n"
        "is only a quotation followed by emit is expanded by direct splicing.\n"
        "At compile time `emit` is always the built-in: a user word named\n"
        "emit is used only by runtime code.\n\n"
        "Example:\n"
        "  macro add (x y) `$x $y +` emit ;\n"
        "  add 20 22        # becomes 20 22 +\n\n"
        "Constants: `const <name> <value>` is the short form of\n"
        "`macro <name> `<value>` emit ;` for one token (a number, character,\n"
        "string, or word). It is a top-level form; `priority N const ...`\n"
        "works. The macro is created when the name is first used, so unused\n"
        "constants cost almost nothing.\n"
        "  const SYS_WRITE 1"
    )
    _L2_MACRO_LANG_DETAIL = (
        _L2_MACRO_CANONICAL_TEXT
        + "\n\n"
        + "CT logic and match:\n"
        + "  macro zero_to_one (x)\n"
        + "    $x token-lexeme \"0\" string-equal? if `1` emit else `$x` emit end\n"
        + "  ;\n"
        + "  macro simplify\n"
        + "    match\n"
        + "      [$x:int, +, 0] => `$x` emit\n"
        + "      [_] =>\n"
        + "    end\n"
        + "  ;"
    )
    _L2_MACRO_CT_BLOCK = textwrap.indent(_L2_MACRO_CANONICAL_TEXT, "  ") + "\n\n"
    _L2_MACRO_QA_BLOCK = textwrap.indent(_L2_MACRO_CANONICAL_TEXT, "    ") + "\n\n"

    # ── Language Reference Entries ──────────────────────────────────
    _LANG_REF_ENTRIES: List[Dict[str, str]] = [
        {
            "name": "word ... end",
            "category": "Definitions",
            "syntax": "word <name> <body...> end",
            "summary": "Define a new word (function).",
            "detail": (
                "Defines a named word that can be called by other words. "
                "The body consists of stack operations, literals, and calls to other words. "
                "Defining a name again keeps the earlier definition as a candidate; the "
                "highest priority wins and the newest wins a tie (a same-priority redefinition "
                "warns).\n\n"
                "Example:\n"
                "  word square dup * end\n"
                "  word greet \"hello world\" puts end"
            ),
        },
        {
            "name": "inline word ... end",
            "category": "Definitions",
            "syntax": "inline [preserve] word <name> <body...> end",
            "summary": "Define an inlined word (body is expanded at call sites).",
            "detail": (
                "Marks the definition for inline expansion. "
                "Every call site gets a copy of the body rather than a function call. "
                "By default, the standalone definition is not emitted. Add `preserve` "
                "immediately after `inline` to keep it in the output as well. "
                "Recursive inline calls are rejected at compile time.\n\n"
                "Example:\n"
                "  inline word inc 1 + end"
            ),
        },
        {
            "name": "preserve",
            "category": "Definitions",
            "syntax": "preserve <definition>",
            "summary": "Keep a definition emitted as a standalone word.",
            "detail": (
                "Use `preserve` before a `word` or `:asm` definition to retain the "
                "standalone definition, even when it has no runtime callers. It works "
                "with or without `inline`; `inline preserve` also expands native uses "
                "while retaining the original definition. This is useful when taking "
                "a word pointer or exposing a symbol.\n\n"
                "Examples:\n"
                "  preserve word inc 1 + end\n"
                "  inline preserve word inc 1 + end\n"
                "  inline preserve :asm fast_inc { inc qword [r12] } ;"
            ),
        },
        {
            "name": ":asm ... ;",
            "category": "Definitions",
            "syntax": ":asm <name> { <nasm body> } ;",
            "summary": "Define a word in raw NASM x86-64 assembly.",
            "detail": (
                "The body is copied verbatim into the output assembly. "
                "r12 = data stack pointer, r13 = return stack pointer. "
                "Values are 64-bit qwords. An implicit `ret` is appended.\n\n"
                "Example:\n"
                "  :asm double {\n"
                "      mov rax, [r12]\n"
                "      shl rax, 1\n"
                "      mov [r12], rax\n"
                "  } ;"
            ),
        },
        {
            "name": ":py ... ;",
            "category": "Definitions",
            "syntax": ":py <name> { <python body> } ;",
            "summary": "Define a compile-time Python macro or intrinsic.",
            "detail": (
                "The body executes once during parsing. It may define:\n"
                "  - macro(ctx: MacroContext): manipulate tokens, emit literals\n"
                "  - intrinsic(builder: FunctionEmitter): emit assembly directly\n\n"
                "Used by syntax extensions like libs/fn.sl to reshape the language."
            ),
        },
        {
            "name": "extern",
            "category": "Definitions",
            "syntax": "extern <name> <n_args> <n_rets>\nextern <ret_type> <name>(<arg_types>)",
            "summary": "Declare a foreign (C) function.",
            "detail": (
                "Two forms:\n"
                "  Raw:    extern foo 2 1     (2 args, 1 return)\n"
                "  C-like: extern double atan2(double y, double x)\n\n"
                "The emitter marshals arguments into System V registers "
                "(rdi, rsi, rdx, rcx, r8, r9 for ints; xmm0-xmm7 for floats), "
                "aligns rsp, and pushes the result from rax or xmm0."
            ),
        },
        {
            "name": "macro ... ;",
            "category": "Definitions",
            "syntax": (
                "macro <name> <param_count> <tokens...> ;\n"
                "macro <name> (<params...>) <tokens...> ;\n"
                "macro <name>\n  <pattern...> => <replacement...> ;\n  ...\n;"
            ),
            "summary": "Define substitution macros and pattern-matching macros.",
            "detail": _L2_MACRO_LANG_DETAIL,
        },
        {
            "name": "struct ... end",
            "category": "Definitions",
            "syntax": "struct <Name>\n  field <field> <size>\n  ...\nend",
            "summary": "Define a packed struct with auto-generated accessors.",
            "detail": (
                "Emits helper words:\n"
                "  <Name>.size         — total byte size\n"
                "  <Name>.<field>.size   — field byte size\n"
                "  <Name>.<field>.offset — field byte offset\n"
                "  <Name>.<field>@     — read field from struct pointer\n"
                "  <Name>.<field>!     — write field to struct pointer\n\n"
                "Layout is tightly packed with no implicit padding.\n\n"
                "Example:\n"
                "  struct Point\n"
                "    field x 8\n"
                "    field y 8\n"
                "  end\n"
                "  # Now Point.x@, Point.x!, Point.y@, Point.y! exist"
            ),
        },
        {
            "name": "cstruct ... end",
            "category": "Definitions",
            "syntax": "cstruct <Name>\n  cfield <field> <c_type>\n  ...\nend",
            "summary": "Define a C-compatible struct with ABI-aligned layout.",
            "detail": (
                "Computes offsets, alignment, and final struct size using C ABI rules. "
                "Generates <Name>.size, <Name>.align, <Name>.<field>.size, and "
                "<Name>.<field>.offset for every field. Accessors @/! are generated "
                "for 8-byte fields.\n\n"
                "Example:\n"
                "  cstruct Pair\n"
                "    cfield left long\n"
                "    cfield right long\n"
                "  end"
            ),
        },
        {
            "name": "if ... end",
            "category": "Control Flow",
            "syntax": "<cond> if <body> end\n<cond> if <then> else <otherwise> end",
            "summary": "Conditional execution — pops a flag from the stack.",
            "detail": (
                "Pops the top of stack. If non-zero, executes the `then` branch; "
                "otherwise executes the `else` branch (if present).\n\n"
                "For else-if chains, place `if` on the same line as `else` "
                "(backward-compatible style):\n"
                "  <cond1> if\n"
                "    ... branch 1 ...\n"
                "  else <cond2> if\n"
                "    ... branch 2 ...\n"
                "  else\n"
                "    ... fallback ...\n"
                "  end\n\n"
                "The parser also accepts flexible shorthand where chained if/else "
                "blocks may close with fewer explicit `end` tokens; omitted trailing "
                "if/else closes are resolved automatically.\n\n"
                "Example:\n"
                "  dup 0 > if \"positive\" puts else \"non-positive\" puts end"
            ),
        },
        {
            "name": "while ... do ... end",
            "category": "Control Flow",
            "syntax": "while <condition> do <body> end",
            "summary": "Loop while condition is true.",
            "detail": (
                "The condition block runs before each iteration. It must leave "
                "a flag on the stack. If non-zero, the body executes and the loop "
                "repeats. If zero, execution continues after `end`.\n\n"
                "Example:\n"
                "  10\n"
                "  while dup 0 > do\n"
                "    dup puti cr\n"
                "    1 -\n"
                "  end\n"
                "  drop"
            ),
        },
        {
            "name": "for ... end",
            "category": "Control Flow",
            "syntax": "<count> for <body> end",
            "summary": "Counted loop — pops count, loops that many times.",
            "detail": (
                "Pops the loop count from the stack, stores it on the return stack, "
                "and decrements it each pass. Use `r@` (return "
                "stack peek) to read the current counter value.\n\n"
                "Example:\n"
                "  10 for\n"
                "    \"hello\" puts\n"
                "  end\n\n"
                "  # prints \"hello\" 10 times"
            ),
        },
        {
            "name": "begin ... again",
            "category": "Control Flow",
            "syntax": "begin <body> again",
            "summary": "Unconditional loop; leave it with `break`.",
            "detail": (
                "Repeats the body forever, at compile time and in runtime code. "
                "`again` (not `end`) closes it. Leave it with `break`, or return "
                "from the word with `exit`.\n\n"
                "Example:\n"
                "  begin\n"
                "    read_stdin\n"
                "    dup 0 == if drop break end\n"
                "    process\n"
                "  again"
            ),
        },
        {
            "name": "break",
            "category": "Control Flow",
            "syntax": "break",
            "summary": "Leave the innermost enclosing loop.",
            "detail": (
                "Works in `for`, `while ... do ... end`, and `begin ... again`, "
                "at compile time and in runtime code. Only the innermost loop is "
                "left. Leaving a `for` loop drops its counter, so the return stack "
                "and the compile-time loop index `i` stay consistent. Using it "
                "outside a loop is a parse error "
                "(parser-loop-control-outside-loop).\n\n"
                "Example:\n"
                "  0 10 for 1 + dup 4 == if break end end   # leaves 4"
            ),
        },
        {
            "name": "continue",
            "category": "Control Flow",
            "syntax": "continue",
            "summary": "Start the next iteration of the innermost loop.",
            "detail": (
                "Works in the same loops as `break`. In a `for` loop it counts the "
                "iteration (like reaching `end`); in `while` it re-tests the "
                "condition; in `begin ... again` it jumps to `begin`. Using it "
                "outside a loop is a parse error."
            ),
        },
        {
            "name": "match",
            "category": "Control Flow",
            "syntax": "match [pattern] => <body> ... [_] => <body> end",
            "summary": "Compile-time dispatch on the upcoming input tokens.",
            "detail": (
                "Tests each clause's pattern against the next input values "
                "without consuming them; the first match in source order is "
                "consumed and its body runs. A value is a Token or a whole "
                "backtick quotation. Pattern elements, separated by commas: a "
                "literal token, `*` (any one value), `$name` or `$name:type` "
                "(binds one value; types any, ident, int, float, number, string, "
                "char, literal, syntax), and `\\x` for the literal x. A repeated "
                "`$name` must see an equal value. `[_]` is the optional default "
                "and must be last; an empty default passes the input through. "
                "Bindings are values in the clause body (`$name` pushes it) and "
                "splice into quotations. Without a match and without a default "
                "it is match-no-clause. Compile-time only.\n\n"
                "Example:\n"
                "  word add_zero\n"
                "    match\n"
                "      [0, +, $x:int] => `$x` emit\n"
                "      [_] =>\n"
                "    end\n"
                "  end immediate compile-only"
            ),
        },
        {
            "name": "label / goto",
            "category": "Control Flow",
            "syntax": "label <name>\ngoto <name>",
            "summary": "Local jumps within a definition.",
            "detail": (
                "Defines a local label and jumps to it. "
                "Labels are scoped to the enclosing word definition.\n\n"
                "Example:\n"
                "  word example\n"
                "    label start\n"
                "    dup 0 == if drop exit end\n"
                "    1 - goto start\n"
                "  end"
            ),
        },
        {
            "name": "&name",
            "category": "Control Flow",
            "syntax": "&<word_name>",
            "summary": "Push pointer to a word's code label.",
            "detail": (
                "Pushes the callable address of the named word onto the stack. "
                "Combine with `jmp` for indirect/tail calls.\n\n"
                "Example:\n"
                "  &my_handler jmp   # tail-call my_handler"
            ),
        },
        {
            "name": "with ... in ... end",
            "category": "Control Flow",
            "syntax": "with <a> <b> in <body> end",
            "summary": "Local variable scope using hidden globals.",
            "detail": (
                "Pops the named values from the stack and stores them in hidden "
                "global cells (__with_a, etc.). Inside the body, reading `a` "
                "compiles to `@`, writing compiles to `!`. The cells persist "
                "across calls and are NOT re-entrant.\n\n"
                "Example:\n"
                "  10 20 with x y in\n"
                "    x y + puti cr   # prints 30\n"
                "  end"
            ),
        },
        {
            "name": "import",
            "category": "Modules",
            "syntax": "import <path>",
            "summary": "Textually include another .sl file.",
            "detail": (
                "Inserts the referenced file. Resolution order:\n"
                "  1. Absolute path\n"
                "  2. Relative to the importing file\n"
                "  3. Each include path (defaults: project root, ./stdlib)\n\n"
                "Each file is included at most once per compilation unit."
            ),
        },
        {
            "name": "flags",
            "category": "Modules",
            "syntax": "flags \"<cli flags>\"",
            "summary": "Provide compiler flags from source.",
            "detail": (
                "Processed during source loading before tokenization. "
                "Supports shell-like token splitting. "
                "`-I`/`--include` update import search paths (relative to the "
                "current file when not absolute). The quoted string is replayed "
                "through the normal CLI parser, so source pragmas can set the "
                "same compiler options you would pass on the command line.\n\n"
                "Examples:\n"
                "  flags \"-lc -lm -L. -I.\"\n"
                "  flags \"-lc -lm -L. -I.\""
            ),
        },
        {
            "name": "cimport",
            "category": "Modules",
            "syntax": "cimport \"header.h\"",
            "summary": "Import C declarations and auto-generate extern/cstruct forms.",
            "detail": (
                "Reads a C header, preprocesses it, then injects generated `extern` "
                "declarations and `cstruct` definitions into the token stream. "
                "Resolution follows normal import search rules."
            ),
        },
        {
            "name": "ifdef / ifndef / elsedef / endif",
            "category": "Modules",
            "syntax": "ifdef <NAME> ... elsedef ... endif\nifndef <NAME> ... elsedef ... endif",
            "summary": "Conditional source inclusion based on -D symbols.",
            "detail": (
                "Source preprocessing evaluates these directives before tokenization. "
                "Symbols may be defined via CLI `-D NAME` and source-level "
                "`define NAME`. `elsedef` flips the current branch. "
                "Nested conditionals are supported."
            ),
        },
        {
            "name": "[ ... ]",
            "category": "Data",
            "syntax": "[ <values...> ]",
            "summary": "Heap list literal — captures stack segment into mmap'd buffer.",
            "detail": (
                "Captures the intervening stack values into a freshly allocated "
                "buffer. Format: [len, item0, item1, ...] as qwords. "
                "The buffer address is pushed. User must `munmap` when done.\n\n"
                "Example:\n"
                "  [ 1 2 3 4 5 ]   # pushes addr of [5, 1, 2, 3, 4, 5]"
            ),
        },
        {
            "name": "{ ... } and { ... }:N",
            "category": "Data",
            "syntax": "{ <int-or-char...> } | { <int-or-char...> }:<size> | {}:<size>",
            "summary": "BSS-backed fixed-size list literal.",
            "detail": (
                "Allocates a fixed-size qword list in .bss and pushes its address. "
                "Layout is [len, item0, item1, ...]. "
                "With :N, len is forced to N and any missing trailing elements are "
                "zero-initialized.\n\n"
                "Examples:\n"
                "  { 1 2 3 }      # len=3, items=1,2,3\n"
                "  { 1 2 }:10     # len=10, items=1,2, then zeros\n"
                "  {}:10          # len=10, all zeros"
            ),
        },
        {
            "name": "String literals",
            "category": "Data",
            "syntax": "\"<text>\"",
            "summary": "Push (addr len) pair for a string.",
            "detail": (
                "String literals push a (addr len) pair with length on top. "
                "Stored in .data with a trailing NULL for C compatibility. "
                "Escape sequences: \\\", \\\\, \\n, \\r, \\t, \\0.\n\n"
                "Example:\n"
                "  \"hello world\" puts   # prints: hello world"
            ),
        },
        {
            "name": "Char literals",
            "category": "Data",
            "syntax": "'<ch>'",
            "summary": "Push character code as integer.",
            "detail": (
                "Single-quoted literals push an integer character code. "
                "Supported escapes: \\n, \\r, \\t, \\0, \\\\, \\', \", \\xNN.\n\n"
                "Example:\n"
                "  'A' puti cr    # prints 65"
            ),
        },
        {
            "name": "Number literals",
            "category": "Data",
            "syntax": "123  0xFF  0b1010  0o77",
            "summary": "Push a signed 64-bit integer.",
            "detail": (
                "Numbers are signed 64-bit integers. Supports:\n"
                "  Decimal:  123, -42\n"
                "  Hex:      0xFF, 0x1A\n"
                "  Binary:   0b1010, 0b11110000\n"
                "  Octal:    0o77, 0o755\n"
                "  Float:    3.14, 1e10 (stored as 64-bit IEEE double)"
            ),
        },
        {
            "name": "immediate",
            "category": "Modifiers",
            "syntax": "immediate",
            "summary": "Mark the last-defined word to execute at parse time.",
            "detail": (
                "Applied to the most recently defined word. Immediate words "
                "run during parsing rather than being compiled into the output. "
                "Used for syntax extensions and compile-time computation."
            ),
        },
        {
            "name": "compile-only",
            "category": "Modifiers",
            "syntax": "compile-only",
            "summary": "Mark the last-defined word as compile-only.",
            "detail": (
                "The word can only be used inside other definitions, not at "
                "the top level. Often combined with `immediate`."
            ),
        },
        {
            "name": "priority",
            "category": "Modifiers",
            "syntax": "priority <int>",
            "summary": "Set priority for the next definition (conflict resolution).",
            "detail": (
                "Controls redefinition conflicts. Higher priority wins; "
                "lower-priority definitions are silently ignored. Equal priority "
                "keeps the last definition with a warning."
            ),
        },
        {
            "name": "compile-time",
            "category": "Modifiers",
            "syntax": "compile-time <word>",
            "summary": "Execute the named word immediately during compilation.",
            "detail": (
                "Invokes the named word immediately in the CT VM. If used inside "
                "another word definition, the compiler also emits a call to the "
                "named word in that definition's body; the enclosing definition's "
                "flags determine whether its body is emitted for runtime. At top "
                "level, only the compile-time invocation occurs. "
                "The target must already be defined and cannot be runtime-only or "
                "compile-only."
            ),
        },
        {
            "name": "here",
            "category": "Modifiers",
            "syntax": "here",
            "summary": "Push current source location string.",
            "detail": (
                "Immediate word that pushes `file:line:column` for the current parse "
                "location as a string literal. Useful for diagnostics and assertions."
            ),
        },
        {
            "name": "on-event",
            "category": "Events",
            "syntax": "word <listener> <body> end on-event <pattern>",
            "summary": "Register a compile-time word as a compiler event listener.",
            "detail": (
                "Use `on-event` as a suffix after a word definition. The word is "
                "marked compile-only and called for matching compiler events; "
                "patterns support shell-style globs such as `word.compile.*`. "
                "The handler gets its own copy of the event on an otherwise empty "
                "stack and must consume it (stack effect `event --`). Handler "
                "errors stop compilation. This is compile-time only.\n\n"
                "Example:\n"
                "  word inspect_compile_event\n"
                "    event-name \"compile\" string-equal? static-assert\n"
                "  end on-event compile"
            ),
        },
        {
            "name": "emit-event",
            "category": "Events",
            "syntax": "emit-event <name>",
            "summary": "Publish a named event during compilation.",
            "detail": (
                "Publishes a user event whose payload holds the directive's "
                "SourceOrigin at key `source-origin`. For structured payloads use "
                "`event-new`, `event-payload-set`, and `ct-event-emit-object`. Events are available to compile-time "
                "listeners and external compiler tooling.\n\n"
                "Example:\n"
                "  emit-event build.started"
            ),
        },
        {
            "name": "syscall",
            "category": "System",
            "syntax": "<argN> ... <arg0> <count> <nr> syscall",
            "summary": "Invoke a Linux system call directly.",
            "detail": (
                "Expects (argN ... arg0 count nr) on the stack. Count is "
                "clamped to [0,6]. Arguments are loaded into rdi, rsi, rdx, r10, "
                "r8, r9. Executes `syscall` and pushes rax.\n\n"
                "Example:\n"
                "  # write(1, addr, len)\n"
                "  addr len 1   # fd=stdout\n"
                "  3 1 syscall  # 3 args, nr=1 (write)"
            ),
        },
        {
            "name": "ret",
            "category": "Control Flow",
            "syntax": "ret",
            "summary": "Return from a word",
            "detail": (
                "Returns from a word.\n\n"
                "Example:\n"
                "  word a\n"
                "    \"g\" puts\n"
                "    ret\n"
                "    \"g\" puts\n"
                "  end\n\n"
                "  word main\n"
                "    a\n"
                "  end\n"
                "Output:\n"
                "  g\n"
            ),
        },
        {
            "name": "exit",
            "category": "System",
            "syntax": "<code> exit",
            "summary": "Terminate the process with given exit code.",
            "detail": (
                "Pops the exit code and terminates via sys_exit_group(231). "
                "Convention: 0 = success, non-zero = failure.\n\n"
                "Example:\n"
                "  0 exit   # success"
            ),
        },
    ]

    _LANG_REF_CATEGORIES = []
    _cat_seen: set = set()
    for _lre in _LANG_REF_ENTRIES:
        if _lre["category"] not in _cat_seen:
            _cat_seen.add(_lre["category"])
            _LANG_REF_CATEGORIES.append(_lre["category"])

    _L2_LICENSE_TEXT = (
        "═══════════════════════════════════════════════════════════════\n"
        "          Apache License, Version 2.0\n"
        "          January 2004\n"
        "          http://www.apache.org/licenses/\n"
        "═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION\n"
        "\n"
        "  1. Definitions.\n"
        "\n"
        "  \"License\" shall mean the terms and conditions for use,\n"
        "  reproduction, and distribution as defined by Sections 1\n"
        "  through 9 of this document.\n"
        "\n"
        "  \"Licensor\" shall mean the copyright owner or entity\n"
        "  authorized by the copyright owner that is granting the\n"
        "  License.\n"
        "\n"
        "  \"Legal Entity\" shall mean the union of the acting entity\n"
        "  and all other entities that control, are controlled by,\n"
        "  or are under common control with that entity. For the\n"
        "  purposes of this definition, \"control\" means (i) the\n"
        "  power, direct or indirect, to cause the direction or\n"
        "  management of such entity, whether by contract or\n"
        "  otherwise, or (ii) ownership of fifty percent (50%) or\n"
        "  more of the outstanding shares, or (iii) beneficial\n"
        "  ownership of such entity.\n"
        "\n"
        "  \"You\" (or \"Your\") shall mean an individual or Legal\n"
        "  Entity exercising permissions granted by this License.\n"
        "\n"
        "  \"Source\" form shall mean the preferred form for making\n"
        "  modifications, including but not limited to software\n"
        "  source code, documentation source, and configuration\n"
        "  files.\n"
        "\n"
        "  \"Object\" form shall mean any form resulting from\n"
        "  mechanical transformation or translation of a Source\n"
        "  form, including but not limited to compiled object code,\n"
        "  generated documentation, and conversions to other media\n"
        "  types.\n"
        "\n"
        "  \"Work\" shall mean the work of authorship, whether in\n"
        "  Source or Object form, made available under the License,\n"
        "  as indicated by a copyright notice that is included in\n"
        "  or attached to the work.\n"
        "\n"
        "  \"Derivative Works\" shall mean any work, whether in\n"
        "  Source or Object form, that is based on (or derived\n"
        "  from) the Work and for which the editorial revisions,\n"
        "  annotations, elaborations, or other modifications\n"
        "  represent, as a whole, an original work of authorship.\n"
        "\n"
        "  \"Contribution\" shall mean any work of authorship,\n"
        "  including the original version of the Work and any\n"
        "  modifications or additions to that Work or Derivative\n"
        "  Works thereof, that is intentionally submitted to the\n"
        "  Licensor for inclusion in the Work by the copyright\n"
        "  owner or by an individual or Legal Entity authorized to\n"
        "  submit on behalf of the copyright owner.\n"
        "\n"
        "  \"Contributor\" shall mean Licensor and any individual or\n"
        "  Legal Entity on behalf of whom a Contribution has been\n"
        "  received by the Licensor and subsequently incorporated\n"
        "  within the Work.\n"
        "\n"
        "  2. Grant of Copyright License.\n"
        "\n"
        "  Subject to the terms and conditions of this License,\n"
        "  each Contributor hereby grants to You a perpetual,\n"
        "  worldwide, non-exclusive, no-charge, royalty-free,\n"
        "  irrevocable copyright license to reproduce, prepare\n"
        "  Derivative Works of, publicly display, publicly perform,\n"
        "  sublicense, and distribute the Work and such Derivative\n"
        "  Works in Source or Object form.\n"
        "\n"
        "  3. Grant of Patent License.\n"
        "\n"
        "  Subject to the terms and conditions of this License,\n"
        "  each Contributor hereby grants to You a perpetual,\n"
        "  worldwide, non-exclusive, no-charge, royalty-free,\n"
        "  irrevocable (except as stated in this section) patent\n"
        "  license to make, have made, use, offer to sell, sell,\n"
        "  import, and otherwise transfer the Work, where such\n"
        "  license applies only to those patent claims licensable\n"
        "  by such Contributor that are necessarily infringed by\n"
        "  their Contribution(s) alone or by combination of their\n"
        "  Contribution(s) with the Work to which such\n"
        "  Contribution(s) was submitted.\n"
        "\n"
        "  If You institute patent litigation against any entity\n"
        "  (including a cross-claim or counterclaim in a lawsuit)\n"
        "  alleging that the Work or a Contribution incorporated\n"
        "  within the Work constitutes direct or contributory\n"
        "  patent infringement, then any patent licenses granted\n"
        "  to You under this License for that Work shall terminate\n"
        "  as of the date such litigation is filed.\n"
        "\n"
        "  4. Redistribution.\n"
        "\n"
        "  You may reproduce and distribute copies of the Work or\n"
        "  Derivative Works thereof in any medium, with or without\n"
        "  modifications, and in Source or Object form, provided\n"
        "  that You meet the following conditions:\n"
        "\n"
        "  (a) You must give any other recipients of the Work or\n"
        "      Derivative Works a copy of this License; and\n"
        "\n"
        "  (b) You must cause any modified files to carry prominent\n"
        "      notices stating that You changed the files; and\n"
        "\n"
        "  (c) You must retain, in the Source form of any Derivative\n"
        "      Works that You distribute, all copyright, patent,\n"
        "      trademark, and attribution notices from the Source\n"
        "      form of the Work, excluding those notices that do\n"
        "      not pertain to any part of the Derivative Works; and\n"
        "\n"
        "  (d) If the Work includes a \"NOTICE\" text file as part\n"
        "      of its distribution, then any Derivative Works that\n"
        "      You distribute must include a readable copy of the\n"
        "      attribution notices contained within such NOTICE\n"
        "      file, excluding any notices that do not pertain to\n"
        "      any part of the Derivative Works, in at least one\n"
        "      of the following places: within a NOTICE text file\n"
        "      distributed as part of the Derivative Works; within\n"
        "      the Source form or documentation, if provided along\n"
        "      with the Derivative Works; or, within a display\n"
        "      generated by the Derivative Works, if and wherever\n"
        "      such third-party notices normally appear.\n"
        "\n"
        "  5. Submission of Contributions.\n"
        "\n"
        "  Unless You explicitly state otherwise, any Contribution\n"
        "  intentionally submitted for inclusion in the Work by You\n"
        "  to the Licensor shall be under the terms and conditions\n"
        "  of this License, without any additional terms or\n"
        "  conditions. Notwithstanding the above, nothing herein\n"
        "  shall supersede or modify the terms of any separate\n"
        "  license agreement you may have executed with Licensor\n"
        "  regarding such Contributions.\n"
        "\n"
        "  6. Trademarks.\n"
        "\n"
        "  This License does not grant permission to use the trade\n"
        "  names, trademarks, service marks, or product names of\n"
        "  the Licensor, except as required for reasonable and\n"
        "  customary use in describing the origin of the Work and\n"
        "  reproducing the content of the NOTICE file.\n"
        "\n"
        "  7. Disclaimer of Warranty.\n"
        "\n"
        "  Unless required by applicable law or agreed to in\n"
        "  writing, Licensor provides the Work (and each\n"
        "  Contributor provides its Contributions) on an \"AS IS\"\n"
        "  BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,\n"
        "  either express or implied, including, without limitation,\n"
        "  any warranties or conditions of TITLE, NON-INFRINGEMENT,\n"
        "  MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE.\n"
        "  You are solely responsible for determining the\n"
        "  appropriateness of using or redistributing the Work and\n"
        "  assume any risks associated with Your exercise of\n"
        "  permissions under this License.\n"
        "\n"
        "  8. Limitation of Liability.\n"
        "\n"
        "  In no event and under no legal theory, whether in tort\n"
        "  (including negligence), contract, or otherwise, unless\n"
        "  required by applicable law (such as deliberate and\n"
        "  grossly negligent acts) or agreed to in writing, shall\n"
        "  any Contributor be liable to You for damages, including\n"
        "  any direct, indirect, special, incidental, or\n"
        "  consequential damages of any character arising as a\n"
        "  result of this License or out of the use or inability\n"
        "  to use the Work (including but not limited to damages\n"
        "  for loss of goodwill, work stoppage, computer failure\n"
        "  or malfunction, or any and all other commercial damages\n"
        "  or losses), even if such Contributor has been advised\n"
        "  of the possibility of such damages.\n"
        "\n"
        "  9. Accepting Warranty or Additional Liability.\n"
        "\n"
        "  While redistributing the Work or Derivative Works\n"
        "  thereof, You may choose to offer, and charge a fee for,\n"
        "  acceptance of support, warranty, indemnity, or other\n"
        "  liability obligations and/or rights consistent with\n"
        "  this License. However, in accepting such obligations,\n"
        "  You may act only on Your own behalf and on Your sole\n"
        "  responsibility, not on behalf of any other Contributor,\n"
        "  and only if You agree to indemnify, defend, and hold\n"
        "  each Contributor harmless for any liability incurred\n"
        "  by, or claims asserted against, such Contributor by\n"
        "  reason of your accepting any such warranty or\n"
        "  additional liability.\n"
        "\n"
        "  END OF TERMS AND CONDITIONS\n"
        "\n"
        "═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  Copyright 2024-2026 Igor Cielniak\n"
        "\n"
        "  Licensed under the Apache License, Version 2.0 (the\n"
        "  \"License\"); you may not use this file except in\n"
        "  compliance with the License. You may obtain a copy at\n"
        "\n"
        "    http://www.apache.org/licenses/LICENSE-2.0\n"
        "\n"
        "  Unless required by applicable law or agreed to in\n"
        "  writing, software distributed under the License is\n"
        "  distributed on an \"AS IS\" BASIS, WITHOUT WARRANTIES\n"
        "  OR CONDITIONS OF ANY KIND, either express or implied.\n"
        "  See the License for the specific language governing\n"
        "  permissions and limitations under the License.\n"
        "\n"
        "═══════════════════════════════════════════════════════════════\n"
    )

    _L2_PHILOSOPHY_TEXT = (
        "═══════════════════════════════════════════════════════════\n"
        "          T H E   P H I L O S O P H Y   O F   L 2\n"
        "═══════════════════════════════════════════════════════════\n"
        "\n"
        "  \"Give the programmer raw power and get out of the way.\"\n"
        "\n"
        "───────────────────────────────────────────────────────────\n"
        "\n"
        "  WHAT IS L2?\n"
        "\n"
        "  At its core, L2 is a programmable assembly templating\n"
        "  engine with a Forth-style stack interface. You write\n"
        "  small 'words' that compose into larger programs, and\n"
        "  each word compiles to a known, inspectable sequence of\n"
        "  x86-64 instructions. The language sits just above raw\n"
        "  assembly — close enough to see every byte, high enough\n"
        "  to be genuinely productive.\n"
        "\n"
        "  But L2 is more than a glorified macro assembler. Its\n"
        "  compile-time virtual machine lets you run arbitrary L2\n"
        "  code at compile time: generate words, compute lookup\n"
        "  tables, build structs, or emit entire subsystems before\n"
        "  a single byte of native code is produced. Text macros,\n"
        "  :py blocks, and token hooks extend the syntax in ways\n"
        "  that feel like language features — because they are.\n"
        "\n"
        "───────────────────────────────────────────────────────────\n"
        "\n"
        "  WHY DOES L2 EXIST?\n"
        "\n"
        "  L2 was built for fun — and that's a feature, not an\n"
        "  excuse. It exists because writing a compiler is deeply\n"
        "  satisfying, because Forth's ideas deserve to be pushed\n"
        "  further, and because sometimes you want to write a\n"
        "  program that does exactly what you told it to.\n"
        "\n"
        "  That said, 'fun' doesn't mean 'toy'. L2 produces real\n"
        "  native binaries, links against C libraries, and handles\n"
        "  practical tasks like file I/O, hashmap manipulation,\n"
        "  and async scheduling — all with a minimal runtime.\n"
        "\n"
        "───────────────────────────────────────────────────────────\n"
        "\n"
        "  CORE TENETS\n"
        "\n"
        "  1. SIMPLICITY OVER CONVENIENCE\n"
        "     No garbage collector, no hidden magic. The compiler\n"
        "     emits a minimal runtime you can read and modify.\n"
        "     You own every allocation and every free.\n"
        "\n"
        "  2. TRANSPARENCY\n"
        "     Every word compiles to a known, inspectable\n"
        "     sequence of x86-64 instructions. --emit-asm\n"
        "     shows exactly what runs on the metal.\n"
        "\n"
        "  3. COMPOSABILITY\n"
        "     Small words build big programs. The stack is the\n"
        "     universal interface — no types to reconcile, no\n"
        "     generics to instantiate. If it fits on the stack,\n"
        "     it composes.\n"
        "\n"
        "  4. META-PROGRAMMABILITY\n"
        "     The front-end is user-extensible: text macros, :py\n"
        "     blocks, immediate words, and token hooks reshape\n"
        "     syntax at compile time. The compile-time VM can\n"
        "     execute full L2 programs during compilation, making\n"
        "     the boundary between 'language' and 'metaprogram'\n"
        "     deliberately blurry.\n"
        "\n"
        "  5. UNSAFE BY DESIGN\n"
        "     Safety is the programmer's job, not the language's.\n"
        "     L2 trusts you with raw memory, inline assembly,\n"
        "     and direct syscalls. This is a feature, not a bug.\n"
        "\n"
        "  6. MINIMAL STANDARD LIBRARY\n"
        "     The stdlib provides building blocks — not policy.\n"
        "     It gives you alloc/free, puts/puti, arrays, and\n"
        "     file I/O. Everything else is your choice.\n"
        "\n"
        "  7. FUN FIRST\n"
        "     If using L2 feels like a chore, the design has\n"
        "     failed. The language should reward curiosity and\n"
        "     make you want to dig deeper into how things work.\n"
        "     At least its fun for me to write programs in. ;)"
        "\n"
        "───────────────────────────────────────────────────────────\n"
        "\n"
        "  L2 is for programmers who want to understand every\n"
        "  byte their program emits, and who believe that the\n"
        "  best abstraction is the one you built yourself.\n"
        "\n"
        "═══════════════════════════════════════════════════════════\n"
    )

    _L2_CT_REF_TEXT = (
        "═══════════════════════════════════════════════════════════════\n"
        "        C O M P I L E - T I M E   R E F E R E N C E\n"
        "═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  L2 runs a compile-time virtual machine (the CT VM) during\n"
        "  parsing. The `compile-time <word>` directive, immediate words, and\n"
        "  :py blocks execute inside this VM. They can inspect and\n"
        "  transform the token stream, emit definitions, manipulate\n"
        "  lists and maps, and control the generated assembly output.\n"
        "\n"
        "  Unless noted otherwise, words listed below are compile-only:\n"
        "  they exist only during compilation and produce no runtime\n"
        "  code.\n"
        "\n"
        "  Stack notation:  [*, deeper, deeper | top] -> [*] || [* | result]\n"
        "    *   = rest of stack (unchanged)\n"
        "    |   = separates deeper elements from the top\n"
        "    ->  = before / after\n"
        "    ||  = separates alternative stack effects\n"
        "\n"
        "  Quick mental model before diving into the API list:\n"
        "\n"
        "    1) Parser phase\n"
        "       Immediate words and token hooks run while parsing and can\n"
        "       rewrite incoming tokens before normal compilation continues.\n"
        "\n"
        "    2) CT VM phase\n"
        "       Compile-time words run in the CT VM with lists/maps/tokens,\n"
        "       and can register macros/rewrites or emit new definitions.\n"
        "\n"
        "    3) Runtime emission phase\n"
        "       After all compile-time execution is done, only emitted runtime\n"
        "       words remain in the final binary output.\n"
        "\n"
        "  How macros fit into this:\n"
        "    - A macro body is ordinary CT code; quotations build Syntax and\n"
        "      `emit` inserts it (§ 7C, § 13).\n"
        "    - `match` dispatches on upcoming input tokens.\n"
        "\n"
        "  Suggested reading order:\n"
        "    - Start with § 1 (execution mode and hooks).\n"
        "    - Then § 6 (token stream + rewrites).\n"
        "    - Then § 7C and § 13 for quotations and macros.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 1  COMPILE-TIME HOOKS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  compile-time                             [immediate]\n"
        "    Immediately invokes the following, already-defined word\n"
        "    in the CT VM. When used inside another definition, also\n"
        "    adds a call to that word to the enclosing body; its flags\n"
        "    determine whether that body is emitted for runtime.\n"
        "    At top level it only performs the CT invocation.\n"
        "\n"
        "      word double-ct dup + end\n"
        "      compile-time double-ct\n"
        "\n"
        "  immediate                                [immediate]\n"
        "    Mark the preceding word as immediate: it runs at parse\n"
        "    time whenever the compiler encounters it. Immediate words\n"
        "    receive a MacroContext and can consume tokens, emit ops,\n"
        "    or inject tokens into the stream.\n"
        "\n"
        "  compile-only                             [immediate]\n"
        "    Mark the preceding word as compile-only. It can only be\n"
        "    called during compilation, its asm is not emitted.\n"
        "\n"
        "  runtime                                  [immediate]\n"
        "\n"
        "  runtime-only                             [immediate]\n"
        "    Mark the preceding word as runtime-only. The word may\n"
        "    be emitted and called at runtime, but any compile-time\n"
        "    attempt to execute it is rejected.\n"
        "\n"
        "  inline                                   [immediate]\n"
        "    Mark a word for inline expansion: its body\n"
        "    is expanded at each call site instead of emitting a call.\n"
        "\n"
        "  preserve                                 [immediate]\n"
        "    Use immediately after inline to also emit the original\n"
        "    standalone definition. Calls are still expanded inline.\n"
        "\n"
        "  CT                                       [runtime + compile-time]\n"
        "    Pushes 1 when running in compile-time execution and 0 in\n"
        "    emitted runtime code, so words can branch on execution\n"
        "    mode explicitly.\n"
        "\n"
        "      CT puti cr   # prints 1 at compile time and 0 at runtime\n"
        "\n"
        "  ct-parser-set-token-hook                 [compile-only]\n"
        "    [* | name] -> [*]\n"
        "    Install the global token hook: bind the name's selected word\n"
        "    (not a macro, not a built-in-only name). Each Token the\n"
        "    parser reads is passed to the hook as `token -- handled?`;\n"
        "    nonzero consumes the Token. Language packs add more hooks\n"
        "    with ct-lang-add-token-hook (§ 8).\n"
        "\n"
        "  ct-parser-clear-token-hook               [compile-only]\n"
        "    [*] -> [*]\n"
        "    Remove the global token hook (pack hooks stay).\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 1A  COMPILER EVENT BUS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "    Every event is a map {event, timestamp_ns, payload}: a non-empty\n"
        "    name, an integer publication time (Unix nanoseconds; not an\n"
        "    ordering key), and a payload map. The payload is copied when the\n"
        "    event is published, so later changes to the producer's maps do not\n"
        "    affect what handlers see. Dispatch is synchronous: an event\n"
        "    published inside a handler is fully dispatched before the outer\n"
        "    event reaches its next handler.\n"
        "\n"
        "    `on-event <pattern>` subscribes the preceding word as a handler.\n"
        "    Patterns are case-sensitive globs such as `word.compile.*`,\n"
        "    `import.*`, or `*`. A handler has stack effect `event --`: it gets\n"
        "    its own copy of the event on an otherwise empty stack and must\n"
        "    consume it. A failing handler, or one that leaves values, stops\n"
        "    compilation with event-handler-failed.\n"
        "\n"
        "      word inspect_word_compile_event\n"
        "        event-payload \"name\" map-get static-assert drop\n"
        "      end on-event word.compile.begin\n"
        "\n"
        "    `emit-event <name>` publishes an event whose payload holds the\n"
        "    directive's SourceOrigin at key `source-origin`.\n"
        "\n"
        "  ct-event-subscribe  [*, pattern, handler | owner] -> [* | handle]\n"
        "    Subscribe a handler word (by name) to a pattern. owner is nil (a\n"
        "    global subscription) or a DefinitionHandle: such a subscription runs\n"
        "    only while that candidate is live and eligible and is removed with\n"
        "    it. Each dispatch uses the subscriptions and handler words as they\n"
        "    were when it started. A failing handler raises a recoverable error\n"
        "    (its own Diagnostic, or event-handler-failed); the failing\n"
        "    subscription is left out of that error's dispatch, and if a handler\n"
        "    resumes it the original dispatch continues.\n"
        "    While a handler runs, its own word calls' ct.execute.* events are\n"
        "    not delivered to that subscription (other subscribers get them).\n"
        "\n"
        "  ct-event-unsubscribe [* | handle] -> [* | removed]\n"
        "    Remove a subscription; 0 if it was already removed.\n"
        "\n"
        "  ct-event-emit       [*, name | payload] -> [* | 1]\n"
        "    Publish an event; payload is a map or nil (an empty map).\n"
        "\n"
        "  event-new           [* | name] -> [* | event]\n"
        "    A draft event with an empty payload and no timestamp.\n"
        "\n"
        "  event-payload-set   [*, event, key | value] -> [* | event]\n"
        "  event-payload-get   [*, event | key] -> [*, value | found]\n"
        "    Write or read one payload entry; a missing key pushes nil 0.\n"
        "\n"
        "  ct-event-emit-object [* | event] -> [* | 1]\n"
        "    Publish a draft or received event with a fresh timestamp. Only the\n"
        "    keys event, timestamp_ns, and payload are allowed. A draft can be\n"
        "    published again; each publication is a separate copy.\n"
        "\n"
        "  event-name          [* | event] -> [* | name]\n"
        "  event-timestamp     [* | event] -> [*, timestamp | found]\n"
        "  event-payload       [* | event] -> [* | payload]\n"
        "    Read the event fields; a draft has no timestamp (nil 0).\n"
        "\n"
        "  ct-event-enabled?   [*] -> [* | flag]\n"
        "  ct-event-set-enabled [* | flag] -> [* | previous]\n"
        "    Turn event publication off or on for later events. While events\n"
        "    are off, emitting or subscribing is an error. `--no-events`\n"
        "    disables events (and error recovery) for the whole compilation.\n"
        "\n"
        "  ct-event-stream-enabled?    [*] -> [* | flag]\n"
        "  ct-event-stream-set-enabled [* | flag] -> [* | previous]\n"
        "    Pause or resume writes to the `--events-stream PATH` JSON Lines\n"
        "    file (compiler values become objects tagged with a `kind` field).\n"
        "    Enabling without a configured stream is an error. A value that\n"
        "    cannot be written raises a recoverable event-stream-output-failed;\n"
        "    JSONL output is skipped while that error is dispatched, and if a\n"
        "    handler resumes it publication continues. `--no-events-json` turns\n"
        "    off the JSONL file only. Python tools use the same EventBus, and\n"
        "    daemon clients can subscribe live.\n"
        "\n"
        "  Every compiler error is published twice with the same payload:\n"
        "  first as compiler.error.<category> (for example\n"
        "  compiler.error.parser_error), then as compiler.error. The payload\n"
        "  holds the Diagnostic (key diagnostic) and recoverable. Errors met\n"
        "  while parsing a token or emitting code are recoverable: their\n"
        "  payload also has recovery {attempt, limit} and restore-point-id. A\n"
        "  handler repairs the problem and calls ct-recovery-resume to retry\n"
        "  the failed operation; without that the error stands.\n"
        "\n"
        "  A retry that fails again continues the same chain (attempt 2, 3,\n"
        "  ...). Each chain keeps the retry limit it started with (default 8).\n"
        "  When the last allowed attempt fails, the error is published once\n"
        "  more with recoverable 0 and terminal 1 and cannot be resumed.\n"
        "  compile.recovery.start is published before the first retry and\n"
        "  compile.recovery.end (outcome success or error) when the chain\n"
        "  ends. Every attempt stays in ct-diagnostic-list; compilation stops\n"
        "  after 20 unresolved errors, and recovered attempts do not count.\n"
        "\n"
        "  ct-recovery-list-restore-points [*] -> [* | points]\n"
        "    The current restore points as maps with id, stage, pos, lexeme,\n"
        "    line, and column. Works outside handlers too.\n"
        "\n"
        "  ct-recovery-get-restore-point [* | id] -> [* | point]\n"
        "    One restore point; an unknown id raises recovery-restore-point-invalid.\n"
        "\n"
        "  ct-recovery-jump    [* | id] -> [*]\n"
        "    Choose where the retry starts (a point of the same stage). The\n"
        "    last valid jump wins; an invalid one changes nothing.\n"
        "\n"
        "  ct-recovery-resume  [*] -> [* | 1]\n"
        "    Ask to retry the error being dispatched. Outside a recoverable\n"
        "    dispatch, jump and resume raise recovery-operation-unavailable.\n"
        "\n"
        "  To supply missing definitions, a handler calls\n"
        "  ct-definition-register-source (see § 11B) before resuming.\n"
        "\n"
        "  ct-recovery-get-retry-limit [*] -> [* | limit]\n"
        "  ct-recovery-set-retry-limit [* | limit] -> [* | previous]\n"
        "    Read or set the retry limit (any positive integer) for chains\n"
        "    that start later.\n"
        "\n"
        "  Example repair handler:\n"
        "    word repair_missing\n"
        "      event-payload dup \"recoverable\" map-get drop if\n"
        "        \"word missing_word 42 end\" ct-definition-register-source drop\n"
        "        ct-recovery-resume drop\n"
        "      end drop\n"
        "    end on-event compiler.error\n"
        "\n"
        "  Restore points save parser position/location, not arbitrary CT state.\n"
        "  Side effects are not rolled back, so handlers should be idempotent.\n"
        "  Errors outside parsing and emission (tokenizing, imports, the\n"
        "  assembler and linker) are published but are not recoverable.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 2  LIST OPERATIONS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Lists are dynamic arrays that live in the CT VM. They hold\n"
        "  integers, strings, tokens, other lists, maps, or nil.\n"
        "\n"
        "  Conventions for all list, map, and string words: reads consume\n"
        "  their subject; mutations return the subject first, then any\n"
        "  results. Indexes are Python-like (negative counts from the end) and\n"
        "  must be integers, not booleans. Wrong types, invalid indexes, and\n"
        "  unsupported map keys are CT errors raised before any mutation.\n"
        "\n"
        "  list-new            [*] -> [* | list]\n"
        "    Create a new empty list.\n"
        "\n"
        "  list-clone          [* | list] -> [* | copy]\n"
        "    Shallow copy: nested lists and maps are shared.\n"
        "\n"
        "  list-deep-clone     [* | list] -> [* | copy]\n"
        "    Recursive copy of nested lists and maps. Shared references and\n"
        "    cycles inside the copied graph are preserved; immutable\n"
        "    compiler-provided maps become ordinary mutable maps.\n"
        "\n"
        "  list-append         [*, list | value] -> [* | list]\n"
        "    Append value to the end of list (mutates in place).\n"
        "\n"
        "  list-push-front     [*, list | value] -> [* | list]\n"
        "    Insert value at the beginning of list.\n"
        "\n"
        "  list-pop            [* | list] -> [*, list, value | found]\n"
        "    Remove the last element. On an empty list pushes the unchanged\n"
        "    list, nil, and 0.\n"
        "\n"
        "  list-pop-front      [* | list] -> [*, list, value | found]\n"
        "    Remove the first element. On an empty list pushes the unchanged\n"
        "    list, nil, and 0.\n"
        "\n"
        "  list-peek-front     [* | list] -> [* | value]\n"
        "    Push the first element without removing it. Errors on an empty\n"
        "    list.\n"
        "\n"
        "  list-last           [* | list] -> [* | value]\n"
        "    Push the last element without removing it. Errors on an empty\n"
        "    list.\n"
        "\n"
        "  list-get            [*, list | index] -> [* | value]\n"
        "    Get the element at index. Errors outside the valid range.\n"
        "\n"
        "  list-set            [*, list, index | value] -> [* | list]\n"
        "    Replace the element at index. Errors outside the valid range.\n"
        "\n"
        "  list-insert         [*, list, index | value] -> [* | list]\n"
        "    Insert value at index. Valid indexes are -length..length\n"
        "    inclusive; length appends.\n"
        "\n"
        "  list-remove         [*, list | index] -> [*, list | value]\n"
        "    Remove and push the element at index.\n"
        "\n"
        "  list-reverse        [* | list] -> [* | list]\n"
        "    Reverse the list in place.\n"
        "\n"
        "  list-clear          [* | list] -> [* | list]\n"
        "    Remove all elements from the list.\n"
        "\n"
        "  list-extend         [*, target | source] -> [* | target]\n"
        "    Append the elements of source to target. Extending a list with\n"
        "    itself appends one snapshot of its elements.\n"
        "\n"
        "  list-length         [* | list] -> [* | n]\n"
        "    Push the number of elements.\n"
        "\n"
        "  list-empty?         [* | list] -> [* | flag]\n"
        "    Push 1 if the list is empty, 0 otherwise.\n"
        "\n"
        "  list-slice          [*, list, start | end] -> [* | sublist]\n"
        "    Push a new list with list[start:end] (Python slice bounds).\n"
        "\n"
        "  list-find           [*, list | value] -> [*, index | found]\n"
        "    First structurally equal element: (index, 1) or (-1, 0).\n"
        "\n"
        "  list-contains?      [*, list | value] -> [* | flag]\n"
        "    Push 1 if a structurally equal element exists, else 0.\n"
        "    Equality compares lists and maps recursively (cycle-safe, map\n"
        "    order ignored), tokens by lexeme, and numbers by value\n"
        "    (true equals 1); other compiler objects compare by identity.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 3  MAP OPERATIONS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Maps are insertion-ordered. Keys are strings, integers, booleans,\n"
        "  or nil; true and 1 (false and 0) address the same entry. Updating\n"
        "  an existing key keeps its stored key and position.\n"
        "\n"
        "  map-new             [*] -> [* | map]\n"
        "    Create a new empty map.\n"
        "\n"
        "  map-set             [*, map, key | value] -> [* | map]\n"
        "    Set key to value in the map (mutates in place).\n"
        "\n"
        "  map-get             [*, map | key] -> [*, value | found]\n"
        "    Look up key: (value, 1) when present, (nil, 0) when absent.\n"
        "    A present key whose value is nil gives (nil, 1).\n"
        "\n"
        "  map-has?            [*, map | key] -> [* | flag]\n"
        "    Push 1 if the key exists in the map, 0 otherwise.\n"
        "\n"
        "  map-delete          [*, map | key] -> [*, map | found]\n"
        "    Delete key if present. Pushes the map, then 1 if deleted, else 0.\n"
        "\n"
        "  map-clear           [* | map] -> [* | map]\n"
        "    Remove all entries from the map.\n"
        "\n"
        "  map-length          [* | map] -> [* | n]\n"
        "    Push number of entries in map.\n"
        "\n"
        "  map-empty?          [* | map] -> [* | flag]\n"
        "    Push 1 if map has no entries, 0 otherwise.\n"
        "\n"
        "  map-keys            [* | map] -> [* | list]\n"
        "    Push a list of map keys in insertion order.\n"
        "\n"
        "  map-values          [* | map] -> [* | list]\n"
        "    Push a list of map values in insertion order.\n"
        "\n"
        "  map-clone           [* | map] -> [* | copy]\n"
        "    Shallow copy into a new ordinary mutable map.\n"
        "\n"
        "  map-merge           [*, target | source] -> [* | target]\n"
        "    Copy source entries into target; source values win. Existing\n"
        "    keys keep their position, new keys append in source order. All\n"
        "    source keys are validated first; merging a map with itself is a\n"
        "    no-op.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 4  NIL\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  nil                 [*] -> [* | nil]\n"
        "    Push the nil sentinel value.\n"
        "\n"
        "  nil?                [* | value] -> [* | flag]\n"
        "    Push 1 if the value is nil, 0 otherwise.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 5  STRING OPERATIONS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Strings are immutable sequences of Unicode code points. String\n"
        "  inputs are strict: integers and tokens are not converted. Character\n"
        "  classes are fixed by L2, not by the host: whitespace is the 25\n"
        "  Unicode White_Space code points, and case mapping is ASCII-only.\n"
        "\n"
        "  string-equal?       [*, a | b] -> [* | flag]\n"
        "    Push 1 if a and b are the same code-point sequence (no\n"
        "    normalization or case folding), 0 otherwise.\n"
        "\n"
        "  string-length       [* | str] -> [* | n]\n"
        "    Push the number of code points (not UTF-8 bytes).\n"
        "\n"
        "  string-utf8-length  [* | str] -> [* | n]\n"
        "    Push the number of UTF-8 bytes.\n"
        "\n"
        "  string-append       [*, left | right] -> [* | result]\n"
        "    Concatenate two strings.\n"
        "\n"
        "  try-string>int      [* | str] -> [*, value | found]\n"
        "    Parse a signed integer: surrounding whitespace allowed, ASCII\n"
        "    digits, optional 0x/0b/0o prefix (case-insensitive) after an\n"
        "    optional sign, underscores only between digits. Pushes\n"
        "    (value, 1) on success or (0, 0) on failure.\n"
        "\n"
        "  int>string          [* | n] -> [* | str]\n"
        "    Decimal string of an integer; booleans give \"0\"/\"1\". Floats and\n"
        "    other types are errors.\n"
        "\n"
        "  identifier?         [* | value] -> [* | flag]\n"
        "    Push 1 for a string or token whose text is an L2 identifier: the\n"
        "    first code point is an ASCII letter, _, or any code point at or\n"
        "    above U+0080, and later ones may also be ASCII digits. Other\n"
        "    values give 0.\n"
        "\n"
        "  string-contains?    [*, haystack | needle] -> [* | flag]\n"
        "    Push 1 if needle occurs inside haystack, else 0. An empty needle\n"
        "    matches.\n"
        "\n"
        "  string-starts-with? [*, text | prefix] -> [* | flag]\n"
        "    Push 1 if text starts with prefix, else 0.\n"
        "\n"
        "  string-ends-with?   [*, text | suffix] -> [* | flag]\n"
        "    Push 1 if text ends with suffix, else 0.\n"
        "\n"
        "  string-split        [*, text | sep] -> [* | list]\n"
        "    Split text by a non-empty separator, keeping empty fields.\n"
        "\n"
        "  string-join         [*, list | sep] -> [* | str]\n"
        "    Join a list of strings with sep. Non-string elements are errors\n"
        "    (convert integers with int>string).\n"
        "\n"
        "  string-strip        [* | text] -> [* | stripped]\n"
        "    Trim leading and trailing whitespace.\n"
        "\n"
        "  string-replace      [*, text, old | new] -> [* | replaced]\n"
        "    Replace every non-overlapping occurrence of a non-empty old\n"
        "    substring, left to right. new may be empty.\n"
        "\n"
        "  string-upper        [* | text] -> [* | upper]\n"
        "    Map a-z to A-Z; other code points are unchanged.\n"
        "\n"
        "  string-lower        [* | text] -> [* | lower]\n"
        "    Map A-Z to a-z; other code points are unchanged.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 6  TOKEN STREAM MANIPULATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  These words build and inspect Token values and configure how the\n"
        "  parser's token stream is produced. The live parser cursor itself\n"
        "  is covered in § 7.\n"
        "\n"
        "  Tokens are immutable values. Each has a primary SourceOrigin (the\n"
        "  span in its original file) and an expansion chain of ExpansionFrames\n"
        "  recording the transformations that produced it, outermost first.\n"
        "  Rewrites, token hooks, and macro expansions append a frame; new tokens\n"
        "  are built from an explicit origin or an existing token, never from a\n"
        "  bare lexeme.\n"
        "\n"
        "  token-lexeme        [* | token] -> [* | str]\n"
        "    Push the token's lexeme. Non-token values are errors.\n"
        "\n"
        "  token-from-origin   [*, lexeme | origin] -> [* | token]\n"
        "    New token with a non-empty lexeme, anchored at a SourceOrigin, with\n"
        "    an empty expansion chain.\n"
        "\n"
        "  ct-definition-set-effect [*, name, inputs | outputs] -> [* | ok]\n"
        "  ct-definition-effect     [* | name] -> [*, inputs, outputs | found]\n"
        "    Declare / read a word's stack effect in cells (nil: unknown).\n"
        "    A declared effect is checked by the stack checker (calls and the\n"
        "    body's final depth); effect comments count items, not cells, and\n"
        "    are not used for that. `stack-effect NAME IN OUT` (core.sl)\n"
        "    declares one in source; fn declares its functions'.\n"
        "\n"
        "  token-slice         [*, token, start | end] -> [* | token]\n"
        "    The code points [start, end) of the lexeme as a new token. A token\n"
        "    read from source gets the exact sub-span as its origin; otherwise\n"
        "    the origin is kept. The expansion chain is kept.\n"
        "\n"
        "  lexeme              [* | token-or-string] -> [* | string]\n"
        "    A token's lexeme, or the string itself.\n"
        "\n"
        "  token?              [* | value] -> [* | flag]\n"
        "    1 when the value is a token.\n"
        "\n"
        "  token-with-lexeme   [*, token | lexeme] -> [* | token]\n"
        "    New token with a non-empty lexeme that keeps the template token's\n"
        "    origin and expansion chain. The template is not changed.\n"
        "\n"
        "  token-origin        [* | token] -> [* | origin]\n"
        "    Push the token's SourceOrigin.\n"
        "\n"
        "  token-expansion-chain [* | token] -> [* | frames]\n"
        "    Push the token's ExpansionFrames as a list, outermost first.\n"
        "\n"
        "  token-line          [* | token] -> [* | line]\n"
        "    1-based line of the token's origin in its file.\n"
        "\n"
        "  token-column        [* | token] -> [* | column]\n"
        "    1-based column (in code points, tabs count as one) of the token's\n"
        "    origin.\n"
        "\n"
        "  A SourceOrigin is an immutable span: canonical path, 0-based\n"
        "  half-open code-point offsets into the original file, and 1-based\n"
        "  start/end line and column (the end is one past the last character).\n"
        "  LF, CRLF, and lone CR each end one line.\n"
        "\n"
        "  source-origin-path         [* | origin] -> [* | path]\n"
        "  source-origin-start        [* | origin] -> [* | offset]\n"
        "  source-origin-end          [* | origin] -> [* | offset]\n"
        "  source-origin-start-line   [* | origin] -> [* | line]\n"
        "  source-origin-start-column [* | origin] -> [* | column]\n"
        "  source-origin-end-line     [* | origin] -> [* | line]\n"
        "  source-origin-end-column   [* | origin] -> [* | column]\n"
        "    Read one field of a SourceOrigin.\n"
        "\n"
        "  An ExpansionFrame records one transformation: its kind (for example\n"
        "  macro, reader, grammar, or token-hook), the rule or word name, the\n"
        "  SourceOrigin of the triggering token, and, for macros, the macro's\n"
        "  definition origin when known.\n"
        "\n"
        "  expansion-frame-kind              [* | frame] -> [* | kind]\n"
        "  expansion-frame-name              [* | frame] -> [* | name]\n"
        "  expansion-frame-call-site         [* | frame] -> [* | origin]\n"
        "  expansion-frame-definition-origin [* | frame] -> [* | origin-or-nil]\n"
        "    Read one field of an ExpansionFrame.\n"
        "\n"
        "  Tokenizer separators are configured with ct-tokenizer-set-config\n"
        "  (§ 7A); a change affects later tokenization only.\n"
        "\n"
        "  ── Rewrites\n"
        "\n"
        "  A rewrite rule replaces a token sequence matched by a pattern list\n"
        "  with a replacement list. Reader rules run first on each Token, then\n"
        "  the token hooks, then grammar rules. Eligible rules (enabled, on an\n"
        "  active pipeline, owning pack active) are tried in one fixed order:\n"
        "  priority descending, then earlier registration. After a rewrite the\n"
        "  parser retries at the replacement start; consumed input is never\n"
        "  revisited.\n"
        "\n"
        "  Pattern elements: a literal; `\\x` (the literal x: `\\*`, `\\$x`,\n"
        "  `\\!y`, `\\\\`); `*` (any one token); `$name[:constraint]` (one\n"
        "  token); `$*name[:constraint]` (zero or more); `!piece` (one token\n"
        "  that does not match piece); and a `?`, `*`, or `+` suffix on a\n"
        "  literal or `$name` capture. Constraints: any ident int float number\n"
        "  string char literal. Replacement elements are literals, `\\x`\n"
        "  escapes, `$name` (a single-token capture), or `$*name` (a sequence\n"
        "  capture). Everything is validated at registration.\n"
        "\n"
        "  ct-rewrite-add-reader        [*, pattern | replacement] -> [* | handle]\n"
        "  ct-rewrite-add-reader-named  [*, name, pattern | replacement] -> [* | handle]\n"
        "  ct-rewrite-add-grammar       [*, pattern | replacement] -> [* | handle]\n"
        "  ct-rewrite-add-grammar-named [*, name, pattern | replacement] -> [* | handle]\n"
        "    Register a rule (priority 0, enabled, pipeline \"default\", no\n"
        "    guard) and push its RewriteHandle. Names need not be unique;\n"
        "    unnamed rules get a generated name. Example:\n"
        "      list-new \"kw\" list-append\n"
        "      list-new \"42\" list-append\n"
        "      ct-rewrite-add-grammar drop\n"
        "\n"
        "  ct-rewrite-remove [* | handle] -> [* | removed]\n"
        "    Remove the rule a handle identifies (either stage). A stale\n"
        "    handle pushes 0.\n"
        "\n"
        "  ct-rewrite-clear-reader  [*] -> [* | count]\n"
        "  ct-rewrite-clear-grammar [*] -> [* | count]\n"
        "    Remove every rule of the stage that no language pack owns.\n"
        "\n"
        "  ct-rewrite-list-reader  [*] -> [* | list]\n"
        "  ct-rewrite-list-grammar [*] -> [* | list]\n"
        "    One {handle, name} map per registration, in registration order,\n"
        "    including disabled rules and rules of inactive packs.\n"
        "\n"
        "  ct-rewrite-rule-set-priority [*, handle | priority] -> [* | found]\n"
        "  ct-rewrite-rule-set-enabled  [*, handle | flag] -> [* | found]\n"
        "  ct-rewrite-rule-set-pipeline [*, handle | pipeline] -> [* | found]\n"
        "  ct-rewrite-rule-set-guard    [*, handle | name-or-nil] -> [* | found]\n"
        "    Change one property of a rule; 0 for a stale handle.\n"
        "\n"
        "  ct-rewrite-rule-get-priority [* | handle] -> [*, priority | found]\n"
        "  ct-rewrite-rule-get-enabled  [* | handle] -> [*, flag | found]\n"
        "  ct-rewrite-rule-get-pipeline [* | handle] -> [*, pipeline | found]\n"
        "  ct-rewrite-rule-get-guard    [* | handle] -> [*, name-or-nil | found]\n"
        "    Read one property; also accepts `stage name` for a name unique in\n"
        "    the stage (an ambiguous name is an error). Missing -> nil 0.\n"
        "\n"
        "  A guard is a CT word `payload -- applies?` called when its rule\n"
        "  matches; payload has stage, handle, rule, start, end, pattern,\n"
        "  replacement, and captures. 0 skips the rule.\n"
        "\n"
        "  ct-rewrite-set-pipeline-active   [*, stage, pipeline | flag] -> [*]\n"
        "  ct-rewrite-list-active-pipelines [* | stage] -> [* | pipelines]\n"
        "    Activate/deactivate a pipeline; list active ones (sorted).\n"
        "\n"
        "  ct-rewrite-scope-push [*] -> [* | depth]\n"
        "  ct-rewrite-scope-pop  [*] -> [* | restored]\n"
        "    Save/restore the active pipeline sets.\n"
        "\n"
        "  ct-rewrite-set-max-steps [* | limit] -> [*]\n"
        "  ct-rewrite-get-max-steps [*] -> [* | limit]\n"
        "    Rewrite applications allowed per parse, shared by both stages\n"
        "    (default 100000; 0 allows none).\n"
        "\n"
        "  ct-rewrite-set-loop-detection [* | flag] -> [*]\n"
        "  ct-rewrite-loop-detection?    [*] -> [* | flag]\n"
        "    Reject a rewrite that would repeat an earlier token sequence of\n"
        "    the stage (default on).\n"
        "\n"
        "  ct-rewrite-get-loop-reports   [*] -> [* | reports]\n"
        "  ct-rewrite-clear-loop-reports [*] -> [* | count]\n"
        "    Read/clear the reports of detected loops.\n"
        "\n"
        "  ct-rewrite-run-on-list [*, stage | lexemes] -> [*, lexemes | patches]\n"
        "  ct-rewrite-dry-run     [*, stage, lexemes | max-steps] -> [*, lexemes | patches]\n"
        "    Apply the stage's rules to a lexeme list (the input is not\n"
        "    changed) and push the result plus one patch map per application\n"
        "    (stage, handle, rule, start, end, before, after, priority). Live\n"
        "    rewrites publish the same patch as a rewrite.applied event.\n"
        "\n"
        "  ct-expansion-set-depth-limit [* | limit] -> [*]\n"
        "\n"
        "  ct-expansion-get-depth-limit [*] -> [* | limit]\n"
        "    Set/read the nesting limit shared by macro and rewrite\n"
        "    expansion (default 256; positive integers only).\n"
        "\n"
        "  ── Deterministic random numbers\n"
        "\n"
        "  One pseudo-random stream per compilation, starting from seed\n"
        "  0x1A2B3C4D; imports and CT code draw from it in execution order and\n"
        "  error recovery does not rewind it. Arguments are integers (booleans\n"
        "  are rejected). Sequences are reproducible for the same Python\n"
        "  version, seed, and draws, but not across Python versions; the\n"
        "  stream is not suitable for secrets.\n"
        "\n"
        "  ct-rng-seed  [* | seed] -> [*]\n"
        "    Reset the stream; the seed is reduced modulo 2^64.\n"
        "\n"
        "  ct-rng-int   [* | bound] -> [* | n]\n"
        "    n in [0, bound); bound must be > 0.\n"
        "\n"
        "  ct-rng-range [*, lo | hi] -> [* | n]\n"
        "    n in [lo, hi] (inclusive); lo must be <= hi.\n"
        "\n"
        "  ── Control frames, operations, and labels\n"
        "\n"
        "  Control frames are ordinary maps on the parser's control stack\n"
        "  (shared with built-in control parsing; explicit pushes and pops are\n"
        "  not undone if parsing later fails). Push, peek, and pop copy the\n"
        "  top-level map only; nested lists and maps stay shared. The parser\n"
        "  may add `line`/`column` fields.\n"
        "\n"
        "  ct-control-frame-new [* | type] -> [* | frame]\n"
        "    A map with `type` set.\n"
        "\n"
        "  ct-control-get       [*, frame | key] -> [*, value | found]\n"
        "    The key's value and 1, or nil 0 when absent (nil 1 for a stored nil).\n"
        "\n"
        "  ct-control-get?      [*, frame | key] -> [* | value]\n"
        "    The key's value, or nil when absent (no found flag).\n"
        "\n"
        "  ct-control-set       [*, frame, key | value] -> [* | frame]\n"
        "    Store a value in the map and return that same map.\n"
        "\n"
        "  ct-control-push      [* | frame] -> [*]\n"
        "  ct-control-pop       [*] -> [* | frame]\n"
        "  ct-control-peek      [*] -> [* | frame-or-nil]\n"
        "  ct-control-depth     [*] -> [* | n]\n"
        "    Stack operations; pop on an empty stack is an error and changes\n"
        "    nothing.\n"
        "\n"
        "  ct-control-add-close-op [*, frame, op | data] -> [* | frame]\n"
        "    Append the descriptor {op, data} to frame.close_ops. When `end`\n"
        "    closes the frame, every descriptor is validated first (exactly the\n"
        "    keys op and data, op a nonempty string); a bad one leaves the frame\n"
        "    on the stack and emits nothing. Otherwise the frame is popped and\n"
        "    each op is appended in order. `data` is kept by reference.\n"
        "\n"
        "  ct-op-emit           [*, op | data] -> [*]\n"
        "    Append an internal operation to the current body; `data` is kept\n"
        "    by reference. Unsupported operations fail later, at emission.\n"
        "\n"
        "  ct-label-new         [* | prefix] -> [* | label]\n"
        "    A fresh `L_<prefix>_<n>` label; the prefix must match\n"
        "    [A-Za-z_][A-Za-z0-9_]*. Unique only among allocated labels.\n"
        "\n"
        "  ct-parser-last-line  [*] -> [* | line]\n"
        "    Line of the last parser token, or 0.\n"
        "\n"
        "  ct-parser-register-block-opener   [* | name] -> [*]\n"
        "  ct-parser-unregister-block-opener [* | name] -> [*]\n"
        "    Edit the lexemes counted as block openers by `with` nesting and\n"
        "    predeclaration scans (default: word with for while begin). This\n"
        "    never disables a built-in construct.\n"
        "\n"
        "  ct-control-register-override   [* | name] -> [*]\n"
        "  ct-control-unregister-override [* | name] -> [*]\n"
        "    Route if, else, for, while, or do (only these) to the immediate\n"
        "    word of that name. While registered, a missing, runtime-only, or\n"
        "    non-immediate handler is an error; it never falls back to the\n"
        "    built-in.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 7  PARSER STREAM\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Compile-time words can read, rewind, and extend the live token\n"
        "  stream while the parser runs. Positions are token indexes in\n"
        "  [0, token-count]. Injected tokens are read next and go through the\n"
        "  normal stages again (reader rewrites, token hook, macros, grammar\n"
        "  rewrites); they are never turned back into source text.\n"
        "\n"
        "  ct-parser-consume     [*] -> [* | token]\n"
        "    Consume the next token. Raises a parse error at end of input;\n"
        "    check ct-parser-eof? first when input may be exhausted.\n"
        "\n"
        "  ct-parser-peek        [* | offset] -> [* | token-or-nil]\n"
        "    Look ahead without consuming; 0 is the next token. The offset\n"
        "    must be a non-negative integer; past the end pushes nil.\n"
        "\n"
        "  ct-parser-current     [*] -> [* | token-or-nil]\n"
        "    The most recently consumed token, or nil. Consuming and\n"
        "    collecting update it; peeking and injecting do not. While an\n"
        "    immediate word runs it starts as the word's own token, so read it\n"
        "    before consuming if it should anchor generated tokens.\n"
        "\n"
        "  ct-parser-get-pos     [*] -> [* | position]\n"
        "  ct-parser-remaining   [*] -> [* | count]\n"
        "  ct-parser-eof?        [*] -> [* | flag]\n"
        "  ct-parser-tail        [*] -> [* | tokens]\n"
        "    Inspect the cursor, the number of tokens left, the end-of-input\n"
        "    state, and a copy of the remaining tokens. None of them consume.\n"
        "\n"
        "  ct-parser-set-pos     [* | position] -> [* | old-position]\n"
        "    Move the cursor; the current token becomes the token before the\n"
        "    new position (nil at 0). Out-of-range positions are errors.\n"
        "\n"
        "  ct-parser-slice       [*, start | end] -> [* | tokens]\n"
        "    The tokens between two positions, in stream order (the smaller\n"
        "    position first), without moving the cursor.\n"
        "\n"
        "  ct-parser-inject      [* | tokens] -> [*]\n"
        "    Insert a list of tokens at the cursor, in list order, without\n"
        "    moving it. Every element must be a token. Build tokens with\n"
        "    token-with-lexeme or token-from-origin.\n"
        "\n"
        "  ct-parser-collect-until [* | delimiter] -> [*, tokens | found]\n"
        "    Consume up to and including the delimiter and return the tokens\n"
        "    before it. At end of input pushes the tokens read and 0. The\n"
        "    delimiter must be non-empty.\n"
        "\n"
        "  ct-parser-collect-balanced [*, open | close] -> [*, tokens | found]\n"
        "    Call after consuming the outer opener. Returns the interior,\n"
        "    nested pairs included, and consumes the matching close. When open\n"
        "    and close are equal the first match closes (no nesting).\n"
        "\n"
        "  ct-parser-expected    [* | expected] -> [*, token-or-nil | matched]\n"
        "    Non-consuming check of the next token against a lexeme, a token,\n"
        "    or a non-empty list of them. Pushes the token and 1 on a match,\n"
        "    or nil and 0 on a mismatch or at end of input.\n"
        "\n"
        "  ct-parser-checkpoint  [*] -> [* | checkpoint]\n"
        "  ct-parser-restore     [* | checkpoint] -> [* | restored]\n"
        "    A checkpoint is an immutable opaque value holding the cursor and\n"
        "    current token. Restoring pushes 1, or 0 without changing anything\n"
        "    when the checkpoint belongs to another parser or its position no\n"
        "    longer fits the stream. It does not undo injected tokens.\n"
        "\n"
        "  ct-parser-session-begin    [*] -> [* | session]\n"
        "  ct-parser-session-commit   [* | session] -> [* | committed]\n"
        "  ct-parser-session-rollback [* | session] -> [* | rolled-back]\n"
        "    A session saves the token list, cursor, and current token, so a\n"
        "    rollback also undoes injected tokens. Sessions nest in strict\n"
        "    LIFO order: commit and rollback push 1 for the open topmost\n"
        "    session and 0, changing nothing, for any other session. They do\n"
        "    not undo definitions, rewrites, or other side effects.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 7A  TOKENIZER AND SOURCE CURSOR\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  The default L2 tokenizer splits text at whitespace (the 25 fixed\n"
        "  Unicode White_Space code points) and at configured separators.\n"
        "  String literals, character literals, and # comments are fixed\n"
        "  rules. A backtick ` is always a token of its own, and \\` is one\n"
        "  escaped-backtick token; both end a word in progress. At each\n"
        "  position the longest matching separator wins over word text, so\n"
        "  with ; configured, ;end is ; then end.\n"
        "\n"
        "  ct-tokenizer-get-config [*] -> [* | config]\n"
        "    A detached copy of the configuration map. Its only key is\n"
        "    separators, a list of non-empty strings, initially\n"
        "    ( ) { } ; , [ ].\n"
        "\n"
        "  ct-tokenizer-set-config [* | config] -> [*]\n"
        "    Validate the complete map and replace the configuration. The\n"
        "    separators key is required; unknown keys, empty strings, and\n"
        "    separators containing whitespace, a quote, a backtick, or # are\n"
        "    errors that leave the configuration unchanged. The setting\n"
        "    lasts for the rest of the compilation and affects only later\n"
        "    tokenization: source already read is not split again.\n"
        "\n"
        "  ct-tokenizer-run    [*, text | path] -> [* | tokens]\n"
        "    Tokenize text with the current configuration. Each token's\n"
        "    origin names path (used as given) and its exact span in text.\n"
        "    No rewrites, hooks, or macros are applied. Unterminated or\n"
        "    malformed literals are errors at their source span.\n"
        "\n"
        "  A source cursor reads raw text one code point at a time, for\n"
        "  frontends with their own lexical rules. It is a mutable handle:\n"
        "  every reference to it sees the same position. Offsets are 0-based\n"
        "  code-point indexes; LF, CRLF, and lone CR each end one line.\n"
        "\n"
        "  source-cursor-new   [*, text | path] -> [* | cursor]\n"
        "    New cursor at offset 0. path is used as given, without lookup.\n"
        "\n"
        "  source-cursor-peek  [* | cursor] -> [* | codepoint]\n"
        "  source-cursor-read  [* | cursor] -> [*, cursor | codepoint]\n"
        "    The next code point as an integer; read also advances. At end\n"
        "    of text both raise source-cursor-eof without moving.\n"
        "\n"
        "  source-cursor-eof?  [* | cursor] -> [* | flag]\n"
        "  source-cursor-pos   [* | cursor] -> [* | offset]\n"
        "  source-cursor-set-pos [*, cursor | offset] -> [* | cursor]\n"
        "    Inspect or move the cursor; set-pos accepts [0, length].\n"
        "\n"
        "  source-cursor-slice [*, cursor, start | end] -> [* | str]\n"
        "  source-cursor-span  [*, cursor, start | end] -> [* | origin]\n"
        "    The text, or a SourceOrigin with line and column, of the range\n"
        "    [start, end), without moving the cursor. Bad offsets or ranges\n"
        "    raise source-cursor-invalid-argument.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 7B  IMPORTS AND CUSTOM FRONTENDS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  An import request is the token import followed by a string\n"
        "  literal path. A line `import path` in L2 source is that request.\n"
        "  The parser loads the file when it reaches the request, so CT code\n"
        "  that ran earlier (for example ct-tokenizer-set-config) applies to\n"
        "  it. Each file is loaded once; relative paths resolve against the\n"
        "  importing file's directory, then the include paths. A request that\n"
        "  is not followed by a string literal raises import-malformed; a\n"
        "  missing file raises import-unresolved.\n"
        "\n"
        "  A file selects a custom frontend with a header on its first\n"
        "  nonblank line:\n"
        "\n"
        "    frontend \"libs/my_lang.sl\" entry-word   # entry optional\n"
        "\n"
        "  The implementation is ordinary L2 source, imported once per\n"
        "  compilation with the default frontend. Its entry word (default\n"
        "  frontend-parse) runs at compile time for each file that uses the\n"
        "  frontend:\n"
        "\n"
        "    entry               [*, text | path] -> [* | tokens]\n"
        "\n"
        "  text is the file's exact content, header included (skip it, but\n"
        "  keep offsets into the full text); path is its canonical path. It\n"
        "  must leave exactly one list of tokens. The tokens are parsed as\n"
        "  they are: reader rewrites, the token hook, and grammar rewrites do\n"
        "  not apply to them. Each gets an outer expansion frame of kind\n"
        "  frontend naming the entry word. The frontend owns import syntax and\n"
        "  lowers imports to import \"path\" requests; files imported without a\n"
        "  header use the same frontend. Errors use frontend-header-invalid,\n"
        "  frontend-implementation-unavailable, frontend-entry-unavailable,\n"
        "  frontend-invalid-result, and frontend-entry-failed.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 7C  QUOTATIONS AND SYNTAX\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Backticks quote source as a compile-time value: `1 2 +` is an\n"
        "  immutable Syntax value holding those Tokens. Nothing inside runs\n"
        "  when the value is built. Contents use the ordinary tokenizer\n"
        "  (strings are atomic, comments are dropped); quotations do not\n"
        "  nest. Inside a quotation, \\$name gives the token $name, \\` a\n"
        "  backtick, and \\\\ a backslash; any other escape is an error\n"
        "  (syntax-escape-invalid). $name splices a macro parameter or match\n"
        "  binding (a Token, or all Tokens of a Syntax); an unbound $name is\n"
        "  an error when the quotation is evaluated. A quotation without a\n"
        "  closing backtick is syntax-unterminated-quotation. Quotations are\n"
        "  compile-time only: one reached from runtime code is\n"
        "  syntax-quotation-runtime, so words holding them are compile-only.\n"
        "  Literal Tokens keep their source origin.\n"
        "\n"
        "  $( code ) runs compile-time code and splices its value: a Token\n"
        "  or Syntax as is, a string as one word, an integer as a literal,\n"
        "  a list element by element. The value is the top of the stack\n"
        "  afterwards; the code may consume one value below it, so $( )\n"
        "  splices (and consumes) the top value and $(dup) a copy. Earlier\n"
        "  $( ) values are already taken off the stack. Text written against it is\n"
        "  pasted on: get-$(name) and $(name)-x form one token. Quotations\n"
        "  may appear inside $( ). Quotations do not nest: a macro that\n"
        "  generates a macro writes the inner quotation with \\` and \\$name\n"
        "  escapes (e.g. `macro $name (x) \\` \\$x $n + \\` emit ;` emit).\n"
        "  Unique names come from gensym:\n"
        "\n"
        "      `word $(\"tmp\" gensym) 1 end`\n"
        "\n"
        "  syntax-length  [* | syntax] -> [* | count]\n"
        "  syntax-get     [*, syntax | index] -> [* | token]\n"
        "    Checked read; negative indices count from the end.\n"
        "  syntax-slice   [*, syntax, start | end] -> [* | syntax]\n"
        "  syntax-concat  [*, left | right] -> [* | syntax]\n"
        "  syntax>list    [* | syntax] -> [* | tokens]\n"
        "  list>syntax    [* | tokens] -> [* | syntax]\n"
        "    Only a list of Tokens is accepted.\n"
        "\n"
        "  emit  [* | syntax] -> [*]\n"
        "    Insert the Syntax's Tokens at the current position of the input\n"
        "    being compiled, so the parser reads them next. Outside a parse it\n"
        "    raises emit-no-input.\n"
        "\n"
        "      word define_answer `word answer 42 end` emit end\n"
        "      compile-time define_answer   # answer is defined here\n"
        "\n"
        "  match [pattern] => body ... end\n"
        "    Compile-time dispatch on the upcoming input values (Tokens, or a\n"
        "    whole quotation as one Syntax value). Clauses are tested in order\n"
        "    without consuming; the first match is consumed and its body runs\n"
        "    with its $name bindings. Elements: literal, `*`, `$name`,\n"
        "    `$name:type`, `\\x`. `[_]` is the default (last; an empty body\n"
        "    passes input through). The rewrite-only forms $*name, x?, x*,\n"
        "    x+ and !x are rejected (escape them for literals). Exact duplicate\n"
        "    patterns warn (match-duplicate-pattern). Bad patterns are\n"
        "    match-pattern-invalid; no fitting clause is match-no-clause.\n"
        "    Repetition: `$( elements )*` (zero or more) or `$( ... )+` (one\n"
        "    or more), with an optional separator written against the\n"
        "    quantifier (`$( $k, =, $v ),*`). It is greedy and gives back\n"
        "    iterations the rest of the pattern needs; each name inside binds\n"
        "    to a list. In a quotation, `$( ... )*` repeats its tokens once per\n"
        "    element of the repeated names it uses (all of equal length), with\n"
        "    the separator between: `$( word $k $v end )*`.\n"
        "\n"
        "  gensym  [* | prefix] -> [* | name]\n"
        "    A fresh identifier: the prefix reduced to [A-Za-z0-9_] (`g` if\n"
        "    empty, `g_` prepended before a digit), then `_N` from a counter\n"
        "    that only grows, skipping names that are already defined.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 8  ASSEMBLY OUTPUT CONTROL\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  These compile-only words add trusted assembler fragments to the\n"
        "  output of the current compilation. Fragments are strings emitted\n"
        "  verbatim (no parsing or normalization); between a fragment and its\n"
        "  neighbour one LF is inserted only when neither side already has a\n"
        "  line break there, and empty fragments emit nothing. Set validates\n"
        "  the whole list before changing the buffer.\n"
        "\n"
        "  ct-output-append-text-preamble [* | fragment] -> [*]\n"
        "  ct-output-set-text-preamble    [* | fragment-list] -> [*]\n"
        "  ct-output-clear-text-preamble  [*] -> [*]\n"
        "    The .text preamble: emitted after the runtime prelude and before\n"
        "    word definitions. It never owns the entry point: a program with\n"
        "    its own `_start` uses `:asm _start { ... } ;`.\n"
        "\n"
        "  ct-output-append-data [* | fragment] -> [*]\n"
        "  ct-output-set-data    [* | fragment-list] -> [*]\n"
        "  ct-output-clear-data  [*] -> [*]\n"
        "    CT .data fragments, emitted after definition data (string\n"
        "    literals) and before variable storage. Set and clear affect only\n"
        "    these fragments, never compiler-generated data.\n"
        "\n"
        "  ct-output-append-bss [* | fragment] -> [*]\n"
        "  ct-output-set-bss    [* | fragment-list] -> [*]\n"
        "  ct-output-clear-bss  [*] -> [*]\n"
        "    The runtime BSS buffer, which starts as the standard runtime\n"
        "    layout (dstack, rstack, print_buf, persistent, list_capture_*).\n"
        "    Append adds to it; set replaces all of it and clear empties it,\n"
        "    so a replacement must provide those runtime symbols itself.\n"
        "    Compiler-generated BSS (for example bss lists) is always emitted\n"
        "    after the buffer.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 9  EXPRESSION HELPER\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  The infix-to-postfix converter is not a built-in: libs/fn.sl\n"
        "  provides it as fn-infix>postfix (+ - below * / %, all\n"
        "  left-associative, parentheses grouped; it checks only that elements\n"
        "  are strings and parentheses balance).\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 10  LOOP INDEX\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  i                   [*] -> [* | index]\n"
        "    Push the current iteration index (0-based) of the\n"
        "    innermost compile-time for loop.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 11  ASSERTIONS & ERRORS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  static-assert       [* | condition] -> [*]\n"
        "    The condition must be a boolean or an integer (other types are a\n"
        "    type error). False or zero raises static-assertion-failed\n"
        "    (ct_execution_error) at the call site.\n"
        "\n"
        "  ct-parser-error         [* | message] -> (aborts)\n"
        "    Abort compilation with the given error message. The error is\n"
        "    located at the parser's current input (the user code being\n"
        "    processed); the raising CT code is shown as a related note.\n"
        "\n"
        "  ct-parser-error-at      [* | message line column] -> (aborts)\n"
        "    Like ct-parser-error, at a 1-based line and column of the file\n"
        "    the parser is currently reading.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 11A  DIAGNOSTICS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Every compiler error is a Diagnostic: an immutable value with a\n"
        "  severity (error, warning, or note), a stable code, a category\n"
        "  (errors only, lowercase snake_case), an optional phase, a message,\n"
        "  a primary SourceOrigin, and ordered related records. Codes and\n"
        "  categories are stable; message text is not. Built-in codes map to\n"
        "  categories, for example lexer-unterminated-string -> lexer_error,\n"
        "  import-unresolved -> import_error, static-assertion-failed ->\n"
        "  ct_execution_error, emission-unknown-word -> emission_error.\n"
        "\n"
        "  diagnostic-new [*, severity, code, category, phase, message, primary | related] -> [* | diagnostic]\n"
        "    Build a Diagnostic. An error needs a category; a warning or note\n"
        "    needs nil. phase is a string or nil, primary a SourceOrigin, and\n"
        "    related a list of maps with keys kind, optional message, origin.\n"
        "\n"
        "  diagnostic-severity [* | diagnostic] -> [* | severity]\n"
        "  diagnostic-code     [* | diagnostic] -> [* | code]\n"
        "  diagnostic-category [* | diagnostic] -> [* | category-or-nil]\n"
        "  diagnostic-phase    [* | diagnostic] -> [* | phase-or-nil]\n"
        "  diagnostic-message  [* | diagnostic] -> [* | message]\n"
        "  diagnostic-primary  [* | diagnostic] -> [* | origin]\n"
        "  diagnostic-related  [* | diagnostic] -> [* | records]\n"
        "    Read one field. related returns fresh maps.\n"
        "\n"
        "  ct-diagnostic-raise [* | diagnostic] -> (raises)\n"
        "    Raise an error Diagnostic through normal error handling, keeping\n"
        "    all its fields. Custom frontends report syntax errors this way.\n"
        "\n"
        "  ct-diagnostic-error    [* | message] -> (raises)\n"
        "  ct-diagnostic-error-at [* | message line column] -> (raises)\n"
        "    Raise ct-diagnostic-raised (category ct_diagnostic) at the\n"
        "    parser's current input, or at the given 1-based line and column\n"
        "    of the file being read; the raising CT code is a related note.\n"
        "\n"
        "  ct-diagnostic-warning [* | message] -> [*]\n"
        "  ct-diagnostic-note    [* | message] -> [*]\n"
        "    Report at the calling CT code's location. The warning and\n"
        "    note are recorded (codes ct-warning-raised, ct-note-raised) and\n"
        "    published as compile.diagnostic events.\n"
        "\n"
        "  ct-diagnostic-list  [*] -> [* | maps]\n"
        "    The recorded diagnostics in order, as maps with keys severity,\n"
        "    code, category, phase, message, primary, and related.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 11B  DEFINITIONS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Definitions share one global namespace. A name keeps every\n"
        "  definition registered for it (its candidates). The selected\n"
        "  candidate has the highest priority; equal priorities go to the\n"
        "  newest registration. `priority <int>` sets the next definition's\n"
        "  priority (default 0, no bounds). An equal-priority redefinition\n"
        "  warns; a lower one is kept as a shadowed fallback with a note.\n"
        "  Removing the selected candidate reveals the next one. Built-in\n"
        "  handlers (compile-time, runtime-mode CT, and emission intrinsics)\n"
        "  are per-name baselines, not candidates, so a built-in-only name\n"
        "  has no candidates.\n"
        "\n"
        "  A source definition becomes a candidate only when its body is\n"
        "  complete; CT code cannot see it while it is being parsed. A CT\n"
        "  call keeps the candidate it started with; later lookups see the\n"
        "  current selection. CT invocation and runtime emission both use the\n"
        "  selected candidate: a selected runtime-only candidate cannot run\n"
        "  at compile time and never falls back to a lower one. Emission takes\n"
        "  one snapshot of the selected definitions per attempt.\n"
        "\n"
        "  Each candidate has an opaque DefinitionHandle. Copies compare\n"
        "  equal, handles have no order and are not map keys, and a removed\n"
        "  candidate's handle goes stale forever. With a stale handle,\n"
        "  getters push nil 0 and setters push 0. Name-based reads use the\n"
        "  selected candidate; name-based changes need exactly one unowned\n"
        "  candidate (else 0). Candidates owned by a language pack can be\n"
        "  read but not changed by these words (definition-owned).\n"
        "  Argument errors use category definition_error.\n"
        "\n"
        "  ct-definition-candidates [* | name] -> [* | records]\n"
        "    Immutable DefinitionCandidate maps, highest priority first then\n"
        "    newest, with keys handle, name, owner, eligible?, priority,\n"
        "    selected?, kind (l2, assembly, external, macro, python),\n"
        "    source-origin, immediate?, compile-only?, runtime-only?,\n"
        "    inline?, preserve?, prefer-l2?, compile-time-intrinsic?,\n"
        "    runtime-intrinsic?, and emission-intrinsic?.\n"
        "\n"
        "  ct-definition-exists?            [* | name] -> [* | flag]\n"
        "  ct-definition-list-names         [*] -> [* | names]\n"
        "  ct-definition-list-names-prefix  [* | prefix] -> [* | names]\n"
        "    Names backed by a candidate or a built-in, each once, sorted.\n"
        "\n"
        "  ct-definition-get          [* | handle] -> [*, record | found]\n"
        "  ct-definition-get-by-name  [* | name] -> [*, record | found]\n"
        "  ct-definition-body             [* | handle] -> [*, body | found]\n"
        "  ct-definition-body-by-name     [* | name] -> [*, body | found]\n"
        "  ct-definition-asm              [* | handle] -> [*, asm | found]\n"
        "  ct-definition-asm-by-name      [* | name] -> [*, asm | found]\n"
        "  ct-definition-source           [* | handle] -> [*, source | found]\n"
        "  ct-definition-source-by-name   [* | name] -> [*, source | found]\n"
        "    Detached snapshots. body is a word's op maps (or a text macro's\n"
        "    expansion); asm is an :asm body; source is the declaration's\n"
        "    exact original text, kept across body edits and by clones, and\n"
        "    unavailable for generated or built-in definitions.\n"
        "\n"
        "  ct-definition-set-priority          [*, handle | priority] -> [* | ok]\n"
        "  ct-definition-set-priority-by-name  [*, name | priority] -> [* | ok]\n"
        "  ct-definition-set-flags             [*, handle | patch] -> [* | ok]\n"
        "  ct-definition-set-flags-by-name     [*, name | patch] -> [* | ok]\n"
        "  ct-definition-set-asm               [*, handle | source] -> [* | ok]\n"
        "  ct-definition-set-asm-by-name       [*, name | source] -> [* | ok]\n"
        "    A flag patch holds any of immediate, compile-only, runtime-only,\n"
        "    inline, preserve. set-asm needs an assembly candidate. Unchanged\n"
        "    values succeed silently; invalid values change nothing.\n"
        "\n"
        "  ct-definition-rename          [*, handle | new-name] -> [* | ok]\n"
        "  ct-definition-rename-by-name  [*, name | new-name] -> [* | ok]\n"
        "    Move one candidate to another name, where it becomes the newest\n"
        "    registration; its handle is kept.\n"
        "\n"
        "  ct-definition-clone          [*, handle | new-name] -> [*, handle | created]\n"
        "  ct-definition-clone-by-name  [*, name | new-name] -> [*, handle | created]\n"
        "    Copy a candidate under a name with its priority, a new handle,\n"
        "    and a new registration position. Built-in handlers are not copied.\n"
        "\n"
        "  ct-definition-unregister          [* | handle] -> [* | removed]\n"
        "  ct-definition-unregister-by-name  [* | name] -> [* | removed]\n"
        "\n"
        "  ct-definition-prefer-l2          [* | handle] -> [* | ok]\n"
        "  ct-definition-prefer-l2-by-name  [* | name] -> [* | ok]\n"
        "    Mark a candidate to run its L2 body at compile time instead of\n"
        "    the name's compile-time intrinsic while it is selected. The\n"
        "    by-name form marks the selected candidate.\n"
        "\n"
        "  ct-definition-register-source [* | source] -> [* | handles]\n"
        "    Parse a fragment of word and :asm definitions and register\n"
        "    them in order, returning their handles. Nothing is registered\n"
        "    when the fragment fails to parse. Needs an active compilation.\n"
        "\n"
        "  ct-definition-predeclare-all  [*] -> [*]            [immediate]\n"
        "    Register every later top-level `word` in the parse stream now,\n"
        "    so CT code can call them early. When parsing reaches one, it\n"
        "    completes the same candidate.\n"
        "\n"
        "  Events: definition.candidates.changed (operation: register,\n"
        "  unregister, rename, set_priority, clone, update) carries the\n"
        "  candidate handle, candidate-before/candidate-after records, and\n"
        "  the previous and current selected handles. It is followed by\n"
        "  definition.selection.changed (name, previous-handle,\n"
        "  selected-handle, cause) for each name whose selection changed.\n"
        "  A source definition publishes them after word.compile.end.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 12  EVAL\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  eval                [* | source-string] -> [*]\n"
        "    Parse and execute a string of L2 code in the CT VM.\n"
        "    The string is tokenized, parsed as if it were part of\n"
        "    a definition body, and the resulting ops are executed\n"
        "    immediately.\n"
        "\n"
        "      \"3 4 +\" eval   # pushes 7 onto the CT stack\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 13  MACROS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        + _L2_MACRO_CT_BLOCK
        + "  ct-macro-context  [*] -> [* | map]\n"
        "    While a macro body runs (also inside helpers it calls): a fresh\n"
        "    map with macro, definition-origin, call-site, and expansion-trace\n"
        "    (maps with the same three keys, outermost first). Elsewhere it is\n"
        "    macro-context-unavailable.\n"
        "\n"
        "  ct-macro-get          [* | handle] -> [*, record | found]\n"
        "  ct-macro-get-by-name  [* | name] -> [*, record | found]\n"
        "    A detached record with parameters (list of names), body (the\n"
        "    body's Syntax), and metadata (a user map); nil 0 when the\n"
        "    candidate is stale or not a macro.\n"
        "\n"
        "  ct-macro-set          [*, handle | patch] -> [* | ok]\n"
        "  ct-macro-set-by-name  [*, name | patch] -> [* | ok]\n"
        "    Update any of parameters, body, and metadata in place (handle,\n"
        "    priority, and position kept). The patch is validated and the\n"
        "    body parsed before anything changes; the body is not run. A\n"
        "    change publishes a definition update event. The by-name form\n"
        "    needs exactly one unowned candidate.\n"
        "\n"
        "  ct-definition-set-body          [*, handle | syntax] -> [* | ok]\n"
        "  ct-definition-set-body-by-name  [*, name | syntax] -> [* | ok]\n"
        "    Replace an L2 word's body with Syntax parsed as a body (not run);\n"
        "    flags and source provenance are kept. Calls already running keep\n"
        "    the old body. Macros use ct-macro-set.\n"
        "\n"
        "  ct-lang-register-macro  [*, language | name] -> [* | ok]\n"
        "    Adopt the selected unowned macro into a language pack; it is\n"
        "    then eligible only while the pack is active. 0 when there is no\n"
        "    such macro.\n"
        "\n"
        "  Events: each finished expansion publishes macro.expansion with\n"
        "  macro, call-site, depth (0 at top level), attempt, and status;\n"
        "  success adds the emitted tokens, failure the diagnostic code and\n"
        "  phase. Nothing is built unless something subscribes.\n"
        "\n"
        + "  :py { ... }\n"
        "    Embed a Python code block that runs at compile time.\n"
        "    The block receives a `ctx` (MacroContext) variable and\n"
        "    can call ctx.emit(), ctx.next_token(), etc.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 14  STRUCT & CSTRUCT\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  struct <name> field <name> <size> ... end\n"
        "    Define a simple struct with manually-sized fields.\n"
        "    Generates accessor words:\n"
        "      <struct>.size           — total byte size\n"
        "      <struct>.<field>.offset — byte offset\n"
        "      <struct>.<field>.size   — field byte size\n"
        "      <struct>.<field>@       — read field (qword)\n"
        "      <struct>.<field>!       — write field (qword)\n"
        "\n"
        "  cstruct <name> cfield <name> <type> ... end\n"
        "    Define a C-compatible struct with automatic alignment\n"
        "    and padding. Field types use C names (int, long, char*,\n"
        "    struct <name>*, etc.). Generates the same accessors as\n"
        "    struct plus <struct>.align.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 15  FLOW CONTROL LABELS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  label <name>                             [immediate]\n"
        "    Emit a named label at the current position in the word\n"
        "    body. Can be targeted by `goto`.\n"
        "\n"
        "  goto <name>                              [immediate]\n"
        "    Emit an unconditional jump to the named label.\n"
        "\n"
        "  here                                     [immediate]\n"
        "    Push a \"file:line:col\" string literal for the current\n"
        "    source location. Useful for error messages and debugging.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 16  WITH (SCOPED VARIABLES)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  with <names...> in <body> end\n"
        "    Pop values from the stack into named local variables.\n"
        "    Inside the body, referencing a name reads the variable;\n"
        "    `name !` writes to it. Variables are backed by hidden\n"
        "    globals and are NOT re-entrant.\n"
        "\n"
        "      10 20 with x y in\n"
        "        x y +   # reads x (10) and y (20), adds -> 30\n"
        "      end\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 17  ADDITIONAL CT STACK EFFECTS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  These signatures make callback, stateful, and dual-mode CT APIs\n"
        "  explicit. `*` denotes an unchanged deeper stack. Values marked\n"
        "  `found` are returned as a flag after the associated value.\n"
        "\n"
        "  CT  [*] -> [* | flag]\n"
        "    Push 1 in CT execution and 0 in emitted runtime code.\n"
        "\n"
        "  ct-parser-set-token-hook  [* | name] -> [*]\n"
        "  ct-parser-clear-token-hook  [*] -> [*]\n"
        "    Install/remove the global token hook (§ 1).\n"
        "\n"
        "  ── Language packs\n"
        "\n"
        "  A pack owns token hooks, word and macro candidates, and rewrite\n"
        "  rules. They take part in parsing only while the pack is active;\n"
        "  registering does not activate. Pack names are exact strings; an\n"
        "  unknown pack is an error except for the queries noted below.\n"
        "\n"
        "  ct-lang-create      [* | name] -> [* | created]\n"
        "  ct-lang-remove      [* | name] -> [* | removed]\n"
        "    Create an inactive pack (0 if it exists); remove a pack and all\n"
        "    its resources (0 if absent).\n"
        "\n"
        "  ct-lang-activate    [* | name] -> [* | changed]\n"
        "  ct-lang-deactivate  [* | name] -> [* | changed]\n"
        "    Turn the pack's resources on or off; 0 when already in that state.\n"
        "\n"
        "  ct-lang-exists?     [* | name] -> [* | flag]\n"
        "  ct-lang-active?     [* | name] -> [* | flag]\n"
        "  ct-lang-list        [*] -> [* | names]\n"
        "  ct-lang-list-active [*] -> [* | names]\n"
        "    Queries; 0 for an unknown pack. Lists are sorted.\n"
        "\n"
        "  ct-lang-set-meta    [*, name | metadata] -> [* | ok]\n"
        "  ct-lang-get-meta    [* | name] -> [*, metadata | found]\n"
        "    Store/read a detached string-keyed map of portable data (nil,\n"
        "    integers, numbers, strings, lists, maps); nil clears it.\n"
        "\n"
        "  ct-lang-status      [* | name] -> [*, status | found]\n"
        "    Map with name, active, metadata, token_hooks (handle, handler,\n"
        "    priority, enabled?, active?; dispatch order), words, macros,\n"
        "    reader_rewrites and grammar_rewrites ({handle, name}).\n"
        "\n"
        "  ct-lang-add-token-hook          [*, name | handler] -> [* | handle]\n"
        "  ct-lang-set-token-hook-priority [*, handle | priority] -> [* | found]\n"
        "  ct-lang-set-token-hook-enabled  [*, handle | flag] -> [* | found]\n"
        "  ct-lang-remove-token-hook       [* | handle] -> [* | removed]\n"
        "    Pack hooks run while their pack is active and they are enabled:\n"
        "    higher priority first, later registration first on ties. The\n"
        "    handler name is resolved at each dispatch. Tokens injected by\n"
        "    several hooks keep dispatch order.\n"
        "\n"
        "  ct-lang-register-word           [*, name | word] -> [* | ok]\n"
        "  ct-lang-set-resource-priority   [*, name, resource | priority] -> [* | found]\n"
        "  ct-lang-get-resource-priority   [*, name | resource] -> [*, priority | found]\n"
        "  ct-lang-unregister-resource     [*, name | resource] -> [* | removed]\n"
        "    Adopt the selected unowned word into the pack (replacing the\n"
        "    pack's earlier candidate of that name), and manage owned words\n"
        "    and macros. Generic definition words cannot change them.\n"
        "\n"
        "  ct-lang-add-reader-rewrite-named  [*, name, rule, pattern | replacement] -> [* | handle]\n"
        "  ct-lang-add-grammar-rewrite-named [*, name, rule, pattern | replacement] -> [* | handle]\n"
        "    Register a pack-owned rewrite (§ 6); it matches while the pack is\n"
        "    active and the rule is enabled.\n"
        "\n"
        "  repr  [* | value] -> [* | text]\n"
        "    A readable debug representation of any CT value. Not a stable\n"
        "    format: do not parse it or base decisions on it.\n"
        "\n"
        "  Source of a definition: ct-definition-source-by-name gives the\n"
        "  selected definition's exact source; meta.sl's meta-runtime-get-source\n"
        "  returns it with its UTF-8 byte length at compile time and, at\n"
        "  runtime, from the catalog --source-embed adds (\"\" 0 / 0 0 when\n"
        "  unavailable; shadowed definitions are never returned).\n"
        "\n"
    )

    _ct_ref_bundle_entries: List[Dict[str, Any]] = []
    _ct_ref_summary_text = ""
    _ct_ref_appendix_text = ""

    _docs_helpers = _load_docs_helpers(warn=True)
    if (
        _docs_helpers is not None
        and hasattr(_docs_helpers, "build_ct_reference_bundle")
        and hasattr(_docs_helpers, "attach_ct_entry_line_numbers")
    ):
        try:
            _ct_ref_bundle = _docs_helpers.build_ct_reference_bundle(
                _L2_CT_REF_TEXT,
                _collect_ct_word_metadata(),
            )
            _ct_ref_summary_text = str(_ct_ref_bundle.get("summary_text", ""))
            _ct_ref_appendix_text = str(_ct_ref_bundle.get("appendix_text", ""))
            _ct_ref_bundle_entries = [dict(item) for item in _ct_ref_bundle.get("entries", [])]
        except Exception as exc:
            sys.stderr.write(f"[warn] docs.py bundle generation failed ({exc}); using built-in fallback\n")
            _ct_ref_summary_text = _build_ct_ref_complete_summary_table(_L2_CT_REF_TEXT)
            _ct_ref_appendix_text = _build_ct_ref_function_appendix(_L2_CT_REF_TEXT)
    else:
        _ct_ref_summary_text = _build_ct_ref_complete_summary_table(_L2_CT_REF_TEXT)
        _ct_ref_appendix_text = _build_ct_ref_function_appendix(_L2_CT_REF_TEXT)

    _L2_CT_REF_FULL_TEXT = _L2_CT_REF_TEXT + _ct_ref_summary_text + _ct_ref_appendix_text

    if _docs_helpers is not None and _ct_ref_bundle_entries:
        try:
            _ct_ref_bundle_entries = [
                dict(item)
                for item in _docs_helpers.attach_ct_entry_line_numbers(
                    _L2_CT_REF_FULL_TEXT,
                    _ct_ref_bundle_entries,
                )
            ]
        except Exception as exc:
            sys.stderr.write(f"[warn] docs.py line mapping failed ({exc}); search will use fallback extraction\n")
            _ct_ref_bundle_entries = []

    _L2_QA_TEXT = (
        "═══════════════════════════════════════════════════════════\n"
        "              Q & A   /   T I P S   &   T R I C K S\n"
        "═══════════════════════════════════════════════════════════\n"
        "\n"
        "  HOW DO I DEBUG AN L2 PROGRAM?\n"
        "\n"
        "    Compile with --debug to embed DWARF debug info, then\n"
        "    launch with --dbg to drop straight into GDB:\n"
        "\n"
        "      python3 main.py my_program.sl --debug --dbg\n"
        "\n"
        "    Inside GDB you can:\n"
        "      - Set breakpoints on word labels  (b w_main)\n"
        "      - Inspect the data stack via r12  (x/8gx $r12)\n"
        "      - Step through asm instructions   (si / ni)\n"
        "      - View registers                  (info registers)\n"
        "      - Disassemble a word              (disas w_foo)\n"
        "\n"
        "    Tip: r12 is the stack pointer. [r12] = TOS,\n"
        "    [r12+8] = second element, etc.\n"
        "\n"
        "  HOW DO I VIEW THE GENERATED ASSEMBLY?\n"
        "\n"
        "    Use --emit-asm to stop after generating assembly:\n"
        "\n"
        "      python3 main.py my_program.sl --emit-asm\n"
        "\n"
        "    The .asm file is written to build/<name>.asm.\n"
        "    You can also use -v1 or higher for timing info,\n"
        "    -v2 for per-function details, and -v3 or -v4 for\n"
        "    full optimization tracing.\n"
        "\n"
        "  HOW DO I EXPOSE L2 FUNCTIONS TO C?\n"
        "\n"
        "    Build a shared, static, or object artifact with --c-abi:\n"
        "\n"
        "      python3 main.py library.sl --artifact shared --c-abi\n"
        "\n"
        "    Every runtime function gets an l2_<name> wrapper; no\n"
        "    export marker or stack-effect comment is needed. C names\n"
        "    preserve ordinary identifiers, replace punctuation with\n"
        "    underscores, and add a unique suffix when names collide.\n"
        "\n"
        "    Each wrapper has the form int64_t l2_name(size_t argc, ...).\n"
        "    Pass argc int64_t variadic arguments after the count. The\n"
        "    wrapper pushes them in order onto the L2 data stack, calls\n"
        "    the function, and returns the top stack value as int64_t.\n"
        "    The function must leave a result on the stack. This is the\n"
        "    x86-64 System V ABI; it is not a Windows x64 interface.\n"
        "\n"
        "  HOW DO I CALL C FUNCTIONS?\n"
        "\n"
        "    Declare them with the C-style extern syntax:\n"
        "\n"
        "      extern int printf(const char* fmt, ...)\n"
        "      extern void* malloc(size_t size)\n"
        "\n"
        "    Or use the legacy style:\n"
        "\n"
        "      extern printf 2 1\n"
        "\n"
        "    Link the library with -l:\n"
        "\n"
        "      python3 main.py my_program.sl -l c\n"
        "\n"
        "    You can also use cimport to auto-extract externs:\n"
        "\n"
        "      cimport \"my_header.h\"\n"
        "\n"
        "  HOW DO MACROS WORK?\n"
        "\n"
        + _L2_MACRO_QA_BLOCK
        + "  WHAT IS THE L2 DATA MODEL FOR ARRAYS/STRINGS?\n"
        "\n"
        "    - [ ... ] creates a heap list at runtime. Layout is:\n"
        "      [len, elem0, elem1, ...]. Free it with arr_free.\n"
        "    - { ... } creates a fixed-size BSS-backed list.\n"
        "    - { ... }:N creates BSS storage of N elements, copying\n"
        "      initializers then zero-filling the rest.\n"
        "    - String literals are emitted to static data and\n"
        "      deduplicated by default (--no-string-dedup disables).\n"
        "    - stdlib/dyn_arr.sl provides growable dynamic arrays\n"
        "      with [len, cap, data_ptr] style metadata.\n"
        "\n"
        "  HOW DO I RUN CODE AT COMPILE TIME?\n"
        "\n"
        "    Use --ct-run-main or --script to execute 'main' at\n"
        "    compile time. The CT VM supports most stack ops, I/O,\n"
        "    lists, hashmaps, and string manipulation.\n"
        "    Use --leak-check to run in CTVM with mmap/munmap\n"
        "    interception and leak reporting (non-zero exit on leaks).\n"
        "\n"
        "    You can also invoke an already-defined word during\n"
        "    compilation with the compile-time directive:\n"
        "\n"
        "      word generate-table\n"
        "        # ... runs during compilation\n"
        "      end\n"
        "      compile-time generate-table\n"
        "\n"
        "  WHAT IS THE --SCRIPT FLAG?\n"
        "\n"
        "    Shorthand for --no-artifact --ct-run-main. It parses\n"
        "    and runs 'main' in the compile-time VM without\n"
        "    producing a binary — useful for scripts as the name suggests.\n"
        "\n"
        "  HOW DO I USE THE BUILD CACHE?\n"
        "\n"
        "    The cache is automatic. It stores assembly output\n"
        "    and skips recompilation when source files haven't\n"
        "    changed. Disable with --no-cache if needed.\n"
        "\n"
        "  HOW DO I DO A CHECK-ONLY BUILD?\n"
        "\n"
        "    Use --check to parse/compile/validate without emitting\n"
        "    final artifacts. This is equivalent to enabling\n"
        "    --no-artifact after successful compilation.\n"
        "\n"
        "      python3 main.py prog.sl --check\n"
        "\n"
        "  HOW DO CONDITIONAL DIRECTIVES WORK?\n"
        "\n"
        "    Use ifdef/ifndef/elsedef/endif in source and pass\n"
        "    -D NAME (repeatable) on the CLI, or add `define NAME`\n"
        "    in source. Directives are\n"
        "    resolved during preprocessing before tokenization.\n"
        "\n"
        "      ifdef DEBUG\n"
        "        \"debug path\" puts\n"
        "      elsedef\n"
        "        \"release path\" puts\n"
        "      endif\n"
        "\n"
        "  HOW DO I CONTROL WARNINGS?\n"
        "\n"
        "    Enable categories with -W (repeatable), and promote\n"
        "    warnings to errors with --Werror.\n"
        "\n"
        "      python3 main.py prog.sl -W redefine -W stack-depth\n"
        "      python3 main.py prog.sl -W all --Werror\n"
        "\n"
        "  HOW DO I DUMP THE CONTROL-FLOW GRAPH?\n"
        "\n"
        "    Use --dump-cfg to produce a Graphviz DOT file:\n"
        "\n"
        "      python3 main.py prog.sl --dump-cfg\n"
        "      dot -Tpng build/prog.cfg.dot -o cfg.png\n"
        "\n"
        "  WHAT OPTIMIZATIONS DOES L2 PERFORM?\n"
        "\n"
        "    - Constant folding (--no-folding to disable)\n"
        "    - Peephole optimization (--no-peephole)\n"
        "    - Loop unrolling (--no-loop-unroll)\n"
        "    - Auto-inlining of small asm bodies (--no-auto-inline)\n"
        "    - String literal deduplication (--no-string-dedup to disable)\n"
        "    - Dead code elimination (--no-dce to keep every word)\n"
        "    - -O0 disables all optimizations\n"
        "    - -O2 disables all optimizations AND checks\n"
        "\n"
        "══════════════════════════════════════════════════════════\n"
    )

    _L2_HOW_TEXT = (
        "═══════════════════════════════════════════════════════════════\n"
        "          H O W   L 2   W O R K S   (I N T E R N A L S)\n"
        "═══════════════════════════════════════════════════════════════\n"
        "\n"
        "  ARCHITECTURE OVERVIEW\n"
        "\n"
        "    The command-line path has a client/coordinator layer and\n"
        "    a separate source-to-binary compiler pipeline.\n"
        "\n"
        "    1. CLI CLIENT AND DAEMON COORDINATOR\n"
        "       `main.py` is the normal CLI client. It starts the\n"
        "       background compiler on demand and sends requests over a\n"
        "       local Unix socket. `l2_main.py --daemon-serve` owns the\n"
        "       bounded request queue, worker pool, compilation history,\n"
        "       cache state, and event subscriptions. Use `--no-daemon`\n"
        "       or `L2_DAEMON=0` to run the compiler directly.\n"
        "\n"
        "    2. ISOLATED COMPILER WORKERS\n"
        "       Persistent worker processes accept compiler requests;\n"
        "       each request runs in a forked child with captured stdio\n"
        "       and forwarded events. The client PATH is passed per\n"
        "       request so tools installed after daemon startup, such as\n"
        "       NASM, remain discoverable. The daemon can watch compiler\n"
        "       sources and reload after active requests drain.\n"
        "\n"
        "    3. SOURCE LOADING AND PREPROCESSING\n"
        "       Imports and C headers are resolved recursively before\n"
        "       tokenization. Source spans retain file/line locations\n"
        "       across the combined module.\n"
        "\n"
        "    4. TOKENIZER, PARSER, AND COMPILE-TIME VM\n"
        "       The tokenizer tracks source locations. The parser\n"
        "       expands macros and builds per-word IR while CT words,\n"
        "       hooks, and event listeners can inspect or transform\n"
        "       compilation state.\n"
        "\n"
        "    5. OPTIMIZATION AND CODE GENERATION\n"
        "       The compiler optimizes the word IR and emits NASM\n"
        "       x86-64 assembly, including the System V C FFI and\n"
        "       optional --c-abi wrappers. `--emit-asm` stops here.\n"
        "\n"
        "    6. EXTERNAL TOOLCHAIN\n"
        "       NASM assembles the generated source into an object;\n"
        "       the platform linker produces the requested executable,\n"
        "       shared library, static library, or object artifact.\n"
        "       Tool lookup uses the PATH supplied by the current CLI\n"
        "       request, not only the daemon's startup environment.\n"
        "\n"
        "  CONFORMANCE NOTES\n"
        "\n"
        "    - A program is considered conforming when accepted by\n"
        "      the reference parser/preprocessor in this repository.\n"
        "    - Runtime behavior is defined by emitted x86-64 code\n"
        "      plus imported stdlib words.\n"
        "    - If docs and implementation diverge, implementation\n"
        "      behavior is authoritative until docs are updated.\n"
        "\n"
        "───────────────────────────────────────────────────────────────\n"
        "\n"
        "  THE STACKS\n"
        "\n"
        "    L2 uses register r12 as the stack pointer for its data\n"
        "    stack. The stack grows downward:\n"
        "\n"
        "      push:  sub r12, 8; mov [r12], rax\n"
        "      pop:   mov rax, [r12]; add r12, 8\n"
        "\n"
        "    The return stack lives in a separate buffer with r13 as\n"
        "    its stack pointer (also grows downward). The native x86\n"
        "    call/ret stack (rsp) is used only for word call/return\n"
        "    linkage and C interop.\n"
        "\n"
        "───────────────────────────────────────────────────────────────\n"
        "\n"
        "  THE COMPILE-TIME VM\n"
        "\n"
        "    The CT VM is a stack-based interpreter that runs during\n"
        "    parsing. It maintains:\n"
        "\n"
        "      - A value stack\n"
        "      - A dictionary of CT-callable words\n"
        "      - A return stack for nested calls\n"
        "\n"
        "    CT words can:\n"
        "      - Emit token sequences into the compiler's stream\n"
        "      - Inspect/modify the parser state\n"
        "      - Call other CT words or builtins\n"
        "      - Perform I/O, string ops, list/hashmap manipulation\n"
        "\n"
        "    When --ct-run-main is used, the CT VM can also JIT-compile\n"
        "    and execute native x86-64 code via the Keystone assembler\n"
        "    engine (for words that need near native performance).\n"
        "\n"
        "───────────────────────────────────────────────────────────────\n"
        "\n"
        "  OPTIMIZATION PASSES\n"
        "\n"
        "    CONSTANT FOLDING\n"
        "      Evaluates pure arithmetic sequences (e.g., 3 4 +\n"
        "      becomes push 7). Works across word boundaries for\n"
        "      inlined words.\n"
        "\n"
        "    PEEPHOLE OPTIMIZATION\n"
        "      Pattern-matches instruction sequences and\n"
        "      replaces them with shorter equivalents. Examples:\n"
        "        swap drop -> nip\n"
        "        swap nip  -> drop\n"
        "\n"
        "    LOOP UNROLLING\n"
        "      Small deterministic loops (e.g., '4 for ... end')\n"
        "      are unrolled into straight-line code when the\n"
        "      iteration count is known at compile time.\n"
        "\n"
        "    AUTO-INLINING\n"
        "      Small asm-body words (below a size threshold) are\n"
        "      automatically inlined at call sites, eliminating\n"
        "      call/ret overhead.\n"
        "\n"
        "    LIST LITERAL LOWERING\n"
        "      [ ... ] literals are always heap-backed and\n"
        "      allocated at runtime.\n"
        "      { ... } literals are fixed-size BSS-backed\n"
        "      arrays; with { ... }:N, trailing elements are\n"
        "      zero-initialized up to N.\n"
        "\n"
        "    DATA LAYOUTS\n"
        "      Heap list layout: [len, elem0, elem1, ...]\n"
        "      BSS list layout:  [len, elem0, elem1, ...]\n"
        "      Dynamic array layout (stdlib/dyn_arr.sl):\n"
        "        [len, cap, data_ptr, inline_elems...]\n"
        "      String literals point at static data bytes\n"
        "      (deduplicated unless --no-string-dedup).\n"
        "\n"
        "    DEAD CODE ELIMINATION\n"
        "      Words that are never called (and not 'main') are\n"
        "      excluded from the final assembly output.\n"
        "\n"
        "───────────────────────────────────────────────────────────────\n"
        "\n"
        "  EXTERN C FFI\n"
        "\n"
        "    L2's extern system supports the full System V AMD64 ABI:\n"
        "\n"
        "    - Integer args -> rdi, rsi, rdx, rcx, r8, r9, then stack\n"
        "    - Float/double args -> xmm0..xmm7, then stack\n"
        "    - Struct args classified per ABI eightbyte rules\n"
        "    - Return values in rax (int), xmm0 (float), or via\n"
        "      hidden sret pointer for large structs\n"
        "    - RSP is aligned to 16 bytes before each call\n"
        "\n"
        "    The compiler auto-classifies argument types from the\n"
        "    C-style declaration and generates the correct register\n"
        "    shuffle and stack layout.\n"
        "\n"
        "───────────────────────────────────────────────────────────────\n"
        "\n"
        "  QUIRKS & GOTCHAS\n"
        "\n"
        "    - No type system: everything is a 64-bit integer on\n"
        "      the stack. Pointers, booleans, characters — all\n"
        "      just numbers. Type safety is your responsibility.\n"
        "\n"
        "    - and / or / not are logical (nonzero is true, result\n"
        "      0 or 1). Bitwise: band bor bxor bnot shl shr sar.\n"
        "\n"
        "    - Integer division by zero (and INT64_MIN -1 /) raises\n"
        "      SIGFPE at runtime, as in C. At compile time it is a\n"
        "      diagnostic.\n"
        "\n"
        "    - Macro expansion depth: macros can expand macros,\n"
        "      but there's a limit (default 256, configurable via\n"
        "      --macro-expansion-limit).\n"
        "      Use --macro-preview to print each macro expansion and\n"
        "      rewrite with its file:line:column (rewrites also show the\n"
        "      surrounding source), and --preview to print the final\n"
        "      transformed source.\n"
        "\n"
        "    - :py blocks: Python code embedded in :py { ... }\n"
        "      runs in the compiler's Python process. It has full\n"
        "      access to the parser and dictionary — powerful but\n"
        "      dangerous.\n"
        "\n"
        "    - The CT VM and native codegen share a dictionary\n"
        "      but have separate stacks. A word defined at CT\n"
        "      exists at CT only unless also compiled normally.\n"
        "\n"
        "    - The build cache tracks file mtimes and a hash of\n"
        "      compiler flags. CT side effects invalidate the\n"
        "      cache for that file.\n"
        "\n"
        "    - Unsafe and implementation-defined behavior is\n"
        "      intentional: raw memory access, inline asm, syscalls,\n"
        "      external calls, exact label layout, and optimization\n"
        "      interactions are programmer-visible and may vary with\n"
        "      flags and code shape.\n"
        "\n"
        "═══════════════════════════════════════════════════════════════\n"
    )

    def _safe_addnstr(scr: Any, y: int, x: int, text: str, maxlen: int, attr: int = 0) -> None:
        h, w = scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        maxlen = min(maxlen, w - x)
        if maxlen <= 0:
            return
        try:
            scr.addnstr(y, x, text, maxlen, attr)
        except curses.error:
            pass

    def _build_detail_lines(entry: DocEntry, width: int) -> List[str]:
        lines: List[str] = []
        lines.append(f"{'Name:':<14} {entry.name}")
        lines.append(f"{'Kind:':<14} {entry.kind}")
        if entry.stack_effect:
            lines.append(f"{'Stack effect:':<14} {entry.stack_effect}")
        else:
            lines.append(f"{'Stack effect:':<14} (none)")
        lines.append(f"{'File:':<14} {entry.path}:{entry.line}")
        lines.append("")
        if entry.description:
            lines.append("Description:")
            # Word-wrap description
            words = entry.description.split()
            current: List[str] = []
            col = 2  # indent
            for w in words:
                if current and col + 1 + len(w) > width - 2:
                    lines.append("  " + " ".join(current))
                    current = [w]
                    col = 2 + len(w)
                else:
                    current.append(w)
                    col += 1 + len(w) if current else len(w)
            if current:
                lines.append("  " + " ".join(current))
        else:
            lines.append("(no description)")
        lines.append("")
        # Show source context
        lines.append("Source context:")
        try:
            src_lines = entry.path.read_text(encoding="utf-8", errors="ignore").splitlines()
            start = max(0, entry.line - 1)
            if entry.kind == "word":
                # Depth-tracking: word/if/while/for/begin/with open blocks closed by 'end'
                _block_openers = {"word", "if", "while", "for", "begin", "with"}
                depth = 0
                end = min(len(src_lines), start + 200)
                for i in range(start, end):
                    stripped = src_lines[i].strip()
                    # Strip comments (# to end of line, but not inside strings)
                    code = stripped.split("#", 1)[0].strip() if "#" in stripped else stripped
                    # Count all block openers and 'end' tokens on the line
                    for tok in code.split():
                        if tok in _block_openers:
                            depth += 1
                        elif tok == "end":
                            depth -= 1
                    prefix = f"  {i + 1:4d}| "
                    lines.append(prefix + src_lines[i])
                    if depth <= 0 and i > start:
                        break
            elif entry.kind in ("asm", "py"):
                # Show until closing brace + a few extra lines of context
                end = min(len(src_lines), start + 200)
                found_close = False
                extra_after = 0
                for i in range(start, end):
                    prefix = f"  {i + 1:4d}| "
                    lines.append(prefix + src_lines[i])
                    stripped = src_lines[i].strip()
                    if not found_close and stripped in ("}", "};") and i > start:
                        found_close = True
                        extra_after = 0
                        continue
                    if found_close:
                        extra_after += 1
                        if extra_after >= 3 or not stripped:
                            break
            elif entry.kind == "macro":
                # Show macro body until closing ';'
                end = min(len(src_lines), start + 200)
                for i in range(start, end):
                    prefix = f"  {i + 1:4d}| "
                    lines.append(prefix + src_lines[i])
                    stripped = src_lines[i].strip()
                    if stripped.endswith(";") and i >= start:
                        break
            else:
                end = min(len(src_lines), start + 30)
                for i in range(start, end):
                    prefix = f"  {i + 1:4d}| "
                    lines.append(prefix + src_lines[i])
        except Exception:
            lines.append("  (unable to read source)")
        return lines

    def _app(stdscr: Any) -> int:
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.keypad(True)

        # Initialize color pairs for kind tags
        _has_colors = False
        try:
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_CYAN, -1)     # word
                curses.init_pair(2, curses.COLOR_GREEN, -1)    # asm
                curses.init_pair(3, curses.COLOR_YELLOW, -1)   # py
                curses.init_pair(4, curses.COLOR_MAGENTA, -1)  # macro
                _has_colors = True
        except Exception:
            pass

        _KIND_COLORS = {
            "word": curses.color_pair(1) if _has_colors else 0,
            "asm": curses.color_pair(2) if _has_colors else 0,
            "py": curses.color_pair(3) if _has_colors else 0,
            "macro": curses.color_pair(4) if _has_colors else 0,
        }

        nonlocal entries
        query = initial_query
        selected = 0
        scroll = 0
        mode = _MODE_BROWSE
        active_tab = _TAB_LIBRARY

        # Search mode state
        search_buf = query

        # Detail mode state
        detail_scroll = 0
        detail_lines: List[str] = []

        # Language reference state
        lang_selected = 0
        lang_scroll = 0
        lang_cat_filter = 0  # 0 = all
        lang_detail_scroll = 0
        lang_detail_lines: List[str] = []

        # License/philosophy scroll state
        info_scroll = 0
        info_lines: List[str] = []

        # Compile-time reference search state
        ct_ref_all_lines: List[str] = _L2_CT_REF_FULL_TEXT.splitlines()
        ct_ref_query = ""
        ct_ref_search_buf = ""

        ct_ref_sections: List[str] = ["all", "intro"]
        ct_ref_line_sections: List[str] = []
        _ct_section_cur = "intro"
        for _ct_line in ct_ref_all_lines:
            _ct_match = _CT_REF_SECTION_RE.match(_ct_line)
            if _ct_match is not None:
                _ct_section_cur = _ct_match.group(1).strip()
                if _ct_section_cur not in ct_ref_sections:
                    ct_ref_sections.append(_ct_section_cur)
            ct_ref_line_sections.append(_ct_section_cur)
        ct_ref_section_idx = 0
        ct_ref_scope_options = [
            "all",
            "immediate",
            "compile-only",
            "runtime+compile-time",
            "runtime-only",
        ]
        ct_ref_scope_idx = 0
        ct_ref_words_only = False
        ct_ref_filter_field = 0
        ct_ref_result_selected = 0
        ct_ref_result_scroll = 0
        ct_ref_result_entries: List[Dict[str, Any]] = []
        ct_ref_result_query = ""
        ct_ref_result_signature: Tuple[str, str, str] = ("", "all", "all")
        ct_ref_detail_scroll = 0
        ct_ref_detail_lines: List[str] = []
        ct_ref_detail_line_no = 0

        # Filter mode state
        filter_kind_idx = 0  # index into _FILTER_KINDS
        filter_field = 0  # 0=kind, 1=args, 2=returns, 3=show_private, 4=show_macros, 5=extra_path, 6=files
        filter_file_scroll = 0
        filter_file_cursor = 0
        filter_args = -1      # -1 = any
        filter_returns = -1   # -1 = any
        filter_extra_path = ""  # text input for adding paths
        filter_extra_roots: List[str] = []  # accumulated extra paths
        filter_show_private = False
        filter_show_macros = False

        # Build unique file list; all enabled by default
        all_file_paths: List[str] = sorted(set(e.path.as_posix() for e in entries))
        filter_files_enabled: Dict[str, bool] = {p: True for p in all_file_paths}

        def _rebuild_file_list() -> None:
            nonlocal all_file_paths, filter_files_enabled
            new_paths = sorted(set(e.path.as_posix() for e in entries))
            old = filter_files_enabled
            filter_files_enabled = {p: old.get(p, True) for p in new_paths}
            all_file_paths = new_paths

        def _filter_lang_ref() -> List[Dict[str, str]]:
            if lang_cat_filter == 0:
                return list(_LANG_REF_ENTRIES)
            cat = _LANG_REF_CATEGORIES[lang_cat_filter - 1]
            return [e for e in _LANG_REF_ENTRIES if e["category"] == cat]

        def _build_lang_detail_lines(entry: Dict[str, str], width: int) -> List[str]:
            lines: List[str] = []
            lines.append(f"{'Name:':<14} {entry['name']}")
            lines.append(f"{'Category:':<14} {entry['category']}")
            lines.append("")
            lines.append("Syntax:")
            for sl in entry["syntax"].split("\n"):
                lines.append(f"  {sl}")
            lines.append("")
            lines.append(f"{'Summary:':<14} {entry['summary']}")
            lines.append("")
            lines.append("Description:")
            for dl in entry["detail"].split("\n"):
                if len(dl) <= width - 4:
                    lines.append(f"  {dl}")
                else:
                    words = dl.split()
                    current: List[str] = []
                    col = 2
                    for w in words:
                        if current and col + 1 + len(w) > width - 2:
                            lines.append("  " + " ".join(current))
                            current = [w]
                            col = 2 + len(w)
                        else:
                            current.append(w)
                            col += 1 + len(w) if current else len(w)
                    if current:
                        lines.append("  " + " ".join(current))
            return lines

        def _render_tab_bar(scr: Any, y: int, width: int) -> None:
            x = 1
            for i, name in enumerate(_TAB_NAMES):
                label = f" {name} "
                attr = curses.A_REVERSE | curses.A_BOLD if i == active_tab else curses.A_DIM
                _safe_addnstr(scr, y, x, label, width - x - 1, attr)
                x += len(label) + 1
            # Right-aligned shortcuts
            shortcuts = " ? Q&A  H how  P philosophy  L license "
            if x + len(shortcuts) < width:
                _safe_addnstr(scr, y, width - len(shortcuts) - 1, shortcuts, len(shortcuts), curses.A_DIM)

        def _apply_filters(items: List[DocEntry]) -> List[DocEntry]:
            result = items
            kind = _FILTER_KINDS[filter_kind_idx]
            if kind != "all":
                result = [e for e in result if e.kind == kind]
            # File toggle filter
            if not all(filter_files_enabled.get(p, True) for p in all_file_paths):
                result = [e for e in result if filter_files_enabled.get(e.path.as_posix(), True)]
            # Signature filters
            if filter_args >= 0 or filter_returns >= 0:
                filtered = []
                for e in result:
                    n_args, n_rets = _parse_stack_effect_counts(e.stack_effect)
                    if filter_args >= 0 and n_args != filter_args:
                        continue
                    if filter_returns >= 0 and n_rets != filter_returns:
                        continue
                    filtered.append(e)
                result = filtered
            return result

        def _ct_ref_line_is_word(line: str) -> bool:
            if not line.strip():
                return False
            if line.lstrip().startswith("§"):
                return False
            if "━━━━━━━━" in line:
                return False
            return _CT_REF_WORD_RE.match(line) is not None

        def _ct_ref_extract_word_name(line: str) -> Optional[str]:
            if not _ct_ref_line_is_word(line):
                return None
            match = _CT_REF_ENTRY_LINE_RE.match(line)
            if match is not None:
                return match.group(1).strip()
            stripped = line.strip()
            if not stripped:
                return None
            return stripped.split()[0]

        def _ct_ref_name_allowed(name: str) -> bool:
            if not name:
                return False
            if name.lower() in {"word", "call:", "overview:", "example:", "category:", "scope:"}:
                return False
            if name.startswith(("-", "*", "->", "<")):
                return False
            return re.match(r"^[A-Za-z0-9][A-Za-z0-9_?.:+\-*/>=!&]*$", name) is not None

        ct_ref_valid_names = {str(item["name"]) for item in _collect_ct_word_metadata()}

        def _ct_ref_entry_scope_guess(entry: Dict[str, Any]) -> str:
            explicit_scope = str(entry.get("scope", "")).strip().lower()
            if explicit_scope in ct_ref_scope_options:
                return explicit_scope

            text = str(entry.get("text", "")).lower()
            if "runtime-only" in text:
                return "runtime-only"
            if "runtime + compile-time" in text or "runtime+compile-time" in text:
                return "runtime+compile-time"
            if "[immediate" in text:
                return "immediate"
            if "compile-only" in text:
                return "compile-only"
            return "all"

        def _ct_ref_collect_word_entries() -> List[Dict[str, Any]]:
            if _ct_ref_bundle_entries:
                structured: List[Dict[str, Any]] = []
                for item in _ct_ref_bundle_entries:
                    name = str(item.get("name", "")).strip()
                    if name not in ct_ref_valid_names or not _ct_ref_name_allowed(name):
                        continue
                    stack_effect = str(item.get("stack_effect", "")).strip()
                    category = str(item.get("category", "Meta")).strip()
                    overview = str(item.get("overview", "")).strip()
                    example = str(item.get("example", "")).strip()
                    example_lines = _split_example_lines(item)
                    scope = str(item.get("scope", "all")).strip().lower()
                    line_no = int(item.get("line_no", 0))
                    block_lines: List[str] = []
                    if stack_effect:
                        block_lines.append(f"{name:<34} {stack_effect}")
                    else:
                        block_lines.append(name)
                    block_lines.append(f"Category: {category}")
                    if scope and scope != "all":
                        block_lines.append(f"Scope: {scope}")
                    if overview:
                        block_lines.append(f"Overview: {overview}")
                    if example_lines:
                        block_lines.append("Example:")
                        for ex_line in example_lines:
                            block_lines.append(f"  - {ex_line}")

                    searchable_text = " ".join(
                        [
                            str(item.get("search_text", "")),
                            name,
                            category,
                            scope,
                            stack_effect,
                            overview,
                            " ".join(example_lines),
                            example,
                        ]
                    ).lower()

                    structured.append(
                        {
                            "name": name,
                            "line_no": line_no,
                            "start": max(0, line_no - 1),
                            "section": str(item.get("section", "COMPLETE CT FUNCTION INDEX")),
                            "lines": block_lines,
                            "text": searchable_text,
                            "has_example": bool(example.strip()),
                            "description_line_count": len([ln for ln in overview.split(".") if ln.strip()]),
                            "scope": scope or "all",
                            "category": category,
                            "stack_effect": stack_effect,
                            "overview": overview,
                            "example": example,
                        }
                    )
                structured.sort(key=lambda entry: str(entry.get("name", "")).lower())
                return structured

            raw_entries: List[Dict[str, Any]] = []
            i = 0
            while i < len(ct_ref_all_lines):
                line = ct_ref_all_lines[i]
                name = _ct_ref_extract_word_name(line)
                if name is None or name not in ct_ref_valid_names or not _ct_ref_name_allowed(name):
                    i += 1
                    continue

                section_name = ct_ref_line_sections[i] if i < len(ct_ref_line_sections) else "intro"
                if "COMPLETE CT FUNCTION INDEX" not in section_name.upper():
                    i += 1
                    continue

                start = i
                end = i + 1
                while end < len(ct_ref_all_lines):
                    next_line = ct_ref_all_lines[end]
                    if _ct_ref_extract_word_name(next_line) is not None:
                        break
                    if _CT_REF_SECTION_RE.match(next_line) is not None:
                        break
                    end += 1

                block_lines = ct_ref_all_lines[start:end]
                block_text = " ".join(part.strip() for part in block_lines if part.strip())
                description_lines = [
                    part.strip()
                    for part in block_lines[1:]
                    if part.strip()
                ]
                has_example = any("example:" in part.lower() for part in block_lines)
                raw_entries.append(
                    {
                        "name": name,
                        "line_no": start + 1,
                        "start": start,
                        "section": section_name,
                        "lines": block_lines,
                        "text": block_text.lower(),
                        "has_example": has_example,
                        "description_line_count": len(description_lines),
                        "scope": "all",
                    }
                )
                i = end

            # Deduplicate by word name and keep the richest description block.
            by_name: Dict[str, Dict[str, Any]] = {}
            for entry in raw_entries:
                name = str(entry["name"])
                existing = by_name.get(name)
                if existing is None:
                    by_name[name] = entry
                    continue

                old_score = (
                    1 if bool(existing.get("has_example")) else 0,
                    int(existing.get("description_line_count", 0)),
                    len(existing.get("text", "")),
                )
                new_score = (
                    1 if bool(entry.get("has_example")) else 0,
                    int(entry.get("description_line_count", 0)),
                    len(entry.get("text", "")),
                )
                if new_score > old_score:
                    by_name[name] = entry

            out = sorted(by_name.values(), key=lambda item: str(item["name"]).lower())
            return out

        ct_ref_word_entries = _ct_ref_collect_word_entries()

        def _ct_ref_entry_matches_scope(entry: Dict[str, Any], scope: str) -> bool:
            if scope == "all":
                return True
            entry_scope = _ct_ref_entry_scope_guess(entry)
            if scope == "compile-only":
                return entry_scope in ("compile-only", "immediate")
            return entry_scope == scope

        def _ct_ref_search_entries(query: str, scope: str, section_filter: str) -> List[Dict[str, Any]]:
            active_scope = scope
            active_section = section_filter
            tokens: List[str] = []
            for part in query.strip().split():
                lower = part.lower()
                if lower.startswith("section:") and len(part) > len("section:"):
                    active_section = part.split(":", 1)[1].replace("_", " ").strip().lower()
                    continue
                if lower.startswith("scope:") and len(part) > len("scope:"):
                    normalized_scope = _ct_ref_normalize_scope(part.split(":", 1)[1])
                    if normalized_scope is not None:
                        active_scope = normalized_scope
                        continue
                tokens.append(lower)
            out: List[Tuple[int, Dict[str, Any]]] = []

            for entry in ct_ref_word_entries:
                if not _ct_ref_entry_matches_scope(entry, active_scope):
                    continue
                if active_section != "all":
                    entry_section = str(entry.get("section", "intro")).lower()
                    if active_section not in entry_section:
                        continue

                name = str(entry.get("name", ""))
                name_l = name.lower()
                text_l = str(entry.get("text", "")).lower()

                if not tokens:
                    score = 10
                    out.append((score, entry))
                    continue

                score = 0
                matched_all = True
                for token in tokens:
                    if token == name_l:
                        score += 220
                    elif name_l.startswith(token):
                        score += 170
                    elif token in name_l:
                        score += 120
                    elif token in text_l:
                        score += 35
                    else:
                        matched_all = False
                        break

                if not matched_all:
                    continue

                if bool(entry.get("has_example")):
                    score += 5
                score += min(int(entry.get("description_line_count", 0)), 12)
                out.append((score, entry))

            out.sort(key=lambda item: (-item[0], str(item[1].get("name", "")).lower()))
            return [entry for _, entry in out]

        def _refresh_ct_ref_results(*, reset_selection: bool) -> None:
            nonlocal ct_ref_result_entries, ct_ref_result_query
            nonlocal ct_ref_result_selected, ct_ref_result_scroll
            nonlocal ct_ref_result_signature

            query = ct_ref_query.strip()
            scope = ct_ref_scope_options[ct_ref_scope_idx]
            section_filter = ct_ref_sections[ct_ref_section_idx].lower()
            signature = (query, scope, section_filter)
            if (not reset_selection) and signature == ct_ref_result_signature and ct_ref_result_entries:
                return

            ct_ref_result_entries = _ct_ref_search_entries(query, scope, section_filter)
            ct_ref_result_query = query
            ct_ref_result_signature = signature

            if reset_selection:
                ct_ref_result_selected = 0
                ct_ref_result_scroll = 0
            else:
                if not ct_ref_result_entries:
                    ct_ref_result_selected = 0
                    ct_ref_result_scroll = 0
                else:
                    ct_ref_result_selected = max(
                        0,
                        min(ct_ref_result_selected, len(ct_ref_result_entries) - 1),
                    )

        def _build_ct_ref_detail_lines(entry: Dict[str, Any], width: int) -> List[str]:
            if _docs_helpers is not None and hasattr(_docs_helpers, "build_ct_detail_lines"):
                try:
                    helper_lines = _docs_helpers.build_ct_detail_lines(entry, width)
                    if helper_lines:
                        return [str(line) for line in helper_lines]
                except Exception:
                    pass

            lines: List[str] = []
            name = str(entry.get("name", ""))
            section = str(entry.get("section", "intro"))
            line_no = int(entry.get("line_no", 0))
            category = str(entry.get("category", "Meta"))
            stack_effect = str(entry.get("stack_effect", "")).strip()
            overview = str(entry.get("overview", "")).strip()
            example = str(entry.get("example", "")).strip()
            example_lines = _split_example_lines(entry)
            scope = _ct_ref_entry_scope_guess(entry)

            lines.append(f"Function: {name}")
            lines.append(f"Category: {category} | Scope: {scope} | Section: {section} | Line: {line_no}")
            lines.append("")

            if stack_effect:
                lines.append("Stack Effect:")
                for wrapped in textwrap.wrap(stack_effect, max(20, width - 6)):
                    lines.append(f"  {wrapped}")
                lines.append("")

            if overview:
                lines.append("Overview:")
                for wrapped in textwrap.wrap(overview, max(20, width - 6)):
                    lines.append(f"  {wrapped}")
                lines.append("")

            if example_lines:
                lines.append("Example:")
                for ex_line in example_lines:
                    wrapped_lines = textwrap.wrap(f"- {ex_line}", max(20, width - 6)) or [""]
                    for wrapped in wrapped_lines:
                        lines.append(f"  {wrapped}")
                lines.append("")

            raw_lines = [str(part) for part in entry.get("lines", [])]
            body_lines: List[str] = []
            for raw in raw_lines:
                stripped = raw.strip()
                if not stripped:
                    body_lines.append("")
                    continue
                wrapped = textwrap.wrap(stripped, max(20, width - 6))
                if wrapped:
                    body_lines.extend(wrapped)
                else:
                    body_lines.append(stripped)

            if body_lines:
                lines.extend(body_lines)
            else:
                lines.append("No details available.")

            return lines

        def _ct_ref_scope_matches(line: str, scope: str) -> bool:
            if scope == "all":
                return True
            low = line.lower()
            if scope == "immediate":
                return "[immediate" in low
            if scope == "compile-only":
                return "compile-only" in low
            if scope == "runtime+compile-time":
                return "runtime + compile-time" in low or "runtime+compile-time" in low
            if scope == "runtime-only":
                return "runtime-only" in low
            return True

        def _ct_ref_normalize_scope(raw_scope: str) -> Optional[str]:
            scope = raw_scope.strip().lower().replace("_", "-")
            aliases = {
                "rt+ct": "runtime+compile-time",
                "runtime-compile-time": "runtime+compile-time",
                "runtime+ct": "runtime+compile-time",
                "rt-only": "runtime-only",
            }
            scope = aliases.get(scope, scope)
            if scope in ct_ref_scope_options:
                return scope
            return None

        def _ct_ref_filter_summary() -> str:
            parts: List[str] = []
            if ct_ref_section_idx > 0:
                parts.append(f"section={ct_ref_sections[ct_ref_section_idx]}")
            if ct_ref_scope_idx > 0:
                parts.append(f"scope={ct_ref_scope_options[ct_ref_scope_idx]}")
            if ct_ref_words_only:
                parts.append("words-only")
            return ", ".join(parts)

        def _filter_ct_ref_lines(raw_query: str) -> List[str]:
            query_text = raw_query.strip()
            query_terms: List[str] = []
            query_section: Optional[str] = None
            query_scope: Optional[str] = None
            for part in query_text.split():
                lower = part.lower()
                if lower.startswith("section:") and len(part) > len("section:"):
                    query_section = part.split(":", 1)[1].replace("_", " ").strip().lower()
                    continue
                if lower.startswith("scope:") and len(part) > len("scope:"):
                    query_scope = _ct_ref_normalize_scope(part.split(":", 1)[1])
                    continue
                query_terms.append(lower)

            section_filter = ct_ref_sections[ct_ref_section_idx].lower()
            if query_section:
                section_filter = query_section

            scope_filter = ct_ref_scope_options[ct_ref_scope_idx]
            if query_scope is not None:
                scope_filter = query_scope

            candidates: List[Tuple[int, str, str]] = []
            for idx, line in enumerate(ct_ref_all_lines):
                section = ct_ref_line_sections[idx]
                if section_filter != "all" and section_filter not in section.lower():
                    continue
                line_is_word = _ct_ref_line_is_word(line)
                if ct_ref_words_only and not line_is_word:
                    continue
                if scope_filter != "all":
                    if not line_is_word:
                        continue
                    if not _ct_ref_scope_matches(line, scope_filter):
                        continue
                candidates.append((idx + 1, line, section))

            if not query_terms:
                if section_filter == "all" and scope_filter == "all" and not ct_ref_words_only:
                    return list(ct_ref_all_lines)
                if not candidates:
                    return ["(no compile-time reference lines match current filters)"]
                out: List[str] = [
                    f"[filter] {len(candidates)} line(s)"
                    + (f" section={section_filter}" if section_filter != "all" else "")
                    + (f" scope={scope_filter}" if scope_filter != "all" else "")
                    + (" words-only" if ct_ref_words_only else ""),
                    "",
                ]
                out.extend(f"{line_no:5d}: {line}" for line_no, line, _ in candidates)
                return out

            scored_matches: List[Tuple[int, int, str, str]] = []
            for line_no, line, section in candidates:
                line_low = line.lower()
                section_low = section.lower()
                score = 0
                for term in query_terms:
                    if line_low == term:
                        score += 300
                    elif re.search(rf"\\b{re.escape(term)}\\b", line_low):
                        score += 180
                    elif line_low.startswith(term):
                        score += 120
                    elif term in line_low:
                        score += 80
                    elif term in section_low:
                        score += 45
                    else:
                        score = -1
                        break
                if score < 0:
                    continue
                if _ct_ref_line_is_word(line):
                    score += 10
                scored_matches.append((score, line_no, line, section))

            if not scored_matches:
                return [f"(no compile-time reference matches for '{query_text}')"]

            scored_matches.sort(key=lambda item: (-item[0], item[1]))
            out = [
                f"[search] {len(scored_matches)} match(es) for '{query_text}'",
                "",
            ]
            out.extend(f"{line_no:5d} [{section}] {line}" for _, line_no, line, section in scored_matches)
            return out

        def _refresh_ct_ref_lines(*, reset_results: bool = True) -> None:
            nonlocal info_lines, info_scroll
            info_lines = _filter_ct_ref_lines(ct_ref_query)
            info_scroll = 0
            _refresh_ct_ref_results(reset_selection=reset_results)

        while True:
            filtered = _apply_filters(_filter_docs(entries, query))
            if selected >= len(filtered):
                selected = max(0, len(filtered) - 1)

            height, width = stdscr.getmaxyx()
            if height < 3 or width < 10:
                stdscr.erase()
                _safe_addnstr(stdscr, 0, 0, "terminal too small", width - 1)
                stdscr.refresh()
                stdscr.getch()
                continue

            # -- DETAIL MODE --
            if mode == _MODE_DETAIL:
                stdscr.erase()
                _safe_addnstr(
                    stdscr, 0, 0,
                    f" {detail_lines[0] if detail_lines else ''} ",
                    width - 1, curses.A_BOLD,
                )
                _safe_addnstr(stdscr, 1, 0, " q/Esc: back  j/k/Up/Down: scroll  PgUp/PgDn ", width - 1, curses.A_DIM)
                body_height = max(1, height - 3)
                max_dscroll = max(0, len(detail_lines) - body_height)
                if detail_scroll > max_dscroll:
                    detail_scroll = max_dscroll
                for row in range(body_height):
                    li = detail_scroll + row
                    if li >= len(detail_lines):
                        break
                    _safe_addnstr(stdscr, 2 + row, 0, detail_lines[li], width - 1)
                pos_text = f" {detail_scroll + 1}-{min(detail_scroll + body_height, len(detail_lines))}/{len(detail_lines)} "
                _safe_addnstr(stdscr, height - 1, 0, pos_text, width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (27, ord("q"), ord("h"), curses.KEY_LEFT):
                    mode = _MODE_BROWSE
                    continue
                if key in (curses.KEY_DOWN, ord("j")):
                    if detail_scroll < max_dscroll:
                        detail_scroll += 1
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    if detail_scroll > 0:
                        detail_scroll -= 1
                    continue
                if key == curses.KEY_NPAGE:
                    detail_scroll = min(max_dscroll, detail_scroll + body_height)
                    continue
                if key == curses.KEY_PPAGE:
                    detail_scroll = max(0, detail_scroll - body_height)
                    continue
                if key == ord("g"):
                    detail_scroll = 0
                    continue
                if key == ord("G"):
                    detail_scroll = max_dscroll
                    continue
                continue

            # -- FILTER MODE --
            if mode == _MODE_FILTER:
                stdscr.erase()
                _safe_addnstr(stdscr, 0, 0, " Filters ", width - 1, curses.A_BOLD)
                _safe_addnstr(stdscr, 1, 0, " Tab: next field  Space/Left/Right: change  a: all files  n: none  Enter/Esc: close ", width - 1, curses.A_DIM)

                _N_FILTER_FIELDS = 7  # kind, args, returns, show_private, show_macros, extra_path, files
                row_y = 3

                # Kind row
                kind_label = f"  Kind: < {_FILTER_KINDS[filter_kind_idx]:6} >"
                kind_attr = curses.A_REVERSE if filter_field == 0 else 0
                _safe_addnstr(stdscr, row_y, 0, kind_label, width - 1, kind_attr)
                row_y += 1

                # Args row
                args_val = "any" if filter_args < 0 else str(filter_args)
                args_label = f"  Args: < {args_val:6} >"
                args_attr = curses.A_REVERSE if filter_field == 1 else 0
                _safe_addnstr(stdscr, row_y, 0, args_label, width - 1, args_attr)
                row_y += 1

                # Returns row
                rets_val = "any" if filter_returns < 0 else str(filter_returns)
                rets_label = f"  Rets: < {rets_val:6} >"
                rets_attr = curses.A_REVERSE if filter_field == 2 else 0
                _safe_addnstr(stdscr, row_y, 0, rets_label, width - 1, rets_attr)
                row_y += 1

                # Show private row
                priv_val = "yes" if filter_show_private else "no"
                priv_label = f"  Private: < {priv_val:6} >"
                priv_attr = curses.A_REVERSE if filter_field == 3 else 0
                _safe_addnstr(stdscr, row_y, 0, priv_label, width - 1, priv_attr)
                row_y += 1

                # Show macros row
                macro_val = "yes" if filter_show_macros else "no"
                macro_label = f"  Macros: < {macro_val:6} >"
                macro_attr = curses.A_REVERSE if filter_field == 4 else 0
                _safe_addnstr(stdscr, row_y, 0, macro_label, width - 1, macro_attr)
                row_y += 1

                # Extra path row
                if filter_field == 5:
                    ep_label = f"  Path: {filter_extra_path}_"
                    ep_attr = curses.A_REVERSE
                else:
                    ep_label = f"  Path: {filter_extra_path or '(type path, Enter to add)'}"
                    ep_attr = 0
                _safe_addnstr(stdscr, row_y, 0, ep_label, width - 1, ep_attr)
                row_y += 1
                for er in filter_extra_roots:
                    _safe_addnstr(stdscr, row_y, 0, f"    + {er}", width - 1, curses.A_DIM)
                    row_y += 1
                row_y += 1

                # Files section
                files_header = "  Files:"
                files_header_attr = curses.A_BOLD if filter_field == 6 else curses.A_DIM
                _safe_addnstr(stdscr, row_y, 0, files_header, width - 1, files_header_attr)
                row_y += 1

                file_area_top = row_y
                file_area_height = max(1, height - file_area_top - 2)
                n_files = len(all_file_paths)

                if filter_field == 6:
                    # Clamp cursor and scroll
                    if filter_file_cursor >= n_files:
                        filter_file_cursor = max(0, n_files - 1)
                    if filter_file_cursor < filter_file_scroll:
                        filter_file_scroll = filter_file_cursor
                    if filter_file_cursor >= filter_file_scroll + file_area_height:
                        filter_file_scroll = filter_file_cursor - file_area_height + 1
                    max_fscroll = max(0, n_files - file_area_height)
                    if filter_file_scroll > max_fscroll:
                        filter_file_scroll = max_fscroll

                for row in range(file_area_height):
                    fi = filter_file_scroll + row
                    if fi >= n_files:
                        break
                    fp = all_file_paths[fi]
                    mark = "[x]" if filter_files_enabled.get(fp, True) else "[ ]"
                    label = f"    {mark} {fp}"
                    attr = curses.A_REVERSE if (filter_field == 6 and fi == filter_file_cursor) else 0
                    _safe_addnstr(stdscr, file_area_top + row, 0, label, width - 1, attr)

                enabled_count = sum(1 for v in filter_files_enabled.values() if v)
                preview = _apply_filters(_filter_docs(entries, query))
                status = f" {enabled_count}/{n_files} files  kind={_FILTER_KINDS[filter_kind_idx]}  args={args_val}  rets={rets_val}  {len(preview)} matches "
                _safe_addnstr(stdscr, height - 1, 0, status, width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()
                if key == 27:
                    mode = _MODE_BROWSE
                    selected = 0
                    scroll = 0
                    continue
                if key in (10, 13, curses.KEY_ENTER) and filter_field != 5:
                    mode = _MODE_BROWSE
                    selected = 0
                    scroll = 0
                    continue
                if key == 9:  # Tab
                    filter_field = (filter_field + 1) % _N_FILTER_FIELDS
                    continue
                if filter_field not in (5, 6):
                    if key in (curses.KEY_DOWN, ord("j")):
                        filter_field = (filter_field + 1) % _N_FILTER_FIELDS
                        continue
                    if key in (curses.KEY_UP, ord("k")):
                        filter_field = (filter_field - 1) % _N_FILTER_FIELDS
                        continue
                if filter_field == 0:
                    # Kind field
                    if key in (curses.KEY_LEFT, ord("h")):
                        filter_kind_idx = (filter_kind_idx - 1) % len(_FILTER_KINDS)
                        continue
                    if key in (curses.KEY_RIGHT, ord("l"), ord(" ")):
                        filter_kind_idx = (filter_kind_idx + 1) % len(_FILTER_KINDS)
                        continue
                elif filter_field == 1:
                    # Args field: Left/Right to adjust, -1 = any
                    if key in (curses.KEY_RIGHT, ord("l"), ord(" ")):
                        filter_args += 1
                        if filter_args > 10:
                            filter_args = -1
                        continue
                    if key in (curses.KEY_LEFT, ord("h")):
                        filter_args -= 1
                        if filter_args < -1:
                            filter_args = 10
                        continue
                elif filter_field == 2:
                    # Returns field: Left/Right to adjust
                    if key in (curses.KEY_RIGHT, ord("l"), ord(" ")):
                        filter_returns += 1
                        if filter_returns > 10:
                            filter_returns = -1
                        continue
                    if key in (curses.KEY_LEFT, ord("h")):
                        filter_returns -= 1
                        if filter_returns < -1:
                            filter_returns = 10
                        continue
                elif filter_field == 3:
                    # Show private toggle
                    if key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l"), ord(" ")):
                        filter_show_private = not filter_show_private
                        if reload_fn is not None:
                            entries = reload_fn(include_private=filter_show_private, include_macros=filter_show_macros, extra_roots=filter_extra_roots)
                            _rebuild_file_list()
                        continue
                elif filter_field == 4:
                    # Show macros toggle
                    if key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l"), ord(" ")):
                        filter_show_macros = not filter_show_macros
                        if reload_fn is not None:
                            entries = reload_fn(include_private=filter_show_private, include_macros=filter_show_macros, extra_roots=filter_extra_roots)
                            _rebuild_file_list()
                        continue
                elif filter_field == 5:
                    # Extra path: text input, Enter adds to roots
                    if key in (10, 13, curses.KEY_ENTER):
                        if filter_extra_path.strip():
                            filter_extra_roots.append(filter_extra_path.strip())
                            filter_extra_path = ""
                            if reload_fn is not None:
                                entries = reload_fn(
                                    include_private=filter_show_private,
                                    include_macros=filter_show_macros,
                                    extra_roots=filter_extra_roots,
                                )
                                _rebuild_file_list()
                        continue
                    if key in (curses.KEY_BACKSPACE, 127, 8):
                        filter_extra_path = filter_extra_path[:-1]
                        continue
                    if 32 <= key <= 126:
                        filter_extra_path += chr(key)
                        continue
                elif filter_field == 6:
                    # Files field
                    if key in (curses.KEY_UP, ord("k")):
                        if filter_file_cursor > 0:
                            filter_file_cursor -= 1
                        continue
                    if key in (curses.KEY_DOWN, ord("j")):
                        if filter_file_cursor + 1 < n_files:
                            filter_file_cursor += 1
                        continue
                    if key == ord(" "):
                        if 0 <= filter_file_cursor < n_files:
                            fp = all_file_paths[filter_file_cursor]
                            filter_files_enabled[fp] = not filter_files_enabled.get(fp, True)
                        continue
                    if key == ord("a"):
                        for fp in all_file_paths:
                            filter_files_enabled[fp] = True
                        continue
                    if key == ord("n"):
                        for fp in all_file_paths:
                            filter_files_enabled[fp] = False
                        continue
                    if key == curses.KEY_PPAGE:
                        filter_file_cursor = max(0, filter_file_cursor - file_area_height)
                        continue
                    if key == curses.KEY_NPAGE:
                        filter_file_cursor = min(max(0, n_files - 1), filter_file_cursor + file_area_height)
                        continue
                continue

            # -- SEARCH MODE --
            if mode == _MODE_SEARCH:
                stdscr.erase()
                prompt = f"/{search_buf}"
                _safe_addnstr(stdscr, 0, 0, prompt, width - 1, curses.A_BOLD)
                preview = _apply_filters(_filter_docs(entries, search_buf))
                _safe_addnstr(stdscr, 1, 0, f" {len(preview)} matches   (Enter: apply  Esc: cancel)", width - 1, curses.A_DIM)
                preview_height = max(1, height - 3)
                for row in range(min(preview_height, len(preview))):
                    e = preview[row]
                    effect = e.stack_effect if e.stack_effect else "(no stack effect)"
                    line = f"  {e.name:24} {effect}"
                    _safe_addnstr(stdscr, 2 + row, 0, line, width - 1)
                stdscr.refresh()
                try:
                    curses.curs_set(1)
                except Exception:
                    pass
                key = stdscr.getch()
                if key == 27:
                    # Cancel search, revert
                    search_buf = query
                    mode = _MODE_BROWSE
                    try:
                        curses.curs_set(0)
                    except Exception:
                        pass
                    continue
                if key in (10, 13, curses.KEY_ENTER):
                    query = search_buf
                    selected = 0
                    scroll = 0
                    mode = _MODE_BROWSE
                    try:
                        curses.curs_set(0)
                    except Exception:
                        pass
                    continue
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    search_buf = search_buf[:-1]
                    continue
                if 32 <= key <= 126:
                    search_buf += chr(key)
                    continue
                continue

            # -- LANGUAGE REFERENCE BROWSE --
            if mode == _MODE_LANG_REF:
                lang_entries = _filter_lang_ref()
                if lang_selected >= len(lang_entries):
                    lang_selected = max(0, len(lang_entries) - 1)

                list_height = max(1, height - 5)
                if lang_selected < lang_scroll:
                    lang_scroll = lang_selected
                if lang_selected >= lang_scroll + list_height:
                    lang_scroll = lang_selected - list_height + 1
                max_ls = max(0, len(lang_entries) - list_height)
                if lang_scroll > max_ls:
                    lang_scroll = max_ls

                stdscr.erase()
                _render_tab_bar(stdscr, 0, width)
                cat_names = ["all"] + _LANG_REF_CATEGORIES
                cat_label = cat_names[lang_cat_filter]
                header = f" Language Reference  {len(lang_entries)} entries  category: {cat_label}"
                _safe_addnstr(stdscr, 1, 0, header, width - 1, curses.A_BOLD)
                hint = " c category  Enter detail  j/k nav  Tab switch  C ct-ref  ? Q&A  H how  P philosophy  q quit"
                _safe_addnstr(stdscr, 2, 0, hint, width - 1, curses.A_DIM)

                for row in range(list_height):
                    idx = lang_scroll + row
                    if idx >= len(lang_entries):
                        break
                    le = lang_entries[idx]
                    cat_tag = f"[{le['category']}]"
                    line = f"  {le['name']:<28} {le['summary']:<36} {cat_tag}"
                    attr = curses.A_REVERSE if idx == lang_selected else 0
                    _safe_addnstr(stdscr, 3 + row, 0, line, width - 1, attr)

                if lang_entries:
                    cur = lang_entries[lang_selected]
                    _safe_addnstr(stdscr, height - 1, 0, f" {cur['syntax'].split(chr(10))[0]}", width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()

                if key in (27, ord("q")):
                    return 0
                if key == 9:  # Tab
                    active_tab = _TAB_CT_REF
                    _refresh_ct_ref_lines()
                    mode = _MODE_CT_REF
                    continue
                if key == ord("c"):
                    lang_cat_filter = (lang_cat_filter + 1) % (len(_LANG_REF_CATEGORIES) + 1)
                    lang_selected = 0
                    lang_scroll = 0
                    continue
                if key in (10, 13, curses.KEY_ENTER):
                    if lang_entries:
                        lang_detail_lines = _build_lang_detail_lines(lang_entries[lang_selected], width)
                        lang_detail_scroll = 0
                        mode = _MODE_LANG_DETAIL
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    if lang_selected > 0:
                        lang_selected -= 1
                    continue
                if key in (curses.KEY_DOWN, ord("j")):
                    if lang_selected + 1 < len(lang_entries):
                        lang_selected += 1
                    continue
                if key == curses.KEY_PPAGE:
                    lang_selected = max(0, lang_selected - list_height)
                    continue
                if key == curses.KEY_NPAGE:
                    lang_selected = min(max(0, len(lang_entries) - 1), lang_selected + list_height)
                    continue
                if key == ord("g"):
                    lang_selected = 0
                    lang_scroll = 0
                    continue
                if key == ord("G"):
                    lang_selected = max(0, len(lang_entries) - 1)
                    continue
                if key == ord("L"):
                    info_lines = _L2_LICENSE_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_LICENSE
                    continue
                if key == ord("P"):
                    info_lines = _L2_PHILOSOPHY_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_PHILOSOPHY
                    continue
                if key == ord("?"):
                    info_lines = _L2_QA_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_QA
                    continue
                if key == ord("H"):
                    info_lines = _L2_HOW_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_HOW
                    continue
                if key == ord("C"):
                    active_tab = _TAB_CT_REF
                    _refresh_ct_ref_lines()
                    mode = _MODE_CT_REF
                    continue

            # -- LANGUAGE DETAIL MODE --
            if mode == _MODE_LANG_DETAIL:
                stdscr.erase()
                _safe_addnstr(
                    stdscr, 0, 0,
                    f" {lang_detail_lines[0] if lang_detail_lines else ''} ",
                    width - 1, curses.A_BOLD,
                )
                _safe_addnstr(stdscr, 1, 0, " q/Esc: back  j/k/Up/Down: scroll  PgUp/PgDn ", width - 1, curses.A_DIM)
                body_height = max(1, height - 3)
                max_ldscroll = max(0, len(lang_detail_lines) - body_height)
                if lang_detail_scroll > max_ldscroll:
                    lang_detail_scroll = max_ldscroll
                for row in range(body_height):
                    li = lang_detail_scroll + row
                    if li >= len(lang_detail_lines):
                        break
                    _safe_addnstr(stdscr, 2 + row, 0, lang_detail_lines[li], width - 1)
                pos_text = f" {lang_detail_scroll + 1}-{min(lang_detail_scroll + body_height, len(lang_detail_lines))}/{len(lang_detail_lines)} "
                _safe_addnstr(stdscr, height - 1, 0, pos_text, width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (27, ord("q"), ord("h"), curses.KEY_LEFT):
                    mode = _MODE_LANG_REF
                    continue
                if key in (curses.KEY_DOWN, ord("j")):
                    if lang_detail_scroll < max_ldscroll:
                        lang_detail_scroll += 1
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    if lang_detail_scroll > 0:
                        lang_detail_scroll -= 1
                    continue
                if key == curses.KEY_NPAGE:
                    lang_detail_scroll = min(max_ldscroll, lang_detail_scroll + body_height)
                    continue
                if key == curses.KEY_PPAGE:
                    lang_detail_scroll = max(0, lang_detail_scroll - body_height)
                    continue
                if key == ord("g"):
                    lang_detail_scroll = 0
                    continue
                if key == ord("G"):
                    lang_detail_scroll = max_ldscroll
                    continue
                continue

            # -- LICENSE / PHILOSOPHY / Q&A / HOW-IT-WORKS MODE --
            if mode in (_MODE_LICENSE, _MODE_PHILOSOPHY, _MODE_QA, _MODE_HOW):
                _info_titles = {
                    _MODE_LICENSE: "License",
                    _MODE_PHILOSOPHY: "Philosophy of L2",
                    _MODE_QA: "Q&A / Tips & Tricks",
                    _MODE_HOW: "How L2 Works (Internals)",
                }
                title = _info_titles.get(mode, "")
                stdscr.erase()
                _safe_addnstr(stdscr, 0, 0, f" {title} ", width - 1, curses.A_BOLD)
                _safe_addnstr(stdscr, 1, 0, " q/Esc: back  j/k: scroll  PgUp/PgDn ", width - 1, curses.A_DIM)
                body_height = max(1, height - 3)
                max_iscroll = max(0, len(info_lines) - body_height)
                if info_scroll > max_iscroll:
                    info_scroll = max_iscroll
                for row in range(body_height):
                    li = info_scroll + row
                    if li >= len(info_lines):
                        break
                    _safe_addnstr(stdscr, 2 + row, 0, f"  {info_lines[li]}", width - 1)
                pos_text = f" {info_scroll + 1}-{min(info_scroll + body_height, len(info_lines))}/{len(info_lines)} "
                _safe_addnstr(stdscr, height - 1, 0, pos_text, width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()
                prev_mode = _MODE_LANG_REF if active_tab == _TAB_LANG_REF else (_MODE_CT_REF if active_tab == _TAB_CT_REF else _MODE_BROWSE)
                if key in (27, ord("q"), ord("h"), curses.KEY_LEFT):
                    mode = prev_mode
                    # Restore info_lines when returning to CT ref
                    if prev_mode == _MODE_CT_REF:
                        _refresh_ct_ref_lines()
                    continue
                if key in (curses.KEY_DOWN, ord("j")):
                    if info_scroll < max_iscroll:
                        info_scroll += 1
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    if info_scroll > 0:
                        info_scroll -= 1
                    continue
                if key == curses.KEY_NPAGE:
                    info_scroll = min(max_iscroll, info_scroll + body_height)
                    continue
                if key == curses.KEY_PPAGE:
                    info_scroll = max(0, info_scroll - body_height)
                    continue
                if key == ord("g"):
                    info_scroll = 0
                    continue
                if key == ord("G"):
                    info_scroll = max_iscroll
                    continue
                continue

            # -- COMPILE-TIME REFERENCE SEARCH MODE --
            if mode == _MODE_CT_REF_SEARCH:
                stdscr.erase()
                prompt = f"/ct {ct_ref_search_buf}"
                _safe_addnstr(stdscr, 0, 0, prompt, width - 1, curses.A_BOLD)
                preview_scope = ct_ref_scope_options[ct_ref_scope_idx]
                preview_section = ct_ref_sections[ct_ref_section_idx].lower()
                preview_entries = _ct_ref_search_entries(ct_ref_search_buf, preview_scope, preview_section)
                _safe_addnstr(
                    stdscr,
                    1,
                    0,
                    f" {len(preview_entries)} function(s)  (Enter: open results  Esc: cancel)  query fields: section:<name> scope:<mode>",
                    width - 1,
                    curses.A_DIM,
                )
                preview_height = max(1, height - 3)
                for row in range(min(preview_height, len(preview_entries))):
                    entry = preview_entries[row]
                    marker = "ex" if bool(entry.get("has_example")) else "--"
                    line = int(entry.get("line_no", 0))
                    item = f"  {str(entry.get('name', '')):<24}  [{str(entry.get('section', 'intro'))}]  L{line:>5}  {marker}"
                    _safe_addnstr(stdscr, 2 + row, 0, item, width - 1)
                stdscr.refresh()
                try:
                    curses.curs_set(1)
                except Exception:
                    pass
                key = stdscr.getch()
                if key == 27:
                    mode = _MODE_CT_REF
                    try:
                        curses.curs_set(0)
                    except Exception:
                        pass
                    continue
                if key in (10, 13, curses.KEY_ENTER):
                    ct_ref_query = ct_ref_search_buf
                    _refresh_ct_ref_lines()
                    mode = _MODE_CT_REF_RESULTS
                    try:
                        curses.curs_set(0)
                    except Exception:
                        pass
                    continue
                if key in (curses.KEY_BACKSPACE, 127, 8):
                    ct_ref_search_buf = ct_ref_search_buf[:-1]
                    continue
                if 32 <= key <= 126:
                    ct_ref_search_buf += chr(key)
                    continue
                continue

            # -- COMPILE-TIME REFERENCE FILTER MODE --
            if mode == _MODE_CT_REF_FILTER:
                stdscr.erase()
                _safe_addnstr(stdscr, 0, 0, " CT Reference Filters ", width - 1, curses.A_BOLD)
                _safe_addnstr(stdscr, 1, 0, " Tab/j/k: field  Left/Right/Space: change  c: clear  Enter/Esc: close ", width - 1, curses.A_DIM)

                _N_CT_FILTER_FIELDS = 3
                row_y = 3

                section_label = f"  Section: < {ct_ref_sections[ct_ref_section_idx]} >"
                section_attr = curses.A_REVERSE if ct_ref_filter_field == 0 else 0
                _safe_addnstr(stdscr, row_y, 0, section_label, width - 1, section_attr)
                row_y += 1

                scope_label = f"  Scope:   < {ct_ref_scope_options[ct_ref_scope_idx]} >"
                scope_attr = curses.A_REVERSE if ct_ref_filter_field == 1 else 0
                _safe_addnstr(stdscr, row_y, 0, scope_label, width - 1, scope_attr)
                row_y += 1

                words_label = f"  Words:   < {'yes' if ct_ref_words_only else 'no'} >"
                words_attr = curses.A_REVERSE if ct_ref_filter_field == 2 else 0
                _safe_addnstr(stdscr, row_y, 0, words_label, width - 1, words_attr)
                row_y += 2

                preview = _filter_ct_ref_lines(ct_ref_query)
                summary = _ct_ref_filter_summary() or "none"
                status = f" active filters: {summary}  query: {ct_ref_query or '(none)'}  preview lines: {len(preview)} "
                _safe_addnstr(stdscr, height - 1, 0, status, width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()

                if key == 27 or key in (10, 13, curses.KEY_ENTER):
                    mode = _MODE_CT_REF
                    continue
                if key == ord("c"):
                    ct_ref_section_idx = 0
                    ct_ref_scope_idx = 0
                    ct_ref_words_only = False
                    _refresh_ct_ref_lines()
                    continue
                if key == 9 or key in (curses.KEY_DOWN, ord("j")):
                    ct_ref_filter_field = (ct_ref_filter_field + 1) % _N_CT_FILTER_FIELDS
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    ct_ref_filter_field = (ct_ref_filter_field - 1) % _N_CT_FILTER_FIELDS
                    continue

                if ct_ref_filter_field == 0:
                    if key in (curses.KEY_RIGHT, ord("l"), ord(" ")):
                        ct_ref_section_idx = (ct_ref_section_idx + 1) % len(ct_ref_sections)
                        _refresh_ct_ref_lines()
                        continue
                    if key in (curses.KEY_LEFT, ord("h")):
                        ct_ref_section_idx = (ct_ref_section_idx - 1) % len(ct_ref_sections)
                        _refresh_ct_ref_lines()
                        continue
                elif ct_ref_filter_field == 1:
                    if key in (curses.KEY_RIGHT, ord("l"), ord(" ")):
                        ct_ref_scope_idx = (ct_ref_scope_idx + 1) % len(ct_ref_scope_options)
                        _refresh_ct_ref_lines()
                        continue
                    if key in (curses.KEY_LEFT, ord("h")):
                        ct_ref_scope_idx = (ct_ref_scope_idx - 1) % len(ct_ref_scope_options)
                        _refresh_ct_ref_lines()
                        continue
                else:
                    if key in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l"), ord(" ")):
                        ct_ref_words_only = not ct_ref_words_only
                        _refresh_ct_ref_lines()
                        continue
                continue

            # -- COMPILE-TIME REFERENCE FUNCTION RESULTS MODE --
            if mode == _MODE_CT_REF_RESULTS:
                stdscr.erase()
                title = " CT Functions "
                if ct_ref_query.strip():
                    title = f" CT Functions /{ct_ref_query.strip()} "
                ct_filter_summary = _ct_ref_filter_summary()
                if ct_filter_summary:
                    title = title[:-1] + f" [{ct_filter_summary}] "
                _safe_addnstr(stdscr, 0, 0, title, width - 1, curses.A_BOLD)
                _render_tab_bar(stdscr, 1, width)
                _safe_addnstr(stdscr, 2, 0, " Enter detail  o jump-to-full  / search  f filters  c clear  j/k nav  PgUp/PgDn  Tab switch  q quit", width - 1, curses.A_DIM)

                body_height = max(1, height - 4)
                n_entries = len(ct_ref_result_entries)

                if n_entries > 0:
                    if ct_ref_result_selected >= n_entries:
                        ct_ref_result_selected = n_entries - 1
                    if ct_ref_result_selected < ct_ref_result_scroll:
                        ct_ref_result_scroll = ct_ref_result_selected
                    if ct_ref_result_selected >= ct_ref_result_scroll + body_height:
                        ct_ref_result_scroll = ct_ref_result_selected - body_height + 1
                    max_rscroll = max(0, n_entries - body_height)
                    if ct_ref_result_scroll > max_rscroll:
                        ct_ref_result_scroll = max_rscroll
                else:
                    ct_ref_result_selected = 0
                    ct_ref_result_scroll = 0
                    max_rscroll = 0

                if not ct_ref_result_entries:
                    _safe_addnstr(stdscr, 3, 0, "  (no compile-time functions match current query/filters)", width - 1, curses.A_DIM)
                else:
                    for row in range(body_height):
                        idx = ct_ref_result_scroll + row
                        if idx >= n_entries:
                            break
                        entry = ct_ref_result_entries[idx]
                        name = str(entry.get("name", ""))
                        section = str(entry.get("section", "intro"))
                        line_no = int(entry.get("line_no", 0))
                        example_mark = "ex" if bool(entry.get("has_example")) else "--"
                        item = f"  {name:<24} [{section:<10}] L{line_no:>5}  {example_mark}"
                        attr = curses.A_REVERSE if idx == ct_ref_result_selected else 0
                        _safe_addnstr(stdscr, 3 + row, 0, item, width - 1, attr)

                if ct_ref_result_entries:
                    cur = ct_ref_result_entries[ct_ref_result_selected]
                    pos = f" {ct_ref_result_selected + 1}/{len(ct_ref_result_entries)} "
                    summary = f" {str(cur.get('name', ''))}  section={str(cur.get('section', 'intro'))}  line={int(cur.get('line_no', 0))} "
                    _safe_addnstr(stdscr, height - 1, 0, summary + pos, width - 1, curses.A_DIM)
                else:
                    _safe_addnstr(stdscr, height - 1, 0, " 0/0 ", width - 1, curses.A_DIM)

                stdscr.refresh()
                key = stdscr.getch()

                if key in (ord("q"),):
                    return 0
                if key in (27, ord("h"), curses.KEY_LEFT):
                    mode = _MODE_CT_REF
                    continue
                if key == 9:  # Tab
                    active_tab = _TAB_LIBRARY
                    mode = _MODE_BROWSE
                    continue
                if key == ord("/"):
                    ct_ref_search_buf = ct_ref_query
                    mode = _MODE_CT_REF_SEARCH
                    continue
                if key == ord("f"):
                    mode = _MODE_CT_REF_FILTER
                    continue
                if key == ord("c"):
                    if ct_ref_query:
                        ct_ref_query = ""
                        _refresh_ct_ref_lines()
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    if ct_ref_result_selected > 0:
                        ct_ref_result_selected -= 1
                    continue
                if key in (curses.KEY_DOWN, ord("j")):
                    if ct_ref_result_selected + 1 < len(ct_ref_result_entries):
                        ct_ref_result_selected += 1
                    continue
                if key == curses.KEY_PPAGE:
                    ct_ref_result_selected = max(0, ct_ref_result_selected - body_height)
                    continue
                if key == curses.KEY_NPAGE:
                    ct_ref_result_selected = min(max(0, len(ct_ref_result_entries) - 1), ct_ref_result_selected + body_height)
                    continue
                if key == ord("g"):
                    ct_ref_result_selected = 0
                    ct_ref_result_scroll = 0
                    continue
                if key == ord("G"):
                    ct_ref_result_selected = max(0, len(ct_ref_result_entries) - 1)
                    continue
                if key in (10, 13, curses.KEY_ENTER, ord("l"), curses.KEY_RIGHT):
                    if ct_ref_result_entries:
                        selected_entry = ct_ref_result_entries[ct_ref_result_selected]
                        ct_ref_detail_lines = _build_ct_ref_detail_lines(selected_entry, width)
                        ct_ref_detail_line_no = int(selected_entry.get("line_no", 0))
                        ct_ref_detail_scroll = 0
                        mode = _MODE_CT_REF_DETAIL
                    continue
                if key == ord("o"):
                    if ct_ref_result_entries:
                        selected_entry = ct_ref_result_entries[ct_ref_result_selected]
                        line_no = int(selected_entry.get("line_no", 1))
                        info_scroll = max(0, line_no - 3)
                        mode = _MODE_CT_REF
                    continue
                if key == ord("L"):
                    info_lines = _L2_LICENSE_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_LICENSE
                    continue
                if key == ord("P"):
                    info_lines = _L2_PHILOSOPHY_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_PHILOSOPHY
                    continue
                if key == ord("?"):
                    info_lines = _L2_QA_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_QA
                    continue
                if key == ord("H"):
                    info_lines = _L2_HOW_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_HOW
                    continue
                continue

            # -- COMPILE-TIME REFERENCE FUNCTION DETAIL MODE --
            if mode == _MODE_CT_REF_DETAIL:
                stdscr.erase()
                title = ct_ref_detail_lines[0] if ct_ref_detail_lines else "CT Function Detail"
                _safe_addnstr(stdscr, 0, 0, f" {title} ", width - 1, curses.A_BOLD)
                _safe_addnstr(stdscr, 1, 0, " q/Esc: back  o: open in full reference  j/k: scroll  PgUp/PgDn ", width - 1, curses.A_DIM)
                body_height = max(1, height - 3)
                max_dscroll = max(0, len(ct_ref_detail_lines) - body_height)
                if ct_ref_detail_scroll > max_dscroll:
                    ct_ref_detail_scroll = max_dscroll
                for row in range(body_height):
                    li = ct_ref_detail_scroll + row
                    if li >= len(ct_ref_detail_lines):
                        break
                    _safe_addnstr(stdscr, 2 + row, 0, ct_ref_detail_lines[li], width - 1)
                pos_text = f" {ct_ref_detail_scroll + 1}-{min(ct_ref_detail_scroll + body_height, len(ct_ref_detail_lines))}/{len(ct_ref_detail_lines)} "
                _safe_addnstr(stdscr, height - 1, 0, pos_text, width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()

                if key in (27, ord("q"), ord("h"), curses.KEY_LEFT):
                    mode = _MODE_CT_REF_RESULTS
                    continue
                if key in (curses.KEY_DOWN, ord("j")):
                    if ct_ref_detail_scroll < max_dscroll:
                        ct_ref_detail_scroll += 1
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    if ct_ref_detail_scroll > 0:
                        ct_ref_detail_scroll -= 1
                    continue
                if key == curses.KEY_NPAGE:
                    ct_ref_detail_scroll = min(max_dscroll, ct_ref_detail_scroll + body_height)
                    continue
                if key == curses.KEY_PPAGE:
                    ct_ref_detail_scroll = max(0, ct_ref_detail_scroll - body_height)
                    continue
                if key == ord("g"):
                    ct_ref_detail_scroll = 0
                    continue
                if key == ord("G"):
                    ct_ref_detail_scroll = max_dscroll
                    continue
                if key == ord("o"):
                    info_scroll = max(0, ct_ref_detail_line_no - 3)
                    mode = _MODE_CT_REF
                    continue
                continue

            # -- COMPILE-TIME REFERENCE MODE --
            if mode == _MODE_CT_REF:
                stdscr.erase()
                title = " Compile-Time Reference "
                if ct_ref_query.strip():
                    title = f" Compile-Time Reference /{ct_ref_query.strip()} "
                ct_filter_summary = _ct_ref_filter_summary()
                if ct_filter_summary:
                    title = title[:-1] + f" [{ct_filter_summary}] "
                _safe_addnstr(stdscr, 0, 0, title, width - 1, curses.A_BOLD)
                _render_tab_bar(stdscr, 1, width)
                _safe_addnstr(stdscr, 2, 0, " / search  Enter/s functions  f filters  c clear  j/k scroll  PgUp/PgDn  Tab switch  ? Q&A  H how  P philosophy  L license  q quit", width - 1, curses.A_DIM)
                body_height = max(1, height - 4)
                max_iscroll = max(0, len(info_lines) - body_height)
                if info_scroll > max_iscroll:
                    info_scroll = max_iscroll
                for row in range(body_height):
                    li = info_scroll + row
                    if li >= len(info_lines):
                        break
                    _safe_addnstr(stdscr, 3 + row, 0, f"  {info_lines[li]}", width - 1)
                pos_text = f" {info_scroll + 1}-{min(info_scroll + body_height, len(info_lines))}/{len(info_lines)} "
                _safe_addnstr(stdscr, height - 1, 0, pos_text, width - 1, curses.A_DIM)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (27, ord("q")):
                    return 0
                if key == 9:  # Tab
                    active_tab = _TAB_LIBRARY
                    mode = _MODE_BROWSE
                    continue
                if key == ord("/"):
                    ct_ref_search_buf = ct_ref_query
                    mode = _MODE_CT_REF_SEARCH
                    continue
                if key == ord("f"):
                    mode = _MODE_CT_REF_FILTER
                    continue
                if key in (10, 13, curses.KEY_ENTER, ord("s")):
                    _refresh_ct_ref_results(reset_selection=False)
                    mode = _MODE_CT_REF_RESULTS
                    continue
                if key == ord("c"):
                    if ct_ref_query:
                        ct_ref_query = ""
                        _refresh_ct_ref_lines()
                    continue
                if key in (curses.KEY_DOWN, ord("j")):
                    if info_scroll < max_iscroll:
                        info_scroll += 1
                    continue
                if key in (curses.KEY_UP, ord("k")):
                    if info_scroll > 0:
                        info_scroll -= 1
                    continue
                if key == curses.KEY_NPAGE:
                    info_scroll = min(max_iscroll, info_scroll + body_height)
                    continue
                if key == curses.KEY_PPAGE:
                    info_scroll = max(0, info_scroll - body_height)
                    continue
                if key == ord("g"):
                    info_scroll = 0
                    continue
                if key == ord("G"):
                    info_scroll = max_iscroll
                    continue
                if key == ord("L"):
                    info_lines = _L2_LICENSE_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_LICENSE
                    continue
                if key == ord("P"):
                    info_lines = _L2_PHILOSOPHY_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_PHILOSOPHY
                    continue
                if key == ord("?"):
                    info_lines = _L2_QA_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_QA
                    continue
                if key == ord("H"):
                    info_lines = _L2_HOW_TEXT.splitlines()
                    info_scroll = 0
                    mode = _MODE_HOW
                    continue
                if key == ord("C"):
                    active_tab = _TAB_CT_REF
                    _refresh_ct_ref_lines()
                    mode = _MODE_CT_REF
                    continue
                continue

            # -- BROWSE MODE --
            list_height = max(1, height - 5)
            if selected < scroll:
                scroll = selected
            if selected >= scroll + list_height:
                scroll = selected - list_height + 1
            max_scroll = max(0, len(filtered) - list_height)
            if scroll > max_scroll:
                scroll = max_scroll

            stdscr.erase()
            kind_str = _FILTER_KINDS[filter_kind_idx]
            enabled_count = sum(1 for v in filter_files_enabled.values() if v)
            filter_info = ""
            has_kind_filter = kind_str != "all"
            has_file_filter = enabled_count < len(all_file_paths)
            has_sig_filter = filter_args >= 0 or filter_returns >= 0
            if has_kind_filter or has_file_filter or has_sig_filter or filter_extra_roots or filter_show_private or filter_show_macros:
                parts = []
                if has_kind_filter:
                    parts.append(f"kind={kind_str}")
                if has_file_filter:
                    parts.append(f"files={enabled_count}/{len(all_file_paths)}")
                if filter_args >= 0:
                    parts.append(f"args={filter_args}")
                if filter_returns >= 0:
                    parts.append(f"rets={filter_returns}")
                if filter_show_private:
                    parts.append("private")
                if filter_show_macros:
                    parts.append("macros")
                if filter_extra_roots:
                    parts.append(f"+{len(filter_extra_roots)} paths")
                filter_info = "  [" + ", ".join(parts) + "]"
            header = f" L2 docs  {len(filtered)}/{len(entries)}" + (f"  search: {query}" if query else "") + filter_info
            _safe_addnstr(stdscr, 0, 0, header, width - 1, curses.A_BOLD)
            _render_tab_bar(stdscr, 1, width)
            hint = " / search  f filters  r reload  Enter detail  Tab switch  C ct-ref  ? Q&A  H how  P philosophy  L license  q quit"
            _safe_addnstr(stdscr, 2, 0, hint, width - 1, curses.A_DIM)

            for row in range(list_height):
                idx = scroll + row
                if idx >= len(filtered):
                    break
                entry = filtered[idx]
                effect = entry.stack_effect if entry.stack_effect else ""
                kind_tag = f"[{entry.kind:5}]"
                name_part = f" {entry.name:24} "
                effect_part = f"{effect:30} "
                is_sel = idx == selected
                base_attr = curses.A_REVERSE if is_sel else 0
                y = 3 + row
                # Draw name
                _safe_addnstr(stdscr, y, 0, name_part, width - 1, base_attr | curses.A_BOLD if is_sel else base_attr)
                # Draw stack effect
                x = len(name_part)
                if x < width - 1:
                    _safe_addnstr(stdscr, y, x, effect_part, width - x - 1, base_attr)
                # Draw kind tag with color
                x2 = x + len(effect_part)
                if x2 < width - 1:
                    kind_color = _KIND_COLORS.get(entry.kind, 0) if not is_sel else 0
                    _safe_addnstr(stdscr, y, x2, kind_tag, width - x2 - 1, base_attr | kind_color)

            if filtered:
                current = filtered[selected]
                detail = f" {current.path}:{current.line}"
                if current.description:
                    detail += f"  {current.description}"
                _safe_addnstr(stdscr, height - 1, 0, detail, width - 1, curses.A_DIM)
            else:
                _safe_addnstr(stdscr, height - 1, 0, " No matches", width - 1, curses.A_DIM)

            stdscr.refresh()
            key = stdscr.getch()

            if key in (27, ord("q")):
                return 0
            if key == 9:  # Tab
                active_tab = _TAB_LANG_REF
                mode = _MODE_LANG_REF
                continue
            if key == ord("L"):
                info_lines = _L2_LICENSE_TEXT.splitlines()
                info_scroll = 0
                mode = _MODE_LICENSE
                continue
            if key == ord("P"):
                info_lines = _L2_PHILOSOPHY_TEXT.splitlines()
                info_scroll = 0
                mode = _MODE_PHILOSOPHY
                continue
            if key == ord("?"):
                info_lines = _L2_QA_TEXT.splitlines()
                info_scroll = 0
                mode = _MODE_QA
                continue
            if key == ord("H"):
                info_lines = _L2_HOW_TEXT.splitlines()
                info_scroll = 0
                mode = _MODE_HOW
                continue
            if key == ord("C"):
                active_tab = _TAB_CT_REF
                _refresh_ct_ref_lines()
                mode = _MODE_CT_REF
                continue
            if key == ord("/"):
                search_buf = query
                mode = _MODE_SEARCH
                continue
            if key == ord("f"):
                mode = _MODE_FILTER
                continue
            if key == ord("r"):
                if reload_fn is not None:
                    entries = reload_fn(include_private=filter_show_private, include_macros=filter_show_macros, extra_roots=filter_extra_roots)
                    _rebuild_file_list()
                    selected = 0
                    scroll = 0
                continue
            if key in (10, 13, curses.KEY_ENTER):
                if filtered:
                    detail_lines = _build_detail_lines(filtered[selected], width)
                    detail_scroll = 0
                    mode = _MODE_DETAIL
                continue
            if key in (curses.KEY_UP, ord("k")):
                if selected > 0:
                    selected -= 1
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                if selected + 1 < len(filtered):
                    selected += 1
                continue
            if key == curses.KEY_PPAGE:
                selected = max(0, selected - list_height)
                continue
            if key == curses.KEY_NPAGE:
                selected = min(max(0, len(filtered) - 1), selected + list_height)
                continue
            if key == ord("g"):
                selected = 0
                scroll = 0
                continue
            if key == ord("G"):
                selected = max(0, len(filtered) - 1)
                continue

        return 0

    return int(curses.wrapper(_app))


def run_docs_explorer(
    *,
    source: Optional[Path],
    include_paths: Sequence[Path],
    explicit_roots: Sequence[Path],
    initial_query: str,
    include_undocumented: bool = False,
    include_private: bool = False,
    include_tests: bool = False,
    ct_word_metadata_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> int:
    configure_runtime(ct_word_metadata_provider=ct_word_metadata_provider)
    roots = _build_default_docs_roots(
        source=source,
        include_paths=include_paths,
        explicit_roots=explicit_roots,
    )

    collect_opts: Dict[str, Any] = dict(
        include_undocumented=include_undocumented,
        include_private=include_private,
        include_tests=include_tests,
        include_macros=False,
    )

    def _reload(**overrides: Any) -> List[DocEntry]:
        extra = overrides.pop("extra_roots", [])
        opts = {**collect_opts, **overrides}
        return _collect_docs_for_runtime(roots, extra_roots=extra, **opts)

    entries = _reload()
    return _run_docs_tui(entries, initial_query=initial_query, reload_fn=_reload)


def _build_default_docs_roots(
    *,
    source: Optional[Path],
    include_paths: Sequence[Path],
    explicit_roots: Sequence[Path],
) -> List[Path]:
    roots: List[Path] = []
    if source is not None:
        roots.append(source.parent)
        roots.append(source)
    # Prefer user-selected roots first so symbol dedup keeps explicit docs
    # over fallback/default roots when names collide.
    roots.extend(explicit_roots)
    roots.extend(include_paths)
    # Keep default docs focused on project + stdlib surface.
    roots.extend([Path("."), Path("./stdlib")])
    return roots


def _collect_docs_for_runtime(
    roots: Sequence[Path],
    *,
    include_undocumented: bool,
    include_private: bool,
    include_tests: bool,
    include_macros: bool,
    include_libs: bool = False,
    extra_roots: Sequence[Path],
) -> List[DocEntry]:
    doc_files = _iter_doc_files(roots, include_tests=include_tests, include_libs=include_libs)
    entries = collect_docs(
        roots,
        include_undocumented=include_undocumented,
        include_private=include_private,
        include_tests=include_tests,
        include_macros=include_macros,
        include_libs=include_libs,
        files=doc_files,
    )
    compiled = _definition_metadata_entries(
        doc_files, include_undocumented=include_undocumented, include_private=include_private)
    if compiled:
        by_name = {entry.name: entry for entry in compiled}
        entries = [entry for entry in entries if entry.name not in by_name] + list(by_name.values())
        entries.sort(key=lambda item: (item.name.lower(), str(item.path), item.line))

    # Scan extra roots directly, bypassing _iter_doc_files skip filters.
    # Always include undocumented entries from user-added paths.
    if extra_roots:
        seen_names = {e.name for e in entries}
        scan_opts = dict(
            include_undocumented=True,
            include_private=True,
            include_macros=include_macros,
        )
        for p in extra_roots:
            ep = Path(p).expanduser().resolve()
            if not ep.exists():
                continue
            if ep.is_file() and ep.suffix == ".sl":
                for entry in _scan_doc_file(ep, **scan_opts):
                    if entry.name not in seen_names:
                        seen_names.add(entry.name)
                        entries.append(entry)
            elif ep.is_dir():
                for sl in sorted(ep.rglob("*.sl")):
                    for entry in _scan_doc_file(sl.resolve(), **scan_opts):
                        if entry.name not in seen_names:
                            seen_names.add(entry.name)
                            entries.append(entry)
        entries.sort(key=lambda item: (item.name.lower(), str(item.path), item.line))

    return entries


def _docs_entry_to_payload(entry: DocEntry, workspace_root: Path) -> Dict[str, Any]:
    abs_path = entry.path.resolve()
    try:
        rel_path = abs_path.relative_to(workspace_root).as_posix()
    except ValueError:
        rel_path = abs_path.as_posix()
    return {
        "name": entry.name,
        "stack_effect": entry.stack_effect,
        "description": entry.description,
        "kind": entry.kind,
        "path": rel_path,
        "line": int(entry.line),
    }


def _extract_docs_tui_assets() -> Dict[str, Any]:
        fallback = {
                "language_entries": [],
                "license_text": "",
                "philosophy_text": "",
                "qa_text": "",
                "how_text": "",
                "ct_base_text": "",
        }
        docs_path = Path(__file__)
        try:
                source = docs_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
                return fallback

        try:
                tree = ast.parse(source)
        except SyntaxError:
                return fallback

        fn_node: Optional[ast.FunctionDef] = None
        for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == "_run_docs_tui":
                        fn_node = node
                        break
        if fn_node is None:
                return fallback

        env: Dict[str, Any] = {}

        def _eval_expr(node: ast.AST) -> Any:
                if isinstance(node, ast.Constant):
                        return node.value

                if isinstance(node, ast.Name):
                        return env.get(node.id)

                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                        left = _eval_expr(node.left)
                        right = _eval_expr(node.right)
                        if isinstance(left, str) and isinstance(right, str):
                                return left + right
                        if isinstance(left, list) and isinstance(right, list):
                                return left + right
                        return None

                if isinstance(node, ast.List):
                        out: List[Any] = []
                        for elem in node.elts:
                                value = _eval_expr(elem)
                                if value is None and not isinstance(elem, ast.Constant):
                                        return None
                                out.append(value)
                        return out

                if isinstance(node, ast.Tuple):
                        out_items: List[Any] = []
                        for elem in node.elts:
                                value = _eval_expr(elem)
                                if value is None and not isinstance(elem, ast.Constant):
                                        return None
                                out_items.append(value)
                        return tuple(out_items)

                if isinstance(node, ast.Dict):
                        out_dict: Dict[Any, Any] = {}
                        for key_node, val_node in zip(node.keys, node.values):
                                key = _eval_expr(key_node) if key_node is not None else None
                                val = _eval_expr(val_node)
                                if key is None or val is None:
                                        return None
                                out_dict[key] = val
                        return out_dict

                if isinstance(node, ast.Call):
                        if (
                                isinstance(node.func, ast.Attribute)
                                and isinstance(node.func.value, ast.Name)
                                and node.func.value.id == "textwrap"
                                and node.func.attr == "indent"
                                and len(node.args) >= 2
                        ):
                                text_value = _eval_expr(node.args[0])
                                prefix_value = _eval_expr(node.args[1])
                                if isinstance(text_value, str) and isinstance(prefix_value, str):
                                        return textwrap.indent(text_value, prefix_value)
                        return None

                return None

        for stmt in fn_node.body:
                target_name: Optional[str] = None
                value_node: Optional[ast.AST] = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                        target_name = stmt.targets[0].id
                        value_node = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
                        target_name = stmt.target.id
                        value_node = stmt.value

                if not target_name or value_node is None:
                        continue

                value = _eval_expr(value_node)
                if value is not None:
                        env[target_name] = value

        result = dict(fallback)
        if isinstance(env.get("_LANG_REF_ENTRIES"), list):
                result["language_entries"] = env.get("_LANG_REF_ENTRIES", [])
        for env_key, result_key in (
                ("_L2_LICENSE_TEXT", "license_text"),
                ("_L2_PHILOSOPHY_TEXT", "philosophy_text"),
                ("_L2_QA_TEXT", "qa_text"),
                ("_L2_HOW_TEXT", "how_text"),
                ("_L2_CT_REF_TEXT", "ct_base_text"),
        ):
                value = env.get(env_key)
                if isinstance(value, str):
                        result[result_key] = value

        return result


def _normalize_language_example_line(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    text = re.sub(r"^[-*]\s+", "", text)
    text = re.sub(r"^\d+[.)]\s+", "", text)
    return text.strip()


def _is_language_example_line(raw_line: str) -> bool:
    stripped = raw_line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if raw_line.startswith(("  ", "\t")):
        return True
    if stripped.startswith(("-", "*", "#")):
        return True
    if re.match(r"^\d+[.)]\s+", stripped):
        return True
    return lowered.startswith(
        (
            "word ",
            "inline ",
            ":asm",
            "extern ",
            "macro ",
            "struct ",
            "cstruct ",
            "with ",
            "if ",
            "for ",
            "while ",
            "begin ",
            "else",
            "elif ",
            "end",
            "import ",
            "flags ",
            "cimport ",
            "ifdef ",
            "ifndef ",
            "elsedef ",
            "endif",
            "[",
            "{",
            '"',
            "'",
            "&",
            "compile-time ",
            "runtime ",
            "runtime-only ",
            "compile-only ",
            "immediate ",
        )
    )


def _split_language_detail_and_examples(detail_text: str) -> Tuple[str, List[str]]:
    cleaned_lines: List[str] = []
    examples: List[str] = []
    seen: Set[str] = set()
    if not detail_text:
        return "", examples

    lines = detail_text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("example:") or lowered.startswith("examples:"):
            inline = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            if inline:
                normalized = _normalize_language_example_line(inline)
                if normalized:
                    key = normalized.lower()
                    if key not in seen:
                        seen.add(key)
                        examples.append(normalized)

            idx += 1
            while idx < len(lines):
                candidate = lines[idx]
                candidate_stripped = candidate.strip()
                if not candidate_stripped:
                    idx += 1
                    continue
                if _is_language_example_line(candidate):
                    normalized = _normalize_language_example_line(candidate_stripped)
                    if normalized:
                        key = normalized.lower()
                        if key not in seen:
                            seen.add(key)
                            examples.append(normalized)
                    idx += 1
                    continue
                break

            while cleaned_lines and not cleaned_lines[-1].strip():
                cleaned_lines.pop()
            if idx < len(lines) and cleaned_lines and lines[idx].strip():
                cleaned_lines.append("")
            continue

        cleaned_lines.append(line.rstrip())
        idx += 1

    while cleaned_lines and not cleaned_lines[-1].strip():
        cleaned_lines.pop()

    return "\n".join(cleaned_lines).strip(), examples


def _extract_language_examples_from_detail(detail_text: str) -> List[str]:
    _, examples = _split_language_detail_and_examples(detail_text)
    return examples


def _normalize_language_examples(raw_examples: Any) -> List[str]:
    examples: List[str] = []
    seen: Set[str] = set()
    if isinstance(raw_examples, list):
        for value in raw_examples:
            text = _normalize_language_example_line(value)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            examples.append(text)
    return examples


def _normalize_language_entries(raw_entries: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_entries, list):
        return out
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue

        raw_detail_text = str(item.get("detail", "")).strip()
        detail_text, extracted_examples = _split_language_detail_and_examples(raw_detail_text)
        if not detail_text and not extracted_examples:
            detail_text = raw_detail_text

        examples = _normalize_language_examples(item.get("examples"))
        if extracted_examples:
            seen = {example.lower() for example in examples}
            for value in extracted_examples:
                key = value.lower()
                if key not in seen:
                    seen.add(key)
                    examples.append(value)

        out.append(
            {
                "name": name,
                "category": str(item.get("category", "General")).strip() or "General",
                "syntax": str(item.get("syntax", "")).strip(),
                "summary": str(item.get("summary", "")).strip(),
                "detail": detail_text,
                "examples": examples,
            }
        )
    out.sort(key=lambda item: (item["category"].lower(), item["name"].lower()))
    return out


def _ct_entry_to_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
        line_no = 0
        try:
                line_no = int(entry.get("line_no", 0))
        except Exception:
                line_no = 0

        examples: List[str] = []
        raw_examples = entry.get("examples")
        if isinstance(raw_examples, list):
                for value in raw_examples:
                        text = str(value).strip()
                        if text:
                                examples.append(text)
        if not examples:
                try:
                        examples = _split_example_lines(entry)
                except Exception:
                        examples = []

        return {
                "name": str(entry.get("name", "")),
                "category": str(entry.get("category", "Meta")),
                "scope": str(entry.get("scope", "all")),
                "stack_effect": str(entry.get("stack_effect", "")),
                "overview": str(entry.get("overview", "")),
                "examples": [line for line in examples if line],
                "line": line_no,
        }


def _format_source_excerpt_lines(
    lines: Sequence[str],
    focus_line: int,
    *,
    before: int = 4,
    after: int = 24,
    end_line: Optional[int] = None,
) -> str:
    if not lines:
        return "(source unavailable)"
    total = len(lines)
    line_no = int(focus_line) if focus_line else 1
    if line_no < 1:
        line_no = 1
    if line_no > total:
        line_no = total
    start = max(1, line_no - before)
    if end_line is None:
        end = min(total, line_no + after)
    else:
        try:
            forced_end = int(end_line)
        except Exception:
            forced_end = line_no + after
        if forced_end < line_no:
            forced_end = line_no
        end = min(total, forced_end)
    width = len(str(end))
    out: List[str] = []
    for n in range(start, end + 1):
        marker = ">" if n == line_no else " "
        out.append(f"{marker}{n:>{width}} | {lines[n - 1]}")
    return "\n".join(out)


_L2_WORD_DEF_RE = re.compile(r"^\s*(?:inline\s+)?word\s+[^\s]+")


def _strip_l2_comment_and_strings(line: str) -> str:
    out: List[str] = []
    in_string = False
    escape = False
    for ch in line:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            out.append(ch if ch.isspace() else " ")
            continue
        if ch == '"':
            in_string = True
            out.append(" ")
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out)


def _find_l2_definition_end_line(lines: Sequence[str], focus_line: int, *, max_scan: int = 800) -> int:
    if not lines:
        return 1
    total = len(lines)
    line_no = int(focus_line) if focus_line else 1
    if line_no < 1:
        line_no = 1
    if line_no > total:
        line_no = total

    head = lines[line_no - 1]
    if _L2_WORD_DEF_RE.match(head) is None:
        return min(total, line_no + 24)

    depth = 1
    openers = {"word", "if", "while", "for", "with"}
    scan_end = min(total, line_no + max_scan)
    for idx in range(line_no + 1, scan_end + 1):
        code = _strip_l2_comment_and_strings(lines[idx - 1]).lower()
        tokens = re.findall(r"\b(?:word|if|while|for|with|end)\b", code)
        for token in tokens:
            if token == "end":
                depth -= 1
            elif token in openers:
                depth += 1
        if depth <= 0:
            return idx

    return min(total, line_no + 120)


def _read_file_source_excerpt(path: Optional[Path], line_no: int) -> str:
    if path is None:
        return "(source file unavailable)"
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return "(unable to read source file)"
    lines = content.splitlines()
    end_line = _find_l2_definition_end_line(lines, line_no)
    return _format_source_excerpt_lines(lines, line_no, end_line=end_line)


def _build_docs_web_static_html(
    payload: Dict[str, Any],
    *,
    tab: str,
    query: str,
    selected: str = "",
    theme: str = "auto",
    workspace_root: Optional[Path] = None,
) -> str:
    tabs: List[Tuple[str, str]] = [
        ("library", "Library Docs"),
        ("language", "Language Reference"),
        ("ct", "Compile-Time Reference"),
        ("license", "License"),
        ("philosophy", "Philosophy of L2"),
        ("qa", "Q&A / Tips & Tricks"),
        ("how", "How L2 Works (Internals)"),
    ]
    valid_tabs = {tab_id for tab_id, _ in tabs}
    active_tab = tab if tab in valid_tabs else "library"
    active_theme = str(theme or "auto").strip().lower()
    if active_theme not in {"auto", "light", "dark"}:
        active_theme = "auto"
    q = str(query or "")
    terms = [part for part in q.lower().split() if part]
    selected_key = str(selected or "")

    def _esc(value: Any) -> str:
        return html.escape("" if value is None else str(value), quote=True)

    def _match(parts: Sequence[Any]) -> bool:
        if not terms:
            return True
        blob = " ".join(str(part or "") for part in parts).lower()
        return all(term in blob for term in terms)

    from urllib.parse import urlencode

    def _href_with_theme(theme_id: str, tab_id: str, q_text: str, sel_text: str = "") -> str:
        params: Dict[str, str] = {"tab": tab_id}
        if theme_id in {"light", "dark"}:
            params["theme"] = theme_id
        if q_text:
            params["q"] = q_text
        if sel_text:
            params["sel"] = sel_text
        return "/?" + urlencode(params)

    def _href(tab_id: str, q_text: str, sel_text: str = "") -> str:
        return _href_with_theme(active_theme, tab_id, q_text, sel_text)

    def _safe_resolve_source(path_text: str) -> Optional[Path]:
        if not path_text:
            return None
        try:
            p = Path(path_text)
            if not p.is_absolute():
                base = workspace_root if workspace_root is not None else Path(".").resolve()
                p = (base / p).resolve()
            else:
                p = p.resolve()
            if not p.is_file():
                return None
            return p
        except Exception:
            return None

    nav_links: List[str] = []
    for tab_id, label in tabs:
        href = _href(tab_id, q, "")
        cls = "tab active" if tab_id == active_tab else "tab"
        nav_links.append(f'<a class="{cls}" href="{_esc(href)}">{_esc(label)}</a>')

    body_html = ""
    match_count = 0
    total_count = 0
    selected_for_form = ""

    if active_tab == "library":
        entries = payload.get("library", {}).get("entries", [])
        if not isinstance(entries, list):
            entries = []
        total_count = len(entries)
        filtered: List[Tuple[str, Dict[str, Any]]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            kind = str(entry.get("kind", "word"))
            stack_effect = str(entry.get("stack_effect", "(no stack effect)"))
            description = str(entry.get("description", ""))
            path = str(entry.get("path", ""))
            line = int(entry.get("line", 0) or 0)
            if not _match([name, kind, stack_effect, description, path, line]):
                continue
            key = f"{name}|{path}|{line}"
            filtered.append((key, entry))

        match_count = len(filtered)
        if filtered and selected_key not in {key for key, _ in filtered}:
            selected_key = filtered[0][0]
        selected_for_form = selected_key

        list_items: List[str] = []
        selected_entry: Optional[Dict[str, Any]] = None
        for key, entry in filtered:
            name = str(entry.get("name", ""))
            kind = str(entry.get("kind", "word"))
            stack_effect = str(entry.get("stack_effect", "(no stack effect)"))
            description = str(entry.get("description", ""))
            path = str(entry.get("path", ""))
            line = int(entry.get("line", 0) or 0)
            if key == selected_key:
                selected_entry = entry
            active_cls = " item-active" if key == selected_key else ""
            href = _href("library", q, key)
            list_items.append(
                f"<li class=\"{active_cls.strip()}\">"
                f"<a class=\"entrybox\" href=\"{_esc(href)}\">"
                f"<div class=\"entry\">{_esc(name)}</div>"
                f"<div class=\"meta\">{_esc(kind)} | {_esc(stack_effect)}</div>"
                f"<div class=\"desc\">{_esc(description or (path + ':' + str(line)))}</div>"
                "</a>"
                "</li>"
            )

        if selected_entry is not None:
            name = str(selected_entry.get("name", ""))
            kind = str(selected_entry.get("kind", "word"))
            stack_effect = str(selected_entry.get("stack_effect", "(no stack effect)"))
            description = str(selected_entry.get("description", ""))
            path = str(selected_entry.get("path", ""))
            line = int(selected_entry.get("line", 0) or 0)
            src_path = _safe_resolve_source(path)
            source_excerpt = _read_file_source_excerpt(src_path, line)
            detail_html = (
                f"<h2 style=\"margin:0 0 8px;font-size:18px;\">{_esc(name)}</h2>"
                f"<div class=\"kv\"><strong>Kind:</strong> {_esc(kind)}</div>"
                f"<div class=\"kv\"><strong>Stack Effect:</strong> {_esc(stack_effect)}</div>"
                f"<div class=\"kv\"><strong>Source:</strong> {_esc(path)}:{_esc(line)}</div>"
                "<div class=\"label\">Description</div>"
                f"<pre class=\"mini\">{_esc(description or 'No description.')}</pre>"
                "<div class=\"label\">Source Excerpt</div>"
                f"<pre class=\"full\">{_esc(source_excerpt)}</pre>"
            )
        else:
            detail_html = "<p>Select an entry to view details.</p>"

        if list_items:
            body_html = (
                "<div class=\"split\">"
                "<div class=\"listcol\"><p class=\"hint\">Click a function to inspect details and source.</p><ul class=\"rows\">"
                + "".join(list_items)
                + "</ul></div>"
                + f"<div class=\"detailcol\">{detail_html}</div>"
                + "</div>"
            )
        else:
            body_html = "<p>No matching entries.</p>"

    elif active_tab == "language":
        entries = payload.get("language", {}).get("entries", [])
        if not isinstance(entries, list):
            entries = []
        total_count = len(entries)
        filtered: List[Tuple[str, Dict[str, Any]]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            category = str(entry.get("category", "General"))
            syntax = str(entry.get("syntax", ""))
            summary = str(entry.get("summary", ""))
            detail = str(entry.get("detail", ""))
            example_text = " ".join(_normalize_language_examples(entry.get("examples", [])))
            if not _match([name, category, syntax, summary, detail, example_text]):
                continue
            key = f"{name}|{category}"
            filtered.append((key, entry))

        match_count = len(filtered)
        if filtered and selected_key not in {key for key, _ in filtered}:
            selected_key = filtered[0][0]
        selected_for_form = selected_key

        list_items: List[str] = []
        selected_entry: Optional[Dict[str, Any]] = None
        for key, entry in filtered:
            name = str(entry.get("name", ""))
            category = str(entry.get("category", "General"))
            syntax = str(entry.get("syntax", ""))
            summary = str(entry.get("summary", ""))
            detail = str(entry.get("detail", ""))
            if key == selected_key:
                selected_entry = entry
            active_cls = " item-active" if key == selected_key else ""
            href = _href("language", q, key)
            list_items.append(
                f"<li class=\"{active_cls.strip()}\">"
                f"<a class=\"entrybox\" href=\"{_esc(href)}\">"
                f"<div class=\"entry\">{_esc(name)}</div>"
                f"<div class=\"meta\">{_esc(category)}</div>"
                f"<div class=\"desc\">{_esc(syntax or '(syntax not provided)')}</div>"
                f"<div class=\"desc\">{_esc(summary or detail)}</div>"
                "</a>"
                "</li>"
            )

        if selected_entry is not None:
            name = str(selected_entry.get("name", ""))
            category = str(selected_entry.get("category", "General"))
            syntax = str(selected_entry.get("syntax", ""))
            summary = str(selected_entry.get("summary", ""))
            detail = str(selected_entry.get("detail", ""))
            examples = _normalize_language_examples(selected_entry.get("examples", []))
            ex_text = "\n".join(examples) if examples else "(no examples)"
            detail_html = (
                f"<h2 style=\"margin:0 0 8px;font-size:18px;\">{_esc(name)}</h2>"
                f"<div class=\"kv\"><strong>Category:</strong> {_esc(category)}</div>"
                "<div class=\"label\">Syntax</div>"
                f"<pre class=\"mini\">{_esc(syntax or '(syntax not provided)')}</pre>"
                "<div class=\"label\">Summary</div>"
                f"<pre class=\"mini\">{_esc(summary or '(no summary)')}</pre>"
                "<div class=\"label\">Description</div>"
                f"<pre class=\"mini\">{_esc(detail or '(no description)')}</pre>"
                "<div class=\"label\">Examples</div>"
                f"<pre class=\"mini\">{_esc(ex_text)}</pre>"
            )
        else:
            detail_html = "<p>Select an entry to view details.</p>"

        if list_items:
            body_html = (
                "<div class=\"split\">"
                "<div class=\"listcol\"><p class=\"hint\">Click an entry to inspect details and examples.</p><ul class=\"rows\">"
                + "".join(list_items)
                + "</ul></div>"
                + f"<div class=\"detailcol\">{detail_html}</div>"
                + "</div>"
            )
        else:
            body_html = "<p>No matching entries.</p>"

    elif active_tab == "ct":
        entries = payload.get("ct", {}).get("entries", [])
        if not isinstance(entries, list):
            entries = []
        total_count = len(entries)
        filtered: List[Tuple[str, Dict[str, Any]]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", ""))
            category = str(entry.get("category", "Meta"))
            scope = str(entry.get("scope", "all"))
            stack_effect = str(entry.get("stack_effect", ""))
            overview = str(entry.get("overview", ""))
            examples = entry.get("examples", [])
            if not isinstance(examples, list):
                examples = []
            if not _match([name, category, scope, stack_effect, overview, " ".join(str(x) for x in examples)]):
                continue
            line = int(entry.get("line", 0) or 0)
            key = f"{name}|{line}"
            filtered.append((key, entry))

        match_count = len(filtered)
        if filtered and selected_key not in {key for key, _ in filtered}:
            selected_key = filtered[0][0]
        selected_for_form = selected_key

        list_items = []
        selected_entry = None
        for key, entry in filtered:
            name = str(entry.get("name", ""))
            category = str(entry.get("category", "Meta"))
            scope = str(entry.get("scope", "all"))
            stack_effect = str(entry.get("stack_effect", ""))
            overview = str(entry.get("overview", ""))
            if key == selected_key:
                selected_entry = entry
            active_cls = " item-active" if key == selected_key else ""
            href = _href("ct", q, key)
            list_items.append(
                f"<li class=\"{active_cls.strip()}\">"
                f"<a class=\"entrybox\" href=\"{_esc(href)}\">"
                f"<div class=\"entry\">{_esc(name)}</div>"
                f"<div class=\"meta\">{_esc(category)} | {_esc(scope)} | {_esc(stack_effect or '(see docs)')}</div>"
                f"<div class=\"desc\">{_esc(overview or 'No overview.')}</div>"
                "</a>"
                "</li>"
            )

        if selected_entry is not None:
            name = str(selected_entry.get("name", ""))
            category = str(selected_entry.get("category", "Meta"))
            scope = str(selected_entry.get("scope", "all"))
            stack_effect = str(selected_entry.get("stack_effect", ""))
            overview = str(selected_entry.get("overview", ""))
            line = int(selected_entry.get("line", 0) or 0)
            examples = selected_entry.get("examples", [])
            if not isinstance(examples, list):
                examples = []
            ex_text = "\n".join("- " + str(x) for x in examples if str(x).strip()) or "(no examples)"
            detail_html = (
                f"<h2 style=\"margin:0 0 8px;font-size:18px;\">{_esc(name)}</h2>"
                f"<div class=\"kv\"><strong>Category:</strong> {_esc(category)}</div>"
                f"<div class=\"kv\"><strong>Scope:</strong> {_esc(scope)}</div>"
                f"<div class=\"kv\"><strong>Stack Effect:</strong> {_esc(stack_effect or '(see docs)')}</div>"
                f"<div class=\"kv\"><strong>Source Line:</strong> {_esc(line)}</div>"
                "<div class=\"label\">Overview</div>"
                f"<pre class=\"mini\">{_esc(overview or 'No overview.')}</pre>"
                "<div class=\"label\">Examples</div>"
                f"<pre class=\"mini\">{_esc(ex_text)}</pre>"
            )
        else:
            detail_html = "<p>Select an entry to view details.</p>"

        if list_items:
            body_html = (
                "<div class=\"split\">"
                "<div class=\"listcol\"><p class=\"hint\">Click a function to inspect details.</p><ul class=\"rows\">"
                + "".join(list_items)
                + "</ul></div>"
                + f"<div class=\"detailcol\">{detail_html}</div>"
                + "</div>"
            )
        else:
            body_html = "<p>No matching entries.</p>"

    else:
        info = payload.get("info", {})
        if not isinstance(info, dict):
            info = {}
        key_map = {
            "license": "license",
            "philosophy": "philosophy",
            "qa": "qa",
            "how": "how",
        }
        text = str(info.get(key_map.get(active_tab, ""), ""))
        lines = text.splitlines()
        total_count = len(lines)
        if terms:
            lines = [line for line in lines if all(term in line.lower() for term in terms)]
        match_count = len(lines)
        shown = "\n".join(lines) if lines else "No matching lines."
        body_html = f'<pre class="full">{_esc(shown)}</pre>'

    if active_tab in {"library", "language", "ct"}:
        meta_text = f"{match_count} / {total_count} entries"
    else:
        meta_text = f"{match_count} / {total_count} lines"

    selected_input = (
        f"<input type=\"hidden\" name=\"sel\" value=\"{_esc(selected_for_form)}\" />"
        if selected_for_form and active_tab in {"library", "language", "ct"}
        else ""
    )
    theme_input = (
        f"<input type=\"hidden\" name=\"theme\" value=\"{_esc(active_theme)}\" />"
        if active_theme != "auto"
        else ""
    )
    sel_for_theme = (
        selected_for_form if selected_for_form and active_tab in {"library", "language", "ct"} else ""
    )
    theme_links = [
        f"<a class=\"btn theme-btn{' active' if active_theme == 'auto' else ''}\" href=\"{_esc(_href_with_theme('auto', active_tab, q, sel_for_theme))}\">Auto</a>",
        f"<a class=\"btn theme-btn{' active' if active_theme == 'light' else ''}\" href=\"{_esc(_href_with_theme('light', active_tab, q, sel_for_theme))}\">Light</a>",
        f"<a class=\"btn theme-btn{' active' if active_theme == 'dark' else ''}\" href=\"{_esc(_href_with_theme('dark', active_tab, q, sel_for_theme))}\">Dark</a>",
    ]

    active_label = next((label for tab_id, label in tabs if tab_id == active_tab), active_tab)
    return (
        "<!doctype html>"
        f"<html lang=\"en\" data-theme=\"{_esc(active_theme)}\"><head><meta charset=\"utf-8\" />"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />"
        "<title>L2 Docs Explorer</title>"
        "<style>"
        ":root{color-scheme:light dark;--bg:#f5f8fc;--fg:#0f2238;--panel:#ffffff;--panel-alt:#f8fbff;--panel-soft:#fbfdff;--border:#d6e1ef;--border-strong:#cbd8ea;--tab-border:#c2d2e6;--tab-fg:#204364;--tab-active-bg:#dbeaff;--tab-active-border:#1d5aa8;--btn-bg:#eef5ff;--btn-border:#b9cde4;--btn-fg:#1f4265;--field-bg:#ffffff;--field-border:#bdcde1;--field-fg:#0f2238;--hint:#4a6382;--subinfo:#486381;--entry:#1a3f64;--meta:#345676;--desc:#35516f;--kv:#2f4c69;--label:#566f8d;--row-border:#ecf2fb;--row-hover:#f2f8ff;--row-active:#eef6ff;--active-accent:#1d5aa8;--code-bg:#f7fbff;}"
        "@media (prefers-color-scheme: dark){html[data-theme='auto']{color-scheme:dark;--bg:#0f1724;--fg:#e6eef9;--panel:#131d2b;--panel-alt:#162233;--panel-soft:#1a2738;--border:#2a3a52;--border-strong:#324764;--tab-border:#3a5477;--tab-fg:#c5d8f2;--tab-active-bg:#1f3553;--tab-active-border:#6ca3ff;--btn-bg:#1e314b;--btn-border:#446186;--btn-fg:#d8e8ff;--field-bg:#101a28;--field-border:#3d5474;--field-fg:#e6eef9;--hint:#9fb6d8;--subinfo:#9ab3d6;--entry:#d7e8ff;--meta:#9eb6d8;--desc:#a8c0e2;--kv:#abc3e6;--label:#89a7cf;--row-border:#24364f;--row-hover:#1d2d44;--row-active:#223955;--active-accent:#78adff;--code-bg:#122033;}}"
        "html[data-theme='light']{color-scheme:light;--bg:#f5f8fc;--fg:#0f2238;--panel:#ffffff;--panel-alt:#f8fbff;--panel-soft:#fbfdff;--border:#d6e1ef;--border-strong:#cbd8ea;--tab-border:#c2d2e6;--tab-fg:#204364;--tab-active-bg:#dbeaff;--tab-active-border:#1d5aa8;--btn-bg:#eef5ff;--btn-border:#b9cde4;--btn-fg:#1f4265;--field-bg:#ffffff;--field-border:#bdcde1;--field-fg:#0f2238;--hint:#4a6382;--subinfo:#486381;--entry:#1a3f64;--meta:#345676;--desc:#35516f;--kv:#2f4c69;--label:#566f8d;--row-border:#ecf2fb;--row-hover:#f2f8ff;--row-active:#eef6ff;--active-accent:#1d5aa8;--code-bg:#f7fbff;}"
        "html[data-theme='dark']{color-scheme:dark;--bg:#0f1724;--fg:#e6eef9;--panel:#131d2b;--panel-alt:#162233;--panel-soft:#1a2738;--border:#2a3a52;--border-strong:#324764;--tab-border:#3a5477;--tab-fg:#c5d8f2;--tab-active-bg:#1f3553;--tab-active-border:#6ca3ff;--btn-bg:#1e314b;--btn-border:#446186;--btn-fg:#d8e8ff;--field-bg:#101a28;--field-border:#3d5474;--field-fg:#e6eef9;--hint:#9fb6d8;--subinfo:#9ab3d6;--entry:#d7e8ff;--meta:#9eb6d8;--desc:#a8c0e2;--kv:#abc3e6;--label:#89a7cf;--row-border:#24364f;--row-hover:#1d2d44;--row-active:#223955;--active-accent:#78adff;--code-bg:#122033;}"
        "body{margin:0;padding:16px;font-family:Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--fg);}"
        ".shell{max-width:1320px;margin:0 auto;background:var(--panel);border:1px solid var(--border-strong);border-radius:12px;overflow:hidden;}"
        ".head{padding:14px 16px;border-bottom:1px solid var(--border);background:var(--panel-alt);}"
        ".subinfo{margin-top:6px;color:var(--subinfo);font-size:13px;}"
        ".tabs{display:flex;flex-wrap:wrap;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);background:var(--panel-soft);}"
        ".tab{display:inline-block;padding:6px 10px;border:1px solid var(--tab-border);border-radius:999px;text-decoration:none;color:var(--tab-fg);background:var(--panel);}"
        ".tab.active{background:var(--tab-active-bg);border-color:var(--tab-active-border);font-weight:600;}"
        ".controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 12px;border-bottom:1px solid var(--border);background:var(--panel-alt);}"
        ".field{min-width:260px;flex:1;border:1px solid var(--field-border);border-radius:8px;padding:8px 10px;background:var(--field-bg);color:var(--field-fg);}"
        ".btn{border:1px solid var(--btn-border);background:var(--btn-bg);color:var(--btn-fg);border-radius:8px;padding:8px 10px;text-decoration:none;display:inline-block;}"
        ".theme-group{margin-left:auto;display:flex;gap:6px;align-items:center;}"
        ".theme-btn{min-width:56px;text-align:center;}"
        ".theme-btn.active{border-color:var(--tab-active-border);box-shadow:inset 0 0 0 1px var(--tab-active-border);font-weight:600;}"
        ".content{padding:12px;}"
        ".hint{margin:0 0 8px;color:var(--hint);font-size:13px;}"
        ".split{display:grid;grid-template-columns:minmax(340px,40%) 1fr;gap:12px;height:min(74vh,820px);min-height:520px;}"
        ".listcol{min-width:0;overflow:auto;overscroll-behavior:contain;border:1px solid var(--border);border-radius:10px;background:var(--panel);padding:8px;}"
        ".detailcol{min-width:0;overflow:auto;overscroll-behavior:contain;border:1px solid var(--border);border-radius:10px;background:var(--panel-soft);padding:10px;}"
        ".rows{list-style:none;margin:0;padding:0;}"
        ".rows li{padding:0;border-bottom:1px solid var(--row-border);border-left:3px solid transparent;}"
        ".rows li:last-child{border-bottom:0;}"
        ".rows li:hover{background:var(--row-hover);}"
        ".rows li.item-active{background:var(--row-active);border-left-color:var(--active-accent);}"
        ".entrybox{display:block;padding:10px 10px;text-decoration:none;color:inherit;}"
        ".entry{font-family:Consolas,monospace;font-weight:600;color:var(--entry);text-decoration:none;}"
        ".entrybox:hover .entry{text-decoration:underline;}"
        ".name{font-family:Consolas,monospace;font-weight:600;}"
        ".meta{font-size:12px;color:var(--meta);margin-top:4px;white-space:pre-wrap;word-break:break-word;}"
        ".desc{font-size:13px;color:var(--desc);margin-top:4px;white-space:pre-wrap;word-break:break-word;}"
        ".kv{font-size:13px;color:var(--kv);margin-top:4px;white-space:pre-wrap;word-break:break-word;}"
        ".label{margin:10px 0 6px;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--label);}"
        ".mini{margin:6px 0 0;padding:8px;border:1px solid var(--border);border-radius:8px;background:var(--code-bg);white-space:pre-wrap;word-break:break-word;}"
        ".full{margin:0;padding:10px;border:1px solid var(--border);border-radius:8px;background:var(--code-bg);white-space:pre-wrap;word-break:break-word;overflow:auto;}"
        "@media (max-width: 980px){.split{grid-template-columns:1fr;height:auto;min-height:0;}.listcol,.detailcol{overflow:visible;overscroll-behavior:auto;}.theme-group{margin-left:0;}}"
        "</style></head><body>"
        "<div class=\"shell\">"
        "<div class=\"head\"><h1 style=\"margin:0;font-size:20px;\">L2 Docs Explorer</h1>"
        "<div class=\"subinfo\">"
        f"Showing: {_esc(active_label)} | {_esc(meta_text)}"
        "</div></div>"
        f"<nav class=\"tabs\">{''.join(nav_links)}</nav>"
        "<form class=\"controls\" method=\"get\" action=\"/\">"
        f"<input type=\"hidden\" name=\"tab\" value=\"{_esc(active_tab)}\" />"
        f"{selected_input}"
        f"{theme_input}"
        f"<input class=\"field\" type=\"search\" name=\"q\" value=\"{_esc(q)}\" placeholder=\"Search current tab...\" />"
        "<button class=\"btn\" type=\"submit\">Search</button>"
        f"<a class=\"btn\" href=\"{_esc(_href(active_tab, '', ''))}\">Clear</a>"
        "<a class=\"btn\" href=\"/api/docs\">Raw API</a>"
        f"<div class=\"theme-group\">{''.join(theme_links)}</div>"
        "</form>"
        f"<div class=\"content\">{body_html}</div>"
        "</div></body></html>"
    )


def run_docs_serve(
    *,
    source: Optional[Path],
    include_paths: Sequence[Path],
    explicit_roots: Sequence[Path],
    initial_query: str,
    include_undocumented: bool = False,
    include_private: bool = False,
    include_tests: bool = False,
    host: str = "127.0.0.1",
    port: int = 8008,
    open_browser: bool = True,
    ct_word_metadata_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> int:
    configure_runtime(ct_word_metadata_provider=ct_word_metadata_provider)

    roots = _build_default_docs_roots(
        source=source,
        include_paths=include_paths,
        explicit_roots=explicit_roots,
    )
    entries = _collect_docs_for_runtime(
        roots,
        include_undocumented=include_undocumented,
        include_private=include_private,
        include_tests=include_tests,
        include_macros=False,
        extra_roots=[],
    )

    workspace_root = Path(".").resolve()
    library_entries = [_docs_entry_to_payload(entry, workspace_root) for entry in entries]
    library_entries.sort(key=lambda item: (str(item.get("name", "")).lower(), str(item.get("path", ""))))

    tui_assets = _extract_docs_tui_assets()
    language_entries = _normalize_language_entries(tui_assets.get("language_entries", []))

    ct_base_text = str(tui_assets.get("ct_base_text", ""))
    ct_entries: List[Dict[str, Any]] = []
    ct_full_text = ct_base_text
    if ct_base_text:
        try:
            ct_bundle = build_ct_reference_bundle(ct_base_text, _collect_ct_word_metadata())
            ct_summary_text = str(ct_bundle.get("summary_text", ""))
            ct_appendix_text = str(ct_bundle.get("appendix_text", ""))
            ct_full_text = ct_base_text + ct_summary_text + ct_appendix_text
            raw_entries = ct_bundle.get("entries", [])
            if isinstance(raw_entries, list):
                for entry in attach_ct_entry_line_numbers(ct_full_text, raw_entries):
                    if isinstance(entry, dict):
                        ct_entries.append(_ct_entry_to_payload(entry))
        except Exception:
            ct_entries = []

    ct_entries.sort(key=lambda item: str(item.get("name", "")).lower())

    payload = {
        "library": {
            "entries": library_entries,
            "count": len(library_entries),
            "kinds": sorted({str(item.get("kind", "")) for item in library_entries}),
        },
        "language": {
            "entries": language_entries,
            "count": len(language_entries),
            "categories": sorted({str(item.get("category", "General")) for item in language_entries}),
        },
        "ct": {
            "entries": ct_entries,
            "count": len(ct_entries),
            "categories": sorted({str(item.get("category", "Meta")) for item in ct_entries}),
            "scopes": sorted({str(item.get("scope", "all")) for item in ct_entries}),
        },
        "info": {
            "license": str(tui_assets.get("license_text", "")),
            "philosophy": str(tui_assets.get("philosophy_text", "")),
            "qa": str(tui_assets.get("qa_text", "")),
            "how": str(tui_assets.get("how_text", "")),
        },
    }

    json_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    from http import HTTPStatus
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class _DocsHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html", "/docs-static"):
                query_map = parse_qs(parsed.query or "")
                static_tab = str(query_map.get("tab", ["library"])[0] or "library")
                static_query = str(query_map.get("q", [initial_query])[0] or "")
                static_sel = str(query_map.get("sel", [""])[0] or "")
                static_theme = str(query_map.get("theme", ["auto"])[0] or "auto")
                static_html = _build_docs_web_static_html(
                    payload,
                    tab=static_tab,
                    query=static_query,
                    selected=static_sel,
                    theme=static_theme,
                    workspace_root=workspace_root,
                )
                self._send(int(HTTPStatus.OK), static_html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/docs":
                self._send(int(HTTPStatus.OK), json_bytes, "application/json; charset=utf-8")
                return
            self._send(int(HTTPStatus.NOT_FOUND), b"not found\n", "text/plain; charset=utf-8")

    try:
        server = ThreadingHTTPServer((host, int(port)), _DocsHandler)
    except OSError as exc:
        raise RuntimeError(f"unable to start docs server on {host}:{port}: {exc}") from exc

    display_host = host
    if host in {"0.0.0.0", "::"}:
        display_host = "127.0.0.1"
    url = f"http://{display_host}:{int(port)}/"

    print(f"[info] docs web ui listening on {url}")
    print(
        "[info] indexed tabs: "
        f"library={len(library_entries)} "
        f"language={len(language_entries)} "
        f"ct={len(ct_entries)}"
    )
    print("[info] mode: static docs only at / (with tab/search/detail views)")
    print("[info] press Ctrl+C to stop")

    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url, new=2)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[info] docs web ui stopped")
    finally:
        server.server_close()

    return 0


def run_docs_cli(
    *,
    source: Optional[Path],
    include_paths: Sequence[Path],
    explicit_roots: Sequence[Path],
    initial_query: str,
    include_undocumented: bool = False,
    include_private: bool = False,
    include_tests: bool = False,
    ct_word_metadata_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
) -> int:
    return run_docs_explorer(
        source=source,
        include_paths=include_paths,
        explicit_roots=explicit_roots,
        initial_query=initial_query,
        include_undocumented=include_undocumented,
        include_private=include_private,
        include_tests=include_tests,
        ct_word_metadata_provider=ct_word_metadata_provider,
    )


def _is_plausible_word_name(name: str) -> bool:
    if not name:
        return False
    low = name.lower()
    if low in {"word", "call:", "example:", "overview:", "category:", "scope:"}:
        return False
    if name.startswith(("-", "*", "->", "<")):
        return False
    return bool(re.search(r"[A-Za-z0-9]", name))


def extract_ct_ref_entry_details(
    doc_text: str,
    *,
    allowed_names: Optional[Set[str]] = None,
) -> Dict[str, Dict[str, str]]:
    details: Dict[str, Dict[str, str]] = {}
    pending: List[Tuple[str, str]] = []
    desc_lines: List[str] = []
    entry_line_re = re.compile(r"^\s{2,}([A-Za-z0-9][A-Za-z0-9_?.:+\-*/>=!&]*)(?:\s+(.*))?$")
    allowed = {name for name in (allowed_names or set()) if _is_plausible_word_name(name)}

    def _flush_pending() -> None:
        nonlocal pending, desc_lines
        if not pending:
            return
        description = " ".join(part.strip() for part in desc_lines if part.strip()).strip()
        for name, stack_effect in pending:
            slot = details.setdefault(name, {"stack": "", "description": ""})
            if stack_effect and not slot["stack"]:
                slot["stack"] = stack_effect
            if description and not slot["description"]:
                slot["description"] = description
        pending = []
        desc_lines = []

    lines = doc_text.splitlines()
    lines.append("")
    for line in lines:
        match = entry_line_re.match(line)
        if match:
            name = match.group(1).strip()
            stack_part = (match.group(2) or "").strip()
            if (
                _is_plausible_word_name(name)
                and not name.startswith(("\u00a7", "=", "-", "_"))
                and "->" in stack_part
                and "[" in stack_part
                and (not allowed or name in allowed)
            ):
                if desc_lines:
                    _flush_pending()
                pending.append((name, stack_part))
                continue

        if pending and line.startswith("    ") and line.strip():
            desc_lines.append(line.strip())
            continue

        if pending:
            _flush_pending()

    return details


def _name_phrase(word_name: str) -> str:
    phrase = word_name
    for prefix in ("ct-", "list-", "map-", "string-", "token-", "prelude-", "data-", "bss-"):
        if phrase.startswith(prefix):
            phrase = phrase[len(prefix):]
            break
    phrase = phrase.replace("?", "")
    phrase = phrase.replace("-", " ")
    return phrase.strip() or word_name


def _target_phrase(word_name: str, prefix: str) -> str:
    if not word_name.startswith(prefix):
        return _name_phrase(word_name)
    return _name_phrase(word_name[len(prefix):])


def _semantic_overview_from_name(word_name: str, category: str, stack_effect: str) -> Optional[str]:
    if word_name.endswith("?"):
        subject = _name_phrase(word_name)
        return (
            f"Predicate for {subject}; returns 1 when the condition holds and 0 otherwise."
        )

    if word_name.startswith("ct-set-"):
        target = _target_phrase(word_name, "ct-set-")
        return f"Sets compile-time {target} configuration for the active {category.lower()} pipeline."

    if word_name.startswith("ct-get-"):
        target = _target_phrase(word_name, "ct-get-")
        return f"Reads current compile-time {target} state for diagnostics and metaprogramming decisions."

    if word_name.startswith("ct-clear-"):
        target = _target_phrase(word_name, "ct-clear-")
        return f"Clears compile-time {target} state and returns cleanup status/count where available."

    if word_name.startswith("ct-list-"):
        target = _target_phrase(word_name, "ct-list-")
        return f"Returns a compile-time list snapshot for {target}, suitable for introspection and tooling."

    if word_name.startswith("ct-add-"):
        target = _target_phrase(word_name, "ct-add-")
        return f"Registers a new compile-time {target} entry and returns the effective rule/name handle."

    if word_name.startswith("ct-remove-"):
        target = _target_phrase(word_name, "ct-remove-")
        return f"Removes compile-time {target} state by name and reports whether removal happened."

    if word_name.startswith("ct-register-"):
        target = _target_phrase(word_name, "ct-register-")
        return f"Registers compile-time {target} behavior so parser/runtime hooks can discover it."

    if word_name.startswith("ct-unregister-"):
        target = _target_phrase(word_name, "ct-unregister-")
        return f"Unregisters compile-time {target} behavior and leaves parser state consistent."

    if word_name.startswith("ct-control-"):
        target = _target_phrase(word_name, "ct-control-")
        return f"Control-frame utility that handles {target} operations for custom compile-time control flow."

    if word_name.startswith("string-"):
        target = _target_phrase(word_name, "string-")
        return f"String helper for {target} during compile-time token/metadata processing."

    if word_name.startswith("list-"):
        target = _target_phrase(word_name, "list-")
        return f"List operation for {target}; mutates or queries CT lists used by macros and rewrites."

    if word_name.startswith("map-"):
        target = _target_phrase(word_name, "map-")
        return f"Map operation for {target}; works on string-keyed CT maps used for structured metadata."

    if (
        word_name.startswith("token-")
        or word_name.startswith("ct-parser-")
        or word_name.startswith("ct-tokenizer-")
        or word_name.startswith("source-cursor-")
    ):
        target = _name_phrase(word_name)
        return f"Token-stream helper for {target}; reads, inspects, or injects parser tokens during compile time."

    if word_name.startswith("ct-output-"):
        target = _name_phrase(word_name)
        return f"Assembly output helper for {target}; changes the text-preamble, data, or runtime BSS buffer of the current compilation."

    if word_name in {"nil", "nil?"}:
        return "Nil sentinel helper used for optional values and missing-key semantics in CT list/map workflows."

    if stack_effect:
        return (
            f"{category} operation used during compile-time execution with stack contract {stack_effect}; "
            "designed for explicit and deterministic parser/VM state changes."
        )
    return None


def _usage_guidance_for_word(word_name: str, category: str) -> str:
    if word_name == "ct-op-emit":
        return "Supply the internal operation name followed by its data; this appends directly to the active compiled body instead of parsing tokens."
    if word_name.startswith("ct-definition-"):
        return "By-name reads use the selected candidate and by-name changes need exactly one unowned candidate; handle forms address one exact candidate, and a stale handle gives nil 0 or 0."
    if word_name == "ct-label-new":
        return "Pass a readable identifier prefix and use the returned unique label in emitted branch/label operations."
    if word_name in {"ct-parser-register-block-opener", "ct-parser-unregister-block-opener", "ct-control-register-override", "ct-control-unregister-override"}:
        return "Use the exact control word lexeme that the parser should recognize; registrations affect later parsing in the current compilation."
    if word_name == "i":
        return "Call it only inside an active compile-time for loop; the index is zero-based and belongs to the innermost loop."
    if category in {"List", "Map", "String", "Nil"}:
        if word_name.startswith(("list-set", "list-insert", "list-remove", "list-pop", "list-clear", "list-reverse", "map-set", "map-delete", "map-merge", "map-clear")):
            return "These operations mutate the supplied collection; retain the returned collection when chaining further work."
        if word_name.startswith(("list-get", "list-find", "list-peek", "list-last", "map-get", "map-has", "map-keys", "map-values")):
            return "Lookups consume the collection; checked reads error on a missing element, while map-get and list-find push a value with a found flag."
        return "Build inputs with the matching list/map/string constructors, then pass arguments in the order shown by the stack effect."
    if category == "Token":
        if word_name.startswith("ct-parser-"):
            return "Use it while the parser is processing source; consumption or injection affects the live token cursor."
        if word_name.startswith("ct-tokenizer-"):
            return "Edit the map from ct-tokenizer-get-config and install it whole; the configuration affects later tokenization only, never tokens already read."
        if word_name.startswith("source-cursor-"):
            return "Create one cursor per source text and keep the handle; reads advance it in place, and slices or spans never move it."
        return "Pass the token, SourceOrigin, or ExpansionFrame value the matching operation returned; these values are immutable and may be shared."
    if category == "Control":
        return "Use control frames within the active parser flow; push/pop operations mutate the parser's control stack, while frame key operations act on the supplied map."
    if category == "Rewrite":
        if word_name.startswith(("ct-rewrite-list-", "ct-rewrite-get-", "ct-rewrite-rule-get-", "ct-rewrite-dry-run", "ct-rewrite-run-on-list", "ct-rewrite-loop-detection?")):
            return "Use the returned values for inspection; listing, reading, and list simulations never register or change rules."
        return "Keep the RewriteHandle a registration returns: per-rule changes and removal take the handle, since rule names need not be unique."
    if category == "Pattern":
        return "Use the pattern macro's registered name consistently; group/scope activation and enabled state jointly determine whether its clauses can match."
    if category == "Language":
        return "Most operations take the pack name first; resources registered through the pack APIs are owned by that pack and are cleaned up when it is removed."
    if category == "RNG":
        return "Seed once with ct-rng-seed when a run must be reproducible; every draw advances the one stream shared by the whole compilation."
    if category == "Macro":
        return "Use the registered macro name; query functions return presence/status separately so missing macros can be handled without guessing."
    if category == "Assembly":
        return "Call it during compilation before final assembly emission; these lines are added to compiler output and are not ordinary runtime instructions."
    if category == "Assert":
        return "Use it to stop compilation at the point a compile-time invariant fails rather than allowing invalid generated code to proceed."
    if category == "Template":
        return "Use it inside a macro template; it controls template expansion and does not emit a runtime instruction by itself."
    if category in {"Event", "Events"} or word_name.startswith(("event-", "ct-event-")):
        return "Event helpers operate on the current compile-time event bus; dispatch is synchronous and subscribers receive the event record."
    if word_name.startswith(("ct-error-", "ct-recovery-")):
        return "Call recovery helpers only from a recoverable compiler.error handler; repair the problem, then request a retry with ct-recovery-resume."
    if word_name.startswith(("ct-parser-", "ct-predef-")):
        return "Use it during parsing against the live token stream; checkpoints restore parser position, not arbitrary compiler or external state."
    if word_name.startswith(("ct-get-", "ct-list-")):
        return "Use the returned value to make a compile-time decision; where the stack effect includes a found flag, test it before using the associated value."
    if word_name.startswith(("ct-set-", "ct-add-", "ct-register-")):
        return "Call it before the source construct that should observe the change; registration and configuration are order-dependent within a compilation."
    if word_name.startswith("ct-clear-"):
        return "Use it at a deliberate phase boundary when subsequent compilation should start without the accumulated state."
    return "Push its inputs in stack order, call it during the phase where its effect is needed, and consume the result before continuing."


def category_for_word(word_name: str) -> str:
    if word_name in _CT_TEMPLATE_DIRECTIVE_NAMES:
        return "Template"
    if word_name in ("nil", "nil?"):
        return "Nil"
    if word_name.startswith("list-"):
        return "List"
    if word_name.startswith("map-"):
        return "Map"
    if word_name.startswith("string-") or word_name in ("int>string", "try-string>int", "identifier?"):
        return "String"
    if word_name.startswith(("token-", "ct-parser-", "ct-tokenizer-", "source-cursor-")):
        return "Token"
    if word_name.startswith("ct-control-") or word_name in (
        "ct-label-new",
        "ct-op-emit",
        "ct-parser-last-line",
        "ct-parser-register-block-opener",
        "ct-parser-unregister-block-opener",
    ):
        return "Control"
    if word_name.startswith("ct-output-"):
        return "Assembly"
    if word_name.startswith("ct-capture-"):
        return "Capture"
    if (
        word_name.startswith("ct-rng-")
    ):
        return "RNG"
    if word_name.startswith("ct-lang-"):
        return "Language"
    if (
        word_name.startswith("ct-set-pattern-")
        or word_name.startswith("ct-get-pattern-")
        or word_name.startswith("ct-list-pattern-")
        or word_name.startswith("ct-detect-pattern-")
    ):
        return "Pattern"
    if (
        word_name.startswith("ct-rewrite-")
    ):
        return "Rewrite"
    if (
        word_name.startswith("ct-register-")
        or word_name.startswith("ct-unregister-")
        or word_name.startswith("ct-macro-")
        or word_name in ("ct-lang-register-macro",)
    ):
        return "Macro"
    if word_name in ("ct-parser-set-token-hook", "ct-parser-clear-token-hook", "compile-time", "compile-only", "immediate", "runtime", "runtime-only", "CT"):
        return "Hook"
    if word_name in ("static-assert", "ct-parser-error", "ct-parser-error-at"):
        return "Assert"
    if word_name == "eval":
        return "Eval"
    if word_name == "i":
        return "Loop"
    return "Meta"


# The macro-template directives were retired with unified macros.
_CT_TEMPLATE_DIRECTIVE_METADATA: Tuple[Dict[str, Any], ...] = ()

_CT_TEMPLATE_DIRECTIVE_NAMES = frozenset(
    str(item.get("name", "")).strip() for item in _CT_TEMPLATE_DIRECTIVE_METADATA
)


def _scope_for_word(meta: Dict[str, Any]) -> str:
    if bool(meta.get("runtime_only")):
        return "runtime-only"
    if bool(meta.get("immediate")):
        return "immediate"
    if bool(meta.get("compile_only")):
        return "compile-only"
    if bool(meta.get("has_runtime_intrinsic")):
        return "runtime+compile-time"
    return "all"


_OVERVIEW_OVERRIDES: Dict[str, str] = {
    "ct-recovery-list-restore-points": "Returns the current error restore points as maps with id, stage, pos, lexeme, line, and column. It works outside error handlers; the records are metadata, not snapshots of compiler state.",
    "ct-recovery-get-restore-point": "Returns one restore point by id; an unknown id raises recovery-restore-point-invalid.",
    "ct-recovery-jump": "Selects the restore point a resumed retry starts from. Only valid while a recoverable error is being dispatched, and only for a point of the same stage; an invalid id leaves the earlier selection unchanged.",
    "ct-recovery-resume": "Requests a retry of the error being dispatched and pushes 1. Outside a recoverable dispatch it raises recovery-operation-unavailable.",
    "ct-recovery-get-retry-limit": "Pushes the retry limit that new recovery chains will use (default 8).",
    "ct-recovery-set-retry-limit": "Sets the retry limit for recovery chains that start later and pushes the previous value. Any positive integer is accepted; a running chain keeps its limit.",
    "ct-event-subscribe": "Subscribes a CT handler word, given by name, to an event-name pattern (owner nil, or a DefinitionHandle that scopes it) and returns an opaque subscription handle. Each dispatch snapshots matching subscriptions and resolves handler words when it starts; the handler gets its own copy of the event and must consume it.",
    "ct-event-unsubscribe": "Removes a subscription by its handle and returns 1, or 0 when it was already removed. A dispatch that is already running still finishes with its snapshot.",
    "ct-event-emit": "Publishes an event with a map payload (nil means an empty map) and returns 1. The payload is copied at publication; handlers run before ct-event-emit returns.",
    "ct-event-emit-object": "Publishes an event map built with event-new and event-payload-set (or one received by a handler) with a fresh timestamp and returns 1. Only the keys event, timestamp_ns, and payload are allowed.",
    "event-new": "Creates a draft event map with the given name and an empty payload. Drafts have no timestamp until they are published.",
    "event-payload-set": "Stores a value in the event's payload and returns the event for chaining. It never changes the event name or timestamp.",
    "event-payload-get": "Reads one payload entry, pushing the value and 1, or nil and 0 when the key is absent.",
    "event-name": "Consumes an event map and pushes its name.",
    "event-timestamp": "Consumes an event map and pushes its publication timestamp and 1, or nil and 0 for an unpublished draft.",
    "event-payload": "Consumes an event map and pushes its payload map.",
    "ct-event-enabled?": "Pushes 1 when event publication is enabled, otherwise 0 (always 0 under --no-events).",
    "ct-event-set-enabled": "Enables or disables event publication for later events and pushes the previous state. Under --no-events, disabling returns 0 and enabling is an error.",
    "CT": "Pushes 1 when running inside compile-time execution and 0 in emitted runtime code, so words can branch explicitly on execution mode.",
    "ct-parser-eof?": "Returns 1 when parser cursor is at or beyond end-of-stream and 0 otherwise, enabling safe loop termination in custom parser macros.",
    "ct-parser-peek": "Performs non-consuming lookahead by offset from current parser cursor; out-of-range reads return nil instead of raising.",
    "ct-parser-set-pos": "Moves the parser cursor to an absolute token index and returns the previous position; the current token becomes the token before the new position.",
    "ct-parser-checkpoint": "Returns an immutable opaque checkpoint holding the parser cursor and current token, for cheap lookahead and backtracking.",
    "ct-parser-restore": "Restores a checkpoint and pushes 1, or pushes 0 without changing state when the checkpoint is from another parser or no longer fits the stream.",
    "ct-parser-tail": "Returns a copy of remaining token objects from current parser cursor for diagnostics, lookahead, or external analyzers.",
    "ct-parser-session-begin": "Starts a speculative parsing session that saves the token list, cursor, and current token; returns an opaque session handle.",
    "ct-parser-session-commit": "Closes the open topmost session and keeps the current parser state; pushes 0 without changes for any other session.",
    "ct-parser-session-rollback": "Restores the token list, cursor, and current token saved by the open topmost session; pushes 0 without changes for any other session.",
    "ct-parser-slice": "Returns the tokens between two stream positions in source order without moving the cursor.",
    "token-with-lexeme": "Builds a new token with different text that keeps the template token's source origin and expansion chain, so generated code still points at real source.",
    "ct-definition-set-effect": "Declares a definition's stack effect in cells (inputs and outputs, nil for unknown); the stack checker then verifies calls to it and its body's final depth.",
    "ct-definition-effect": "Pushes a definition's declared inputs and outputs (nil when unknown) and a found flag.",
    "token-slice": "Takes part of a token's text as a new token; for a token read from source the new token's origin is exactly that part, so code generated from pieces of a token (for example `a+b` split into three) points at the right columns.",
    "lexeme": "Pushes a token's lexeme, or a string unchanged, so a helper can accept either.",
    "token?": "Pushes 1 when the value is a token and 0 otherwise.",
    "ct-rewrite-scope-push": "Pushes active rewrite pipeline/group/scope activation snapshot so temporary DSL rewrites can be scoped safely.",
    "ct-rewrite-scope-pop": "Restores rewrite activation snapshot from scope stack, preventing temporary rewrite settings from leaking globally.",
    "ct-rewrite-run-on-list": "Runs rewrite engine against explicit lexeme list using current stage settings and returns rewritten output plus patch trace.",
    "ct-lang-create": "Creates or updates a named language-extension pack record and returns whether it was newly created.",
    "ct-lang-exists?": "Returns 1 when a named language-extension pack exists.",
    "ct-lang-list": "Returns sorted list of registered language-extension pack names.",
    "ct-lang-active?": "Returns 1 when a named language-extension pack is active.",
    "ct-lang-add-reader-rewrite-named": "Registers a named reader-stage rewrite under a language-extension pack.",
    "ct-lang-add-grammar-rewrite-named": "Registers a named grammar-stage rewrite under a language-extension pack.",
    "ct-lang-status": "Returns status map for a language-extension pack (active state, resources, counts, metadata).",
    "ct-lang-remove": "Removes a language-extension pack and cleans owned rewrites/macros/hooks.",
    "ct-parser-collect-until": "Consumes tokens through a non-empty delimiter lexeme and returns the tokens before it with a found flag (0 at end of input).",
    "ct-parser-collect-balanced": "After the outer opener was consumed, returns the interior of a balanced region (nested pairs included) and consumes the matching close; equal delimiters do not nest.",
    "ct-parser-expected": "Non-consuming check of the next token against a lexeme, token, or list of them; pushes the token and 1 on a match, or nil and 0.",
    "ct-parser-consume": "Consumes and returns the next token; raises a parse error at end of input.",
    "ct-parser-current": "Returns the most recently consumed token, or nil.",
    "ct-parser-get-pos": "Returns the parser cursor as a token index in [0, token-count].",
    "ct-parser-inject": "Inserts a list of tokens at the cursor in list order without moving it; every element must be a token.",
}

_OVERVIEW_OVERRIDES.update({
    "ct-lang-add-grammar-rewrite-named": "Immediately registers a named grammar-stage rewrite and records it as owned by a language pack. Pass the pack name, rule name, input-pattern list, and replacement list; pack activation does not gate the rule, while removing the pack cleans it up.",
    "ct-lang-add-reader-rewrite-named": "Immediately registers a named reader-stage rewrite and records it as owned by a language pack. Pass the pack name, rule name, input-pattern list, and replacement list; pack activation does not gate the rule, while removing the pack cleans it up.",
    "ct-lang-create": "Creates an inactive language-extension pack record when the name is new; an existing record is left in place. Returns 1 only for a newly created pack. Create it before setting metadata or activating it.",
    "ct-lang-exists?": "Tests whether a language-extension pack record exists and returns 1 or 0. This does not tell you whether the pack is active; use ct-lang-active? for that.",
    "ct-lang-list": "Returns all registered language-extension pack names in sorted order, including inactive packs. Use ct-lang-list-active when you only need active packs.",
    "ct-lang-status": "Returns a status map and found flag for a pack. Inspect it to report activation, owned resources, and metadata without separately querying each resource list.",
})


def _compose_overview(
    word_name: str,
    raw_description: str,
    *,
    stack_effect: str,
    category: str,
) -> str:
    override = _OVERVIEW_OVERRIDES.get(word_name)
    if override is not None:
        if len(override) < 105:
            return f"{override} {_usage_guidance_for_word(word_name, category)}"
        return override
    semantic = _semantic_overview_from_name(word_name, category, stack_effect)
    text = raw_description.strip()
    if text:
        rendered = text if text.endswith(".") else text + "."
        if len(rendered) < 105:
            rendered += f" {_usage_guidance_for_word(word_name, category)}"
        return rendered
    if semantic:
        return semantic
    if stack_effect:
        return (
            f"{category} helper used during compile-time execution. "
            f"It follows stack effect {stack_effect} and is intended for predictable, "
            "explicit stack transformations."
        )
    return (
        f"{category} helper for {_name_phrase(word_name)} during compile-time execution. "
        "Use it when you need explicit, deterministic control of parser and VM state."
    )


_EXAMPLE_OVERRIDES: Dict[str, str] = {
    "ct-recovery-list-restore-points": "ct-recovery-list-restore-points list-length puti cr",
    "ct-recovery-get-restore-point": "ct-recovery-list-restore-points 0 list-get \"id\" map-get drop ct-recovery-get-restore-point \"stage\" map-get drop puts",
    "ct-recovery-jump": "event-payload \"restore-point-id\" map-get drop ct-recovery-jump ct-recovery-resume drop",
    "ct-recovery-resume": "event-payload \"recoverable\" map-get drop if ct-recovery-resume drop end",
    "ct-recovery-get-retry-limit": "ct-recovery-get-retry-limit puti cr",
    "ct-recovery-set-retry-limit": "3 ct-recovery-set-retry-limit drop",
    "ct-event-subscribe": "word on_build event-name puts end \"build.begin\" \"on_build\" nil ct-event-subscribe drop",
    "ct-event-unsubscribe": "word on_build drop end \"build.begin\" \"on_build\" nil ct-event-subscribe ct-event-unsubscribe static-assert",
    "ct-event-emit": "\"build.note\" map-new ct-event-emit static-assert",
    "ct-event-emit-object": "\"build.note\" event-new \"source\" \"generator\" event-payload-set ct-event-emit-object static-assert",
    "event-new": "\"build.note\" event-new event-name puts",
    "event-payload-set": "\"build.note\" event-new \"source\" \"generator\" event-payload-set event-payload map-length puti cr",
    "event-payload-get": "\"build.note\" event-new \"source\" \"generator\" event-payload-set \"source\" event-payload-get drop puts",
    "event-name": "\"build.note\" event-new event-name puts",
    "event-timestamp": "\"build.note\" event-new event-timestamp nip puti cr",
    "event-payload": "\"build.note\" event-new event-payload map-length puti cr",
    "ct-event-enabled?": "ct-event-enabled? puti cr",
    "ct-event-set-enabled": "0 ct-event-set-enabled ct-event-set-enabled drop",
    "CT": "CT puti cr   # prints 1 at compile time and 0 at runtime",
    "compile-time": "word build_only 1 puti cr end  compile-time build_only",
    "immediate": "word stamp 99 puti cr end immediate",
    "compile-only": "word helper 42 end compile-only",
    "runtime": "word runtime_word 7 end runtime",
    "runtime-only": "word runtime_word 7 end runtime-only",
    "inline": "inline word inc 1 + end",
    "preserve": "inline preserve word retained 1 + end",
    "ct-tokenizer-get-config": 'ct-tokenizer-get-config "separators" map-get drop "," string-join puts',
    "ct-tokenizer-set-config": 'ct-tokenizer-get-config dup "separators" map-get drop "=>" list-append "separators" swap map-set ct-tokenizer-set-config',
    "ct-tokenizer-run": '"a (b) c" "<example>" ct-tokenizer-run list-length puti cr',
    "source-cursor-new": '"abc" "<example>" source-cursor-new source-cursor-read puti cr drop',
    "diagnostic-new": "\"error\" \"my-code\" \"my_error\" nil \"bad input\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-code puts",
    "diagnostic-severity": "\"note\" \"n\" nil nil \"fyi\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-severity puts",
    "diagnostic-code": "\"error\" \"my-code\" \"my_error\" nil \"bad\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-code puts",
    "diagnostic-category": "\"error\" \"c\" \"my_error\" nil \"bad\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-category puts",
    "diagnostic-phase": "\"error\" \"c\" \"my_error\" \"my-phase\" \"bad\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-phase puts",
    "diagnostic-message": "\"error\" \"c\" \"my_error\" nil \"bad\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-message puts",
    "diagnostic-primary": "\"error\" \"c\" \"my_error\" nil \"bad\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-primary source-origin-start-line puti cr",
    "diagnostic-related": "\"error\" \"c\" \"my_error\" nil \"bad\" 0 ct-parser-peek token-origin list-new diagnostic-new diagnostic-related list-length puti cr",
    "ct-diagnostic-raise": "\"error\" \"my-code\" \"my_error\" nil \"bad input\" 0 ct-parser-peek token-origin list-new diagnostic-new ct-diagnostic-raise",
    "ct-diagnostic-list": "\"heads up\" ct-diagnostic-warning ct-diagnostic-list list-length puti cr",
    "ct-diagnostic-error": "\"unsupported target\" ct-diagnostic-error",
    "ct-diagnostic-error-at": "\"unsupported target\" 1 1 ct-diagnostic-error-at",
    "ct-parser-error-at": "\"bad value\" 1 1 ct-parser-error-at",
    "ct-diagnostic-warning": "\"deprecated option\" ct-diagnostic-warning",
    "ct-diagnostic-note": "\"using defaults\" ct-diagnostic-note",
    "source-cursor-peek": '"abc" "<example>" source-cursor-new source-cursor-peek puti cr',
    "source-cursor-read": '"abc" "<example>" source-cursor-new source-cursor-read swap source-cursor-read nip + puti cr',
    "source-cursor-eof?": '"" "<example>" source-cursor-new source-cursor-eof? puti cr',
    "source-cursor-pos": '"abc" "<example>" source-cursor-new source-cursor-read drop source-cursor-pos puti cr',
    "source-cursor-set-pos": '"abc" "<example>" source-cursor-new 2 source-cursor-set-pos source-cursor-peek puti cr',
    "source-cursor-slice": '"hello" "<example>" source-cursor-new 1 4 source-cursor-slice puts',
    "source-cursor-span": '"a\\nbc" "<example>" source-cursor-new 2 4 source-cursor-span source-origin-start-line puti cr',


    "ct-control-frame-new": '"if" ct-control-frame-new ct-control-push',
    "string-starts-with?": '"alpha:beta:gamma" "alpha:" string-starts-with? static-assert',

    "ct-rewrite-dry-run": '"grammar" list-new 64 ct-rewrite-dry-run',
    "list-new": "list-new 1 list-append 2 list-append",
    "map-new": 'map-new "key" 42 map-set',
    "ct-parser-consume": "ct-parser-consume token-lexeme puts",
    "token-with-lexeme": '0 ct-parser-peek "renamed" token-with-lexeme token-lexeme',
    "ct-definition-set-effect": '"main" 0 0 ct-definition-set-effect drop',
    "ct-definition-effect": '"main" ct-definition-effect drop drop drop',
    "token-slice": '0 ct-parser-peek 0 1 token-slice token-lexeme',
    "lexeme": '"text" lexeme puts',
    "token?": '0 ct-parser-peek token? puti cr',
    "source-origin-path": "0 ct-parser-peek token-origin source-origin-path puts",
    "source-origin-start": "0 ct-parser-peek token-origin source-origin-start puti cr",
    "source-origin-end": "0 ct-parser-peek token-origin source-origin-end puti cr",
    "source-origin-start-line": "0 ct-parser-peek token-origin source-origin-start-line puti cr",
    "source-origin-start-column": "0 ct-parser-peek token-origin source-origin-start-column puti cr",
    "source-origin-end-line": "0 ct-parser-peek token-origin source-origin-end-line puti cr",
    "source-origin-end-column": "0 ct-parser-peek token-origin source-origin-end-column puti cr",
    "expansion-frame-kind": "0 ct-parser-peek token-expansion-chain dup list-empty? if drop else list-last expansion-frame-kind puts end",
    "expansion-frame-name": "0 ct-parser-peek token-expansion-chain dup list-empty? if drop else list-last expansion-frame-name puts end",
    "expansion-frame-call-site": "0 ct-parser-peek token-expansion-chain dup list-empty? if drop else list-last expansion-frame-call-site source-origin-start-line puti cr end",
    "expansion-frame-definition-origin": "0 ct-parser-peek token-expansion-chain dup list-empty? if drop else list-last expansion-frame-definition-origin nil? puti cr end",
    "ct-parser-eof?": "ct-parser-eof?",
    "ct-parser-peek": "0 ct-parser-peek token-lexeme",
    "ct-parser-set-pos": "ct-parser-get-pos ct-parser-set-pos drop",
    "ct-parser-checkpoint": "ct-parser-checkpoint ct-parser-restore static-assert",
    "ct-parser-restore": "ct-parser-checkpoint ct-parser-restore static-assert",
    "ct-parser-tail": "ct-parser-tail list-length",
    "ct-parser-session-begin": "ct-parser-session-begin ct-parser-session-commit static-assert",
    "ct-parser-session-commit": "ct-parser-session-begin ct-parser-session-commit static-assert",
    "ct-parser-session-rollback": "ct-parser-session-begin ct-parser-session-rollback static-assert",
    "ct-parser-collect-until": '"end" ct-parser-collect-until drop list-length puti cr',
    "ct-parser-collect-balanced": '"(" ")" ct-parser-collect-balanced drop list-length puti cr',
    "ct-parser-slice": "ct-parser-get-pos dup ct-parser-slice list-length puti cr",
    "ct-parser-expected": '"word" ct-parser-expected puti cr drop',
    "ct-rewrite-scope-push": "ct-rewrite-scope-push 0 > static-assert",
    "ct-rewrite-scope-pop": "ct-rewrite-scope-push drop ct-rewrite-scope-pop static-assert",
    "ct-rewrite-run-on-list": '"grammar" list-new "kw" list-append ct-rewrite-run-on-list swap drop',
    "ct-lang-create": '"demo.lang" ct-lang-create drop',
    "ct-lang-exists?": '"demo.lang" ct-lang-exists? static-assert',
    "ct-lang-list": "ct-lang-list list-length 0 >= static-assert",
    "ct-lang-activate": '"demo.lang" ct-lang-activate static-assert',
    "ct-lang-deactivate": '"demo.lang" ct-lang-deactivate static-assert',
    "ct-lang-active?": '"demo.lang" ct-lang-active? static-assert',
    "ct-lang-add-reader-rewrite-named": '"demo.lang" "r" list-new list-new ct-lang-add-reader-rewrite-named drop',
    "ct-lang-add-grammar-rewrite-named": '"demo.lang" "g" list-new list-new ct-lang-add-grammar-rewrite-named drop',
    "ct-lang-status": '"demo.lang" ct-lang-status swap drop',
    "ct-lang-remove": '"demo.lang" ct-lang-remove drop',
}

_EXAMPLE_OVERRIDES.update({
    "ct-control-depth": "ct-control-depth puti cr",
    "ct-control-peek": '"if" ct-control-frame-new ct-control-push ct-control-peek ct-control-pop drop',
    "ct-control-pop": '"if" ct-control-frame-new ct-control-push ct-control-pop drop',
    "ct-control-push": '"if" ct-control-frame-new ct-control-push ct-control-pop drop',
    "ct-control-set": '"if" ct-control-frame-new "false" "L_false" ct-control-set drop',
    "ct-parser-current": "ct-parser-current token-lexeme puts",
    "ct-parser-checkpoint": "ct-parser-checkpoint ct-parser-restore static-assert",
    "ct-parser-get-pos": "ct-parser-get-pos puti cr",
    "ct-parser-remaining": "ct-parser-remaining puti cr",
    "ct-parser-tail": "ct-parser-tail list-length puti cr",
    "identifier?": '"valid_name2" identifier? static-assert',
    "ct-parser-inject": 'list-new 0 ct-parser-peek "temporary_word" token-with-lexeme list-append ct-parser-inject',
    "int>string": "42 int>string puts",
    "list-append": 'list-new "first" list-append "second" list-append list-length puti cr',
    "list-clear": 'list-new "temporary" list-append list-clear list-empty? static-assert',
    "list-clone": 'list-new "kept" list-append list-clone list-length puti cr',
    "list-deep-clone": 'list-new list-new "inner" list-append list-append list-deep-clone 0 list-get list-length puti cr',
    "list-contains?": 'list-new "red" list-append "red" list-contains? static-assert',
    "list-empty?": "list-new list-empty? static-assert",
    "list-extend": 'list-new "left" list-append list-new "right" list-append list-extend list-length puti cr',
    "list-find": 'list-new "red" list-append "blue" list-append "blue" list-find dup static-assert drop puti cr',
    "list-get": 'list-new "first" list-append "second" list-append 1 list-get puts',
    "list-insert": 'list-new "a" list-append "c" list-append 1 "b" list-insert list-length puti cr',
    "list-last": 'list-new "first" list-append "last" list-append list-last puts',
    "list-peek-front": 'list-new "first" list-append "last" list-append list-peek-front puts',
    "list-pop": 'list-new "first" list-append "last" list-append list-pop drop swap drop puts',
    "list-pop-front": 'list-new "first" list-append "last" list-append list-pop-front drop swap drop puts',
    "list-push-front": 'list-new "last" list-append "first" list-push-front list-length puti cr',
    "list-remove": 'list-new "keep" list-append "remove" list-append 1 list-remove swap drop puts',
    "list-reverse": 'list-new "first" list-append "last" list-append list-reverse list-get 0 puts',
    "list-set": 'list-new "old" list-append 0 "new" list-set 0 list-get puts',
    "list-slice": 'list-new "a" list-append "b" list-append "c" list-append 1 3 list-slice list-length puti cr',
    "map-clear": 'map-new "temporary" 1 map-set map-clear map-empty? static-assert',
    "map-clone": 'map-new "name" "L2" map-set map-clone map-length puti cr',
    "map-delete": 'map-new "temporary" 1 map-set "temporary" map-delete swap drop static-assert',
    "map-empty?": "map-new map-empty? static-assert",
    "map-get": 'map-new "name" "L2" map-set "name" map-get drop puts',
    "map-has?": 'map-new "name" "L2" map-set "name" map-has? static-assert',
    "map-keys": 'map-new "name" "L2" map-set map-keys list-length puti cr',
    "map-length": 'map-new "name" "L2" map-set map-length puti cr',
    "map-set": 'map-new "name" "L2" map-set "name" map-get drop swap drop puts',
    "map-merge": 'map-new "left" 1 map-set map-new "right" 2 map-set map-merge map-length puti cr',
    "map-values": 'map-new "name" "L2" map-set map-values list-length puti cr',
    "nil": "nil nil? static-assert",
    "nil?": "nil nil? static-assert",
    "string-append": '"compile" "-" string-append puts',
    "string-contains?": '"compile-time" "time" string-contains? static-assert',
    "string-ends-with?": '"compile-time" "time" string-ends-with? static-assert',
    "string-join": 'list-new "reader" list-append "parser" list-append " -> " string-join puts',
    "string-length": '"compile-time" string-length puti cr',
    "string-lower": '"CT-VM" string-lower puts',
    "string-replace": '"red,red" "," ";" string-replace puts',
    "string-split": '"reader,parser,emitter" "," string-split list-length puti cr',
    "string-starts-with?": '"compile-time" "compile" string-starts-with? static-assert',
    "string-upper": '"compile-time" string-upper puts',
    "string-equal?": '"same" "same" string-equal? static-assert',
    "try-string>int": '"0x2a" try-string>int drop 42 == static-assert',
    "token-from-origin": '0 ct-parser-peek token-origin "generated_name" swap token-from-origin token-lexeme puts',
    "token-origin": "0 ct-parser-peek token-origin source-origin-start-line puti cr",
    "token-expansion-chain": "0 ct-parser-peek token-expansion-chain list-length puti cr",
    "token-lexeme": "0 ct-parser-peek token-lexeme puts",
})

_EXAMPLE_OVERRIDES.update({
    "ct-control-depth": "ct-control-depth puti cr",
    "ct-parser-get-pos": "ct-parser-get-pos puti cr",
    "ct-parser-remaining": "ct-parser-remaining puti cr",
    "list-length": 'list-new "first" list-append "second" list-append list-length puti cr',
    "ct-parser-consume": "ct-parser-consume token-lexeme puts",
    "string-strip": '"  compile-time  " string-strip puts',
    "token-column": "0 ct-parser-peek token-column puti cr",
    "token-line": "0 ct-parser-peek token-line puti cr",
})

_EXAMPLE_OVERRIDES.update({
    "ct-control-peek": '"if" ct-control-frame-new ct-control-push ct-control-peek drop ct-control-pop drop',
    "ct-control-pop": '"if" ct-control-frame-new ct-control-push ct-control-pop drop',
    "list-reverse": 'list-new "first" list-append "last" list-append list-reverse 0 list-get puts',
})

_EXAMPLE_OVERRIDES.update({
    "ct-lang-create": '"demo.lang" ct-lang-create static-assert',
    "ct-lang-list": '"demo.lang" ct-lang-create drop ct-lang-list list-length puti cr',
    "ct-lang-activate": '"demo.lang" ct-lang-create drop "demo.lang" ct-lang-activate static-assert',
    "ct-lang-active?": '"demo.lang" ct-lang-create drop "demo.lang" ct-lang-activate drop "demo.lang" ct-lang-active? static-assert',
    "ct-lang-add-grammar-rewrite-named": '"demo.lang" ct-lang-create drop "demo.lang" "g" list-new "kw" list-append list-new "42" list-append ct-lang-add-grammar-rewrite-named drop',
    "ct-lang-add-reader-rewrite-named": '"demo.lang" ct-lang-create drop "demo.lang" "r" list-new "kw" list-append list-new "42" list-append ct-lang-add-reader-rewrite-named drop',
    "ct-lang-deactivate": '"demo.lang" ct-lang-create drop "demo.lang" ct-lang-activate drop "demo.lang" ct-lang-deactivate static-assert',
    "ct-lang-exists?": '"demo.lang" ct-lang-create drop "demo.lang" ct-lang-exists? static-assert',
    "ct-lang-remove": '"demo.lang" ct-lang-create drop "demo.lang" ct-lang-remove static-assert',
    "ct-lang-status": '"demo.lang" ct-lang-create drop "demo.lang" ct-lang-status drop map-length puti cr',
})

_GRAMMAR_REWRITE_EXAMPLE_SETUP = (
    '"rule_kw" list-new "kw" list-append list-new "42" list-append '
    'ct-rewrite-add-grammar-named'
)
_TEXT_MACRO_EXAMPLE_SETUP = "macro twice (x) $x $x ;"

_EXAMPLE_OVERRIDES.update({
})

_EXAMPLE_OVERRIDES.update({
})

_EXAMPLE_OVERRIDES.update({
    "ct-rewrite-dry-run": '"grammar" list-new "kw" list-append 64 ct-rewrite-dry-run swap drop list-length puti cr',
    "ct-rewrite-run-on-list": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} "grammar" list-new "kw" list-append ct-rewrite-run-on-list swap drop list-length puti cr',
})


_OVERVIEW_OVERRIDES.update({
    "ct-definition-candidates": "Returns immutable DefinitionCandidate maps for every candidate registered under a name, highest priority first and newest first within a priority, including inactive pack-owned ones. A missing or built-in-only name gives an empty list.",
    "ct-definition-exists?": "Pushes 1 when the name has a registered candidate (including inactive pack-owned ones) or a built-in handler, else 0.",
    "ct-definition-list-names": "Returns the sorted names that have a candidate or a built-in handler, each once.",
    "ct-definition-list-names-prefix": "Returns the sorted names that have a candidate or a built-in handler and start with the prefix.",
    "ct-definition-get": "Pushes the current DefinitionCandidate map for a handle and 1, or nil and 0 when the handle is stale.",
    "ct-definition-get-by-name": "Pushes the selected eligible candidate's map and 1, or nil and 0 when no candidate is eligible.",
    "ct-definition-body": "Pushes a detached copy of the candidate's body (op maps for a word, expansion tokens for a text macro) and 1, or nil and 0.",
    "ct-definition-body-by-name": "Like ct-definition-body for the selected candidate of a name.",
    "ct-definition-asm": "Pushes an assembly candidate's current body and 1, or nil and 0 for other kinds or a stale handle.",
    "ct-definition-asm-by-name": "Like ct-definition-asm for the selected candidate of a name.",
    "ct-definition-source": "Pushes the exact original source text of the candidate's declaration and 1, or nil and 0 when it has none (generated or built-in definitions).",
    "ct-definition-source-by-name": "Like ct-definition-source for the selected candidate of a name.",
    "ct-definition-set-priority": "Sets one candidate's priority (any integer, not a boolean) and pushes 1, or 0 for a stale handle. Selection is recomputed; registration order is kept.",
    "ct-definition-set-priority-by-name": "Sets the priority of a name's only candidate; pushes 0 when the name has none or several.",
    "ct-definition-set-flags": "Applies a flag patch map (immediate, compile-only, runtime-only, inline, preserve; integers or booleans) to one candidate. Invalid keys or combinations raise and change nothing.",
    "ct-definition-set-flags-by-name": "Applies a flag patch to a name's only candidate; pushes 0 when the name has none or several.",
    "ct-definition-set-asm": "Replaces an assembly candidate's body without running it; symbols are resolved at emission. The declaration source is kept.",
    "ct-definition-set-asm-by-name": "Replaces the body of a name's only candidate, which must be an assembly definition.",
    "ct-definition-rename": "Moves one candidate to another name, where it becomes the newest registration and keeps its handle. The old name falls back to its next candidate.",
    "ct-definition-rename-by-name": "Renames a name's only candidate; pushes 0 when the name has none or several.",
    "ct-definition-clone": "Registers a copy of one candidate under a name with the same priority, a new handle, and a new registration position. Built-in handlers are not copied.",
    "ct-definition-clone-by-name": "Clones a name's only candidate; pushes nil 0 when the name has none or several.",
    "ct-definition-unregister": "Removes one exact candidate and pushes 1, or 0 for a stale handle. Its handle stays stale; the name falls back to its next candidate.",
    "ct-definition-unregister-by-name": "Removes a name's only unowned candidate; pushes 0 when the name has none or several.",
    "ct-definition-prefer-l2": "Marks a candidate to run its L2 definition at compile time instead of the name's compile-time intrinsic while it is selected. Runtime-only candidates cannot be marked.",
    "ct-definition-prefer-l2-by-name": "Marks the selected candidate of a name to run its L2 definition at compile time instead of the compile-time intrinsic; pushes 0 when nothing is selected.",
    "ct-definition-register-source": "Parses a source fragment of word and :asm definitions and registers each in order, returning their handles. A fragment that fails to parse registers nothing. Needs an active compilation.",
    "ct-definition-predeclare-all": "Registers every later top-level word definition in the parse stream now, so compile-time code can call it early; the normal parse later completes the same candidate.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-definition-candidates": '"main" ct-definition-candidates list-length puti cr',
    "ct-definition-exists?": '"puti" ct-definition-exists? static-assert',
    "ct-definition-list-names": "ct-definition-list-names list-length puti cr",
    "ct-definition-list-names-prefix": '"ct-" ct-definition-list-names-prefix list-length puti cr',
    "ct-definition-get": '"puti" ct-definition-candidates 0 list-get "handle" map-get drop ct-definition-get drop "priority" map-get drop puti cr',
    "ct-definition-get-by-name": '"puti" ct-definition-get-by-name drop "kind" map-get drop puts',
    "ct-definition-body": '"puti" ct-definition-candidates 0 list-get "handle" map-get drop ct-definition-body drop list-length puti cr',
    "ct-definition-body-by-name": 'word example_word 1 2 + end "example_word" ct-definition-body-by-name drop list-length puti cr',
    "ct-definition-asm": ':asm example_asm { nop }; "example_asm" ct-definition-candidates 0 list-get "handle" map-get drop ct-definition-asm drop puts',
    "ct-definition-asm-by-name": ':asm example_asm { nop }; "example_asm" ct-definition-asm-by-name drop puts',
    "ct-definition-source": 'word example_word 42 end "example_word" ct-definition-candidates 0 list-get "handle" map-get drop ct-definition-source drop puts',
    "ct-definition-source-by-name": 'word example_word 42 end "example_word" ct-definition-source-by-name drop puts',
    "ct-definition-set-priority": '"example_word" ct-definition-candidates 0 list-get "handle" map-get drop 10 ct-definition-set-priority static-assert',
    "ct-definition-set-priority-by-name": '"example_word" 10 ct-definition-set-priority-by-name drop',
    "ct-definition-set-flags": '"example_word" ct-definition-candidates 0 list-get "handle" map-get drop map-new "compile-only" 1 map-set ct-definition-set-flags drop',
    "ct-definition-set-flags-by-name": '"example_word" map-new "inline" 1 map-set ct-definition-set-flags-by-name drop',
    "ct-definition-set-asm": '"example_asm" ct-definition-candidates 0 list-get "handle" map-get drop "nop" ct-definition-set-asm drop',
    "ct-definition-set-asm-by-name": '"example_asm" "nop" ct-definition-set-asm-by-name drop',
    "ct-definition-rename": '"old_name" ct-definition-candidates 0 list-get "handle" map-get drop "new_name" ct-definition-rename drop',
    "ct-definition-rename-by-name": '"old_name" "new_name" ct-definition-rename-by-name drop',
    "ct-definition-clone": '"example_word" ct-definition-candidates 0 list-get "handle" map-get drop "example_copy" ct-definition-clone drop drop',
    "ct-definition-clone-by-name": '"example_word" "example_copy" ct-definition-clone-by-name drop drop',
    "ct-definition-unregister": '"temporary_word" ct-definition-candidates 0 list-get "handle" map-get drop ct-definition-unregister drop',
    "ct-definition-unregister-by-name": '"temporary_word" ct-definition-unregister-by-name drop',
    "ct-definition-prefer-l2": '"dup" ct-definition-candidates 0 list-get "handle" map-get drop ct-definition-prefer-l2 drop',
    "ct-definition-prefer-l2-by-name": '"dup" ct-definition-prefer-l2-by-name drop',
    "ct-definition-register-source": '"word recovered 42 end" ct-definition-register-source drop',
    "ct-definition-predeclare-all": "ct-definition-predeclare-all",
})


_OVERVIEW_OVERRIDES.update({
    "syntax-length": "Pushes the number of Tokens in a Syntax value.",
    "syntax-get": "Pushes the Token at an index of a Syntax value; negative indices count from the end and an out-of-range index raises.",
    "syntax-slice": "Pushes a new Syntax value with the Tokens from start up to end, using list slice bounds.",
    "syntax-concat": "Pushes a new Syntax value with the left value's Tokens followed by the right value's.",
    "syntax>list": "Pushes a fresh list of the Syntax value's Tokens, which keep their provenance.",
    "list>syntax": "Builds a Syntax value from a list that must contain only Tokens.",
    "emit": "Inserts a Syntax value's Tokens at the current position of the input being compiled; the parser reads them next.",
    "gensym": "Returns a fresh identifier from a prefix: sanitized to [A-Za-z0-9_], then suffixed with a counter that skips names already defined.",
    "ct-expansion-set-depth-limit": "Sets the nesting limit shared by macro and rewrite expansion; only positive integers are accepted.",
    "ct-expansion-get-depth-limit": "Pushes the nesting limit shared by macro and rewrite expansion (default 256).",
})

_EXAMPLE_OVERRIDES.update({
    "syntax-length": "`1 2 +` syntax-length puti cr",
    "syntax-get": "`1 2 +` -1 syntax-get token-lexeme puts",
    "syntax-slice": "`1 2 +` 0 2 syntax-slice syntax-length puti cr",
    "syntax-concat": "`1` `2 +` syntax-concat syntax-length puti cr",
    "syntax>list": "`1 2 +` syntax>list list-length puti cr",
    "list>syntax": "`1 2 +` syntax>list list>syntax syntax-length puti cr",
    "emit": "`word answer 42 end` emit",
    "gensym": '"tmp" gensym puts',
    "ct-expansion-set-depth-limit": "128 ct-expansion-set-depth-limit",
    "ct-expansion-get-depth-limit": "ct-expansion-get-depth-limit puti cr",
})


_OVERVIEW_OVERRIDES.update({
    "ct-macro-get": "Pushes a detached record (parameters, body, metadata) for an exact macro candidate and 1, or nil and 0 when the handle is stale or not a macro.",
    "ct-macro-get-by-name": "Like ct-macro-get for the selected candidate of a name; nil and 0 when it is not a macro.",
    "ct-macro-set": "Updates a macro's parameters, body, or metadata in place from a patch map. Everything is validated, and the body parsed, before anything changes; the body is not run.",
    "ct-macro-set-by-name": "Applies a macro patch to a name's only unowned candidate; pushes 0 when the name has none or several.",
    "ct-macro-context": "While a macro body runs, pushes a fresh map with the macro name, its definition origin, the call site, and the expansion trace.",
    "ct-definition-set-body": "Replaces an L2 word's body with Syntax parsed as a word body (not run); flags and source provenance are kept.",
    "ct-definition-set-body-by-name": "Replaces the body of a name's only unowned L2 candidate; pushes 0 when the name has none or several.",
    "ct-lang-register-macro": "Adopts the selected unowned macro into a language pack, which then owns it; pushes 0 when there is no such macro.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-macro-get": '"add" ct-definition-candidates 0 list-get "handle" map-get drop ct-macro-get drop "parameters" map-get drop list-length puti cr',
    "ct-macro-get-by-name": '"add" ct-macro-get-by-name drop "body" map-get drop syntax-length puti cr',
    "ct-macro-set": '"add" ct-definition-candidates 0 list-get "handle" map-get drop map-new "metadata" map-new "doc" "adds" map-set map-set ct-macro-set drop',
    "ct-macro-set-by-name": '"add" map-new "body" `\\` \\$x \\$y * \\` emit` map-set ct-macro-set-by-name drop',
    "ct-macro-context": 'ct-macro-context "macro" map-get drop puts',
    "ct-definition-set-body": '"value" ct-definition-candidates 0 list-get "handle" map-get drop `40 2 +` ct-definition-set-body drop',
    "ct-definition-set-body-by-name": '"value" `40 2 +` ct-definition-set-body-by-name drop',
    "ct-lang-register-macro": '"demo.lang" ct-lang-create drop "demo.lang" "add" ct-lang-register-macro drop',
})


_OVERVIEW_OVERRIDES.update({
    "ct-rewrite-add-reader": "Registers an unnamed reader-stage rule (pattern and replacement lexeme lists) and pushes its RewriteHandle; the rule gets a generated name.",
    "ct-rewrite-add-reader-named": "Registers a named reader-stage rule and pushes its RewriteHandle; names need not be unique.",
    "ct-rewrite-add-grammar": "Registers an unnamed grammar-stage rule (pattern and replacement lexeme lists) and pushes its RewriteHandle.",
    "ct-rewrite-add-grammar-named": "Registers a named grammar-stage rule and pushes its RewriteHandle; names need not be unique.",
    "ct-rewrite-remove": "Removes the rule a RewriteHandle identifies, in either stage; pushes 0 for a stale handle.",
    "ct-rewrite-clear-reader": "Removes every reader-stage rule that no language pack owns and pushes how many were removed.",
    "ct-rewrite-clear-grammar": "Removes every grammar-stage rule that no language pack owns and pushes how many were removed.",
    "ct-rewrite-list-reader": "Pushes one {handle, name} map per reader-stage registration, in registration order, including disabled and inactive-pack rules.",
    "ct-rewrite-list-grammar": "Pushes one {handle, name} map per grammar-stage registration, in registration order, including disabled and inactive-pack rules.",
    "ct-rewrite-set-loop-detection": "Turns exact-state loop detection on or off (integer or boolean); a detected loop is rejected before the rewrite is committed.",
    "ct-rewrite-loop-detection?": "Pushes 1 when rewrite loop detection is on (the default), else 0.",
    "ct-rewrite-get-loop-reports": "Pushes a detached list of the detected-loop reports (stage, rule, steps, and the repeated lexeme sequence).",
    "ct-rewrite-clear-loop-reports": "Clears the loop reports and pushes how many were removed.",
    "ct-rewrite-set-max-steps": "Sets the number of rewrite applications allowed per parse across both stages; 0 allows none, negative values are rejected.",
    "ct-rewrite-get-max-steps": "Pushes the rewrite application budget (default 100000).",
    "ct-rewrite-set-pipeline-active": "Activates or deactivates a pipeline for a stage; rules on an inactive pipeline do not match.",
    "ct-rewrite-list-active-pipelines": "Pushes the sorted names of the stage's active pipelines.",
    "ct-rewrite-scope-push": "Saves the active pipeline sets of both stages and pushes the new scope depth.",
    "ct-rewrite-scope-pop": "Restores the most recently saved pipeline sets and pushes 1, or 0 when nothing is saved.",
    "ct-rewrite-run-on-list": "Applies the stage's eligible rules to a lexeme list (the input is unchanged) using the configured step budget; pushes the result and the patch list.",
    "ct-rewrite-dry-run": "Like ct-rewrite-run-on-list with an explicit nonnegative step budget.",
    "ct-rewrite-rule-set-priority": "Sets a rule's priority (any integer); higher priorities are tried first and the registration order is kept.",
    "ct-rewrite-rule-get-priority": "Pushes a rule's priority and 1, or nil and 0; takes a RewriteHandle or a stage and a unique rule name.",
    "ct-rewrite-rule-set-enabled": "Enables or disables a rule (integer or boolean) without removing it.",
    "ct-rewrite-rule-get-enabled": "Pushes 1 or 0 for a rule's enabled flag and 1, or nil and 0; takes a RewriteHandle or a stage and a unique rule name.",
    "ct-rewrite-rule-set-pipeline": "Moves a rule to a pipeline (a nonempty name); the rule matches only while that pipeline is active.",
    "ct-rewrite-rule-get-pipeline": "Pushes a rule's pipeline name and 1, or nil and 0; takes a RewriteHandle or a stage and a unique rule name.",
    "ct-rewrite-rule-set-guard": "Sets a rule's guard word (`payload -- applies?`, resolved when the rule matches) or nil for none.",
    "ct-rewrite-rule-get-guard": "Pushes a rule's guard name (or nil) and 1, or nil and 0; takes a RewriteHandle or a stage and a unique rule name.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-rewrite-add-reader": 'list-new "kw" list-append list-new "42" list-append ct-rewrite-add-reader drop',
    "ct-rewrite-add-reader-named": '"rule_kw" list-new "kw" list-append list-new "42" list-append ct-rewrite-add-reader-named drop',
    "ct-rewrite-add-grammar": 'list-new "kw" list-append list-new "42" list-append ct-rewrite-add-grammar drop',
    "ct-rewrite-add-grammar-named": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} drop',
    "ct-rewrite-remove": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} ct-rewrite-remove static-assert',
    "ct-rewrite-clear-reader": 'ct-rewrite-clear-reader puti cr',
    "ct-rewrite-clear-grammar": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} drop ct-rewrite-clear-grammar puti cr',
    "ct-rewrite-list-reader": 'ct-rewrite-list-reader list-length puti cr',
    "ct-rewrite-list-grammar": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} drop ct-rewrite-list-grammar list-length puti cr',
    "ct-rewrite-set-loop-detection": '0 ct-rewrite-set-loop-detection',
    "ct-rewrite-loop-detection?": 'ct-rewrite-loop-detection? puti cr',
    "ct-rewrite-get-loop-reports": 'ct-rewrite-get-loop-reports list-length puti cr',
    "ct-rewrite-clear-loop-reports": 'ct-rewrite-clear-loop-reports puti cr',
    "ct-rewrite-set-max-steps": '4096 ct-rewrite-set-max-steps',
    "ct-rewrite-get-max-steps": 'ct-rewrite-get-max-steps puti cr',
    "ct-rewrite-set-pipeline-active": '"grammar" "optimized" 1 ct-rewrite-set-pipeline-active',
    "ct-rewrite-list-active-pipelines": '"grammar" ct-rewrite-list-active-pipelines list-length puti cr',
    "ct-rewrite-scope-push": 'ct-rewrite-scope-push puti cr',
    "ct-rewrite-scope-pop": 'ct-rewrite-scope-push drop ct-rewrite-scope-pop static-assert',
    "ct-rewrite-run-on-list": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} drop "grammar" list-new "kw" list-append ct-rewrite-run-on-list drop 0 list-get puts',
    "ct-rewrite-dry-run": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} drop "grammar" list-new "kw" list-append 8 ct-rewrite-dry-run list-length puti cr drop',
    "ct-rewrite-rule-set-priority": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} 10 ct-rewrite-rule-set-priority static-assert',
    "ct-rewrite-rule-get-priority": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} ct-rewrite-rule-get-priority drop puti cr',
    "ct-rewrite-rule-set-enabled": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} 0 ct-rewrite-rule-set-enabled static-assert',
    "ct-rewrite-rule-get-enabled": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} drop "grammar" "rule_kw" ct-rewrite-rule-get-enabled drop puti cr',
    "ct-rewrite-rule-set-pipeline": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} "optimized" ct-rewrite-rule-set-pipeline static-assert',
    "ct-rewrite-rule-get-pipeline": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} ct-rewrite-rule-get-pipeline drop puts',
    "ct-rewrite-rule-set-guard": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} "keep_rule" ct-rewrite-rule-set-guard static-assert',
    "ct-rewrite-rule-get-guard": f'{_GRAMMAR_REWRITE_EXAMPLE_SETUP} ct-rewrite-rule-get-guard drop nil? puti cr',
})


_OVERVIEW_OVERRIDES.update({
    "ct-parser-set-token-hook": "Installs the global token hook: binds the name's selected word, which then sees each Token as `token -- handled?`; a nonzero result consumes the Token.",
    "ct-parser-clear-token-hook": "Removes the global token hook; hooks owned by language packs are not affected.",
    "ct-lang-create": "Creates an inactive language pack and pushes 1, or pushes 0 when the name already exists (its state is left unchanged).",
    "ct-lang-exists?": "Pushes 1 when the language pack exists, else 0.",
    "ct-lang-list": "Pushes the sorted names of all language packs, active or not.",
    "ct-lang-list-active": "Pushes the sorted names of the active language packs.",
    "ct-lang-activate": "Activates a pack, enabling its hooks, rewrites, words, and macros; pushes 0 when it was already active.",
    "ct-lang-deactivate": "Deactivates a pack without unregistering its resources; pushes 0 when it was already inactive.",
    "ct-lang-active?": "Pushes 1 when the language pack exists and is active, else 0.",
    "ct-lang-set-meta": "Stores a detached copy of a string-keyed map of portable CT data as the pack's metadata; nil clears it.",
    "ct-lang-get-meta": "Pushes a detached copy of the pack's metadata and 1, or nil and 0 for an unknown pack.",
    "ct-lang-status": "Pushes a detached status map (name, active, metadata, token_hooks, words, macros, reader_rewrites, grammar_rewrites) and 1, or nil and 0.",
    "ct-lang-remove": "Removes a pack and every resource it owns and pushes 1, or 0 when the pack does not exist.",
    "ct-lang-add-reader-rewrite-named": "Registers a reader-stage rewrite owned by the pack and pushes its RewriteHandle; it matches while the pack is active.",
    "ct-lang-add-grammar-rewrite-named": "Registers a grammar-stage rewrite owned by the pack and pushes its RewriteHandle; it matches while the pack is active.",
    "ct-lang-add-token-hook": "Adds an enabled priority-0 token hook to the pack and pushes its TokenHookHandle; the handler name is resolved at each dispatch.",
    "ct-lang-set-token-hook-priority": "Sets a pack hook's priority (higher runs first) and pushes 1, or 0 for a stale handle.",
    "ct-lang-set-token-hook-enabled": "Enables or disables a pack hook and pushes 1, or 0 for a stale handle.",
    "ct-lang-remove-token-hook": "Removes a pack hook and pushes 1, or 0 for a stale handle.",
    "ct-lang-register-word": "Adopts the selected unowned word into the pack (keeping its handle and priority) and pushes 1, or 0 when there is none.",
    "ct-lang-register-macro": "Adopts the selected unowned macro into the pack (keeping its handle and priority) and pushes 1, or 0 when there is none.",
    "ct-lang-set-resource-priority": "Sets the priority of the pack's word or macro of that name and pushes 1, or 0 when the pack owns none.",
    "ct-lang-get-resource-priority": "Pushes the priority of the pack's word or macro of that name and 1, or nil and 0.",
    "ct-lang-unregister-resource": "Removes the pack's word or macro of that name and pushes 1, or 0 when the pack owns none; the next eligible candidate takes over.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-parser-set-token-hook": "word my_hook drop 0 end \"my_hook\" ct-parser-set-token-hook",
    "ct-parser-clear-token-hook": "ct-parser-clear-token-hook",
    "ct-lang-create": "\"demo.lang\" ct-lang-create static-assert",
    "ct-lang-exists?": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-exists? static-assert",
    "ct-lang-list": "\"demo.lang\" ct-lang-create drop ct-lang-list list-length puti cr",
    "ct-lang-list-active": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop ct-lang-list-active list-length puti cr",
    "ct-lang-activate": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate static-assert",
    "ct-lang-deactivate": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" ct-lang-deactivate static-assert",
    "ct-lang-active?": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" ct-lang-active? static-assert",
    "ct-lang-set-meta": "\"demo.lang\" ct-lang-create drop \"demo.lang\" map-new \"version\" 1 map-set ct-lang-set-meta static-assert",
    "ct-lang-get-meta": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-get-meta drop map-length puti cr",
    "ct-lang-status": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-status drop \"active\" map-get drop puti cr",
    "ct-lang-remove": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-remove static-assert",
    "ct-lang-add-reader-rewrite-named": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"r\" list-new \"kw\" list-append list-new \"42\" list-append ct-lang-add-reader-rewrite-named drop",
    "ct-lang-add-grammar-rewrite-named": "\"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"g\" list-new \"kw\" list-append list-new \"42\" list-append ct-lang-add-grammar-rewrite-named drop",
    "ct-lang-add-token-hook": "word demo_hook drop 0 end \"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"demo_hook\" ct-lang-add-token-hook drop",
    "ct-lang-set-token-hook-priority": "\"demo.lang\" ct-lang-create drop \"demo.lang\" \"demo_hook\" ct-lang-add-token-hook 10 ct-lang-set-token-hook-priority static-assert",
    "ct-lang-set-token-hook-enabled": "\"demo.lang\" ct-lang-create drop \"demo.lang\" \"demo_hook\" ct-lang-add-token-hook 0 ct-lang-set-token-hook-enabled static-assert",
    "ct-lang-remove-token-hook": "\"demo.lang\" ct-lang-create drop \"demo.lang\" \"demo_hook\" ct-lang-add-token-hook ct-lang-remove-token-hook static-assert",
    "ct-lang-register-word": "word helper 1 end \"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"helper\" ct-lang-register-word static-assert",
    "ct-lang-register-macro": "macro add (x y) `$x $y +` emit ; \"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"add\" ct-lang-register-macro static-assert",
    "ct-lang-set-resource-priority": "word helper 1 end \"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"helper\" ct-lang-register-word drop \"demo.lang\" \"helper\" 5 ct-lang-set-resource-priority static-assert",
    "ct-lang-get-resource-priority": "word helper 1 end \"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"helper\" ct-lang-register-word drop \"demo.lang\" \"helper\" ct-lang-get-resource-priority drop puti cr",
    "ct-lang-unregister-resource": "word helper 1 end \"demo.lang\" ct-lang-create drop \"demo.lang\" ct-lang-activate drop \"demo.lang\" \"helper\" ct-lang-register-word drop \"demo.lang\" \"helper\" ct-lang-unregister-resource static-assert",
})


_OVERVIEW_OVERRIDES.update({
    "ct-output-append-text-preamble": "Appends one raw fragment to the .text preamble, which is emitted after the runtime prelude and before word definitions.",
    "ct-output-set-text-preamble": "Replaces the .text preamble with a copy of a list of string fragments; the whole list is checked before anything changes.",
    "ct-output-clear-text-preamble": "Removes every .text preamble fragment.",
    "ct-output-append-data": "Appends one raw .data fragment; CT fragments come after definition data and before variable storage.",
    "ct-output-set-data": "Replaces the CT .data fragments with a copy of a list of strings; compiler-generated data is not affected.",
    "ct-output-clear-data": "Removes every CT .data fragment; compiler-generated data is not affected.",
    "ct-output-append-bss": "Appends one raw fragment to the runtime BSS buffer, which starts as the standard runtime layout.",
    "ct-output-set-bss": "Replaces the whole runtime BSS buffer, standard layout included, with a copy of a list of strings; the replacement must define the runtime symbols.",
    "ct-output-clear-bss": "Empties the runtime BSS buffer, standard layout included; later appends build on the empty buffer. Compiler-generated BSS is still emitted.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-output-append-text-preamble": "\"; generated by CT code\" ct-output-append-text-preamble",
    "ct-output-set-text-preamble": "list-new \"; first\" list-append \"; second\" list-append ct-output-set-text-preamble",
    "ct-output-clear-text-preamble": "\"; temporary\" ct-output-append-text-preamble ct-output-clear-text-preamble",
    "ct-output-append-data": "\"scratch_dq: dq 42\" ct-output-append-data",
    "ct-output-set-data": "list-new \"scratch_dq: dq 42\" list-append ct-output-set-data",
    "ct-output-clear-data": "\"scratch_dq: dq 42\" ct-output-append-data ct-output-clear-data",
    "ct-output-append-bss": "\"scratch: resb 64\" ct-output-append-bss",
    "ct-output-set-bss": "list-new \"dstack: resb DSTK_BYTES\" list-append \"dstack_top:\" list-append ct-output-set-bss",
    "ct-output-clear-bss": "ct-output-clear-bss \"rstack: resb RSTK_BYTES\" ct-output-append-bss",
})


_OVERVIEW_OVERRIDES.update({
    "ct-rng-seed": "Resets the compilation's random stream from an integer seed (reduced modulo 2**64); later draws repeat for the same seed and Python version.",
    "ct-rng-int": "Pushes a pseudo-random integer in [0, bound); bound must be a positive integer.",
    "ct-rng-range": "Pushes a pseudo-random integer in the inclusive range [lo, hi]; lo must not exceed hi.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-rng-seed": "12345 ct-rng-seed 10 ct-rng-int puti cr",
    "ct-rng-int": "6 ct-rng-int 1 + puti cr",
    "ct-rng-range": "1 6 ct-rng-range puti cr",
})


_OVERVIEW_OVERRIDES.update({
    "ct-control-get": "Pushes the frame's value for a key and 1, or nil and 0 when the key is absent; a key holding nil gives nil and 1.",
    "ct-control-get?": "Pushes the frame's value for a key, or nil when the key is absent (no found flag; use ct-control-get to tell a missing key from a nil value).",
    "ct-control-add-close-op": "Appends an {op, data} close descriptor to the frame; `end` validates every descriptor before popping the frame, so a bad one changes nothing.",
    "ct-op-emit": "Appends an internal operation to the active body, keeping its data by reference; unsupported operations fail later during emission.",
    "ct-label-new": "Allocates a fresh L_<prefix>_<n> label; the prefix must be an ASCII identifier.",
    "ct-parser-last-line": "Pushes the line of the last token the parser read, or 0 before any token.",
    "ct-parser-register-block-opener": "Adds a lexeme to the block openers counted by `with` nesting and predeclaration scans.",
    "ct-parser-unregister-block-opener": "Removes a lexeme from the block openers; built-in constructs keep working.",
    "ct-control-register-override": "Routes if, else, for, while, or do to the immediate word of that name; a missing or non-immediate handler is then an error.",
    "ct-control-unregister-override": "Removes a control override so the built-in parser handles that control word again.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-control-get": "\"if\" ct-control-frame-new \"false\" \"L_false\" ct-control-set \"false\" ct-control-get drop puts",
    "ct-control-get?": "\"if\" ct-control-frame-new \"false\" \"L_false\" ct-control-set \"false\" ct-control-get? puts",
    "ct-control-add-close-op": "\"if\" ct-control-frame-new \"label\" \"if_false\" ct-label-new ct-control-add-close-op ct-control-push",
    "ct-op-emit": "\"literal\" 42 ct-op-emit",
    "ct-label-new": "\"loop_done\" ct-label-new puts",
    "ct-parser-last-line": "ct-parser-last-line puti cr",
    "ct-parser-register-block-opener": "\"unless\" ct-parser-register-block-opener",
    "ct-parser-unregister-block-opener": "\"unless\" ct-parser-unregister-block-opener",
    "ct-control-register-override": "\"if\" ct-control-register-override",
    "ct-control-unregister-override": "\"if\" ct-control-unregister-override",
})


_OVERVIEW_OVERRIDES.update({
    "repr": "Pushes a readable debug representation of any CT value; the text is not a stable format.",
    "static-assert": "Raises static-assertion-failed at the call site when the condition (a boolean or integer) is false or zero; other types are a type error.",
    "string-utf8-length": "Pushes the number of UTF-8 bytes in a string.",
})

_EXAMPLE_OVERRIDES.update({
    "repr": 'list-new "token" list-append repr puts',
    "static-assert": "1 1 + 2 == static-assert",
    "string-utf8-length": '"h\u00e9" string-utf8-length puti cr',
})


_OVERVIEW_OVERRIDES.update({
    "ct-event-stream-enabled?": "Pushes 1 while a configured --events-stream JSONL file is being written, else 0.",
    "ct-event-stream-set-enabled": "Pauses (0) or resumes writes to the configured --events-stream file and pushes the previous state; enabling without a stream is an error.",
})

_EXAMPLE_OVERRIDES.update({
    "ct-event-stream-enabled?": "ct-event-stream-enabled? puti cr",
    "ct-event-stream-set-enabled": "0 ct-event-stream-set-enabled drop",
})


def _normalize_example_line(text: str) -> str:
    return " ".join(part.strip() for part in str(text).splitlines() if part.strip())


def _curated_example_for_word(word_name: str) -> str:
    override = _EXAMPLE_OVERRIDES.get(word_name)
    if override is not None:
        return override

    if word_name in {"eval", "ct-parser-error", "static-assert", "here", "i"}:
        if word_name == "eval":
            return '"1 2 +" eval'
        if word_name == "ct-parser-error":
            return '"build failed" ct-parser-error'
        if word_name == "static-assert":
            return "1 static-assert"
        if word_name == "here":
            return "here"
        return "10 for i puti cr end"

    raise KeyError(f"no curated CT reference example for {word_name!r}")


def _examples_for_word(word_name: str) -> List[str]:
    example = _normalize_example_line(_curated_example_for_word(word_name))
    if not example:
        raise ValueError(f"curated CT reference example for {word_name!r} is empty")
    return [example]


def _format_examples_block(examples: Sequence[str]) -> str:
    lines: List[str] = []
    for idx, example in enumerate(examples, start=1):
        line = _normalize_example_line(example)
        if line:
            lines.append(f"{idx}) {line}")
    return "\n".join(lines)


def _split_example_lines(entry: Dict[str, Any]) -> List[str]:
    maybe_examples = entry.get("examples")
    if isinstance(maybe_examples, Sequence) and not isinstance(maybe_examples, (str, bytes)):
        out = [_normalize_example_line(item) for item in maybe_examples]
        return [item for item in out if item]
    text = str(entry.get("example", "")).strip()
    out = [_normalize_example_line(line) for line in text.splitlines()]
    return [item for item in out if item]


def _format_example_display_line(line: str) -> str:
    normalized = _normalize_example_line(line)
    if not normalized:
        return ""
    return f"- {normalized}"


def _merge_ct_reference_metadata(ct_words: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    def _normalize_meta(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        name = str(item.get("name", "")).strip()
        if not _is_plausible_word_name(name):
            return None
        return {
            "name": name,
            "compile_only": bool(item.get("compile_only")),
            "immediate": bool(item.get("immediate")),
            "runtime_only": bool(item.get("runtime_only")),
            "has_runtime_intrinsic": bool(item.get("has_runtime_intrinsic")),
        }

    for item in ct_words:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_meta(item)
        if normalized is None:
            continue
        merged[normalized["name"]] = normalized

    for item in _CT_TEMPLATE_DIRECTIVE_METADATA:
        normalized = _normalize_meta(item)
        if normalized is None:
            continue
        merged.setdefault(normalized["name"], normalized)

    return sorted(merged.values(), key=lambda item: str(item.get("name", "")).lower())


def build_ct_reference_entries(base_doc_text: str, ct_words: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_meta = _merge_ct_reference_metadata(ct_words)
    allowed_names = {
        str(meta.get("name", "")).strip()
        for meta in all_meta
        if _is_plausible_word_name(str(meta.get("name", "")).strip())
    }
    details = extract_ct_ref_entry_details(base_doc_text, allowed_names=allowed_names)
    # Build entries for all known CT words + template directives.
    out: List[Dict[str, Any]] = []
    for meta in all_meta:
        name = str(meta.get("name", "")).strip()
        if not _is_plausible_word_name(name):
            continue
        entry = details.get(name, {})
        stack_effect = str(entry.get("stack", "")).strip()
        raw_description = str(entry.get("description", "")).strip()
        category = category_for_word(name)
        overview = _compose_overview(
            name,
            raw_description,
            stack_effect=stack_effect,
            category=category,
        )
        example_lines = _examples_for_word(name)
        example = _format_examples_block(example_lines)
        scope = _scope_for_word(meta)
        search_text = " ".join(
            [
                name,
                category,
                scope,
                stack_effect,
                overview,
                " ".join(example_lines),
                example,
            ]
        ).lower()
        out.append(
            {
                "name": name,
                "category": category,
                "scope": scope,
                "stack_effect": stack_effect,
                "overview": overview,
                "example": example,
                "examples": list(example_lines),
                "search_text": search_text,
            }
        )

    return out


def build_ct_summary_table(entries: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "  § 17  SUMMARY TABLE",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "  Word                               Category        Stack Effect",
        "  ────────────────────────────────   ──────────────  ──────────────────────────",
    ]
    for entry in entries:
        name = str(entry.get("name", ""))
        category = str(entry.get("category", "Meta"))
        stack_effect = str(entry.get("stack_effect", "")).strip() or "(see SECTION 18)"
        lines.append(f"  {name:<32}   {category:<14}  {stack_effect}")
    lines.append("")
    return "\n".join(lines)


def build_ct_function_index(entries: Sequence[Dict[str, Any]]) -> str:
    lines: List[str] = [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "  § 18  COMPLETE CT FUNCTION INDEX",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for entry in entries:
        name = str(entry.get("name", ""))
        stack_effect = str(entry.get("stack_effect", "")).strip()
        category = str(entry.get("category", "Meta"))
        scope = str(entry.get("scope", "all"))
        overview = str(entry.get("overview", "")).strip()
        example_lines = _split_example_lines(entry)

        if stack_effect:
            lines.append(f"  {name:<34}  {stack_effect}")
        else:
            lines.append(f"  {name}")
        lines.append(f"    Category: {category}")
        lines.append(f"    Scope: {scope}")
        lines.append(f"    Overview: {overview}")
        lines.append("    Example:")
        for ex_line in (example_lines or [""]):
            lines.append(f"      - {ex_line}")
        lines.append("")

    return "\n".join(lines)


def build_ct_reference_bundle(base_doc_text: str, ct_words: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    entries = build_ct_reference_entries(base_doc_text, ct_words)
    summary_text = build_ct_summary_table(entries)
    appendix_text = build_ct_function_index(entries)
    return {
        "entries": entries,
        "summary_text": summary_text,
        "appendix_text": appendix_text,
    }


def attach_ct_entry_line_numbers(full_text: str, entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    line_map: Dict[str, int] = {}
    lines = full_text.splitlines()
    current_section = "intro"
    in_index = False

    for idx, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match is not None:
            current_section = match.group(1).strip()
            in_index = "COMPLETE CT FUNCTION INDEX" in current_section.upper()

        if not in_index:
            continue

        em = _ENTRY_RE.match(line)
        if em is None:
            continue
        name = em.group(1).strip()
        if name not in line_map:
            line_map[name] = idx + 1

    out: List[Dict[str, Any]] = []
    for entry in entries:
        item = dict(entry)
        item["line_no"] = int(line_map.get(str(item.get("name", "")), 0))
        item["section"] = "COMPLETE CT FUNCTION INDEX"
        out.append(item)
    return out


def build_ct_detail_lines(entry: Dict[str, Any], width: int) -> List[str]:
    name = str(entry.get("name", ""))
    category = str(entry.get("category", "Meta"))
    scope = str(entry.get("scope", "all"))
    stack_effect = str(entry.get("stack_effect", "")).strip()
    overview = str(entry.get("overview", "")).strip()
    example_lines = _split_example_lines(entry)
    line_no = int(entry.get("line_no", 0))

    lines: List[str] = []
    lines.append(f"Function: {name}")
    lines.append(f"Category: {category} | Scope: {scope} | Line: {line_no}")
    lines.append("")

    if stack_effect:
        lines.append("Stack Effect:")
        for part in textwrap.wrap(stack_effect, max(20, width - 6)):
            lines.append(f"  {part}")
        lines.append("")

    lines.append("Overview:")
    for part in textwrap.wrap(overview or "No overview available.", max(20, width - 6)):
        lines.append(f"  {part}")
    lines.append("")

    lines.append("Example:")
    ex_lines = example_lines if example_lines else [name]
    for ex_line in ex_lines:
        wrapped = textwrap.wrap(f"- {ex_line}", max(20, width - 6)) or [""]
        for part in wrapped:
            lines.append(f"  {part}")

    return lines


_CT_WORD_METADATA_PROVIDER: Optional[Callable[[], List[Dict[str, Any]]]] = None


# The compiler supplies the macros and constants each file defines (from
# a parse: parameters, `doc` metadata, declaration source), so the explorer
# lists them with what the compiler knows, not what a regex finds.
_DEFINITION_METADATA_PROVIDER: Optional[Callable[[Sequence[Path]], List[Dict[str, Any]]]] = None


def configure_definition_metadata(provider: Optional[Callable[[Sequence[Path]], List[Dict[str, Any]]]]) -> None:
    global _DEFINITION_METADATA_PROVIDER
    _DEFINITION_METADATA_PROVIDER = provider


def _definition_metadata_entries(files: Sequence[Path], *, include_undocumented: bool,
                                 include_private: bool) -> List[DocEntry]:
    """DocEntries for the macros and constants defined in `files`: the
    description is the `doc` metadata, else the leading comment block."""
    provider = _DEFINITION_METADATA_PROVIDER
    if provider is None or not files:
        return []
    try:
        rows = provider(files)
    except Exception:
        return []
    lines_by_path: Dict[str, List[str]] = {}
    entries: List[DocEntry] = []
    for row in rows:
        name = str(row.get("name", ""))
        if not _is_doc_symbol_name(name, include_private=include_private):
            continue
        path = str(row.get("path", ""))
        line = int(row.get("line", 0) or 0)
        description = str(row.get("doc") or "")
        if not description and line > 0:
            lines = lines_by_path.get(path)
            if lines is None:
                try:
                    lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    lines = []
                lines_by_path[path] = lines
            _effect, description = _collect_leading_doc_comments(lines, line - 1, name)
        if not include_undocumented and not description:
            continue
        entries.append(DocEntry(name=name, stack_effect=str(row.get("stack_effect", "")),
                                description=description, kind=str(row.get("kind", "macro")),
                                path=Path(path), line=line))
    return entries


def configure_runtime(*, ct_word_metadata_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None) -> None:
    global _CT_WORD_METADATA_PROVIDER
    _CT_WORD_METADATA_PROVIDER = ct_word_metadata_provider


def _collect_ct_word_metadata() -> List[Dict[str, Any]]:
    if _CT_WORD_METADATA_PROVIDER is None:
        data = []
    else:
        try:
            data = _CT_WORD_METADATA_PROVIDER()
        except Exception:
            data = []

    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        name = str(normalized.get("name", "")).strip()
        if not name or name in seen:
            continue
        out.append(normalized)
        seen.add(name)

    for item in _CT_TEMPLATE_DIRECTIVE_METADATA:
        name = str(item.get("name", "")).strip()
        if not name or name in seen:
            continue
        out.append(dict(item))
        seen.add(name)

    out.sort(key=lambda item: str(item.get("name", "")).lower())
    return out


class _DocsSelfProxy:
    def __getattr__(self, name: str) -> Any:
        return globals()[name]


_DOCS_SELF_PROXY = _DocsSelfProxy()


def _load_docs_helpers(*, warn: bool = False) -> Any:
    del warn
    return sys.modules.get(__name__) or _DOCS_SELF_PROXY


def _build_ct_ref_complete_summary_table(base_doc_text: str) -> str:
    return build_ct_summary_table(
        build_ct_reference_entries(base_doc_text, _collect_ct_word_metadata())
    )


def _build_ct_ref_function_appendix(base_doc_text: str) -> str:
    return build_ct_function_index(
        build_ct_reference_entries(base_doc_text, _collect_ct_word_metadata())
    )
