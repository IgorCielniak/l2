"""Structured compile-time docs helpers for the L2 docs explorer.

This module is intentionally lightweight and is imported lazily by l2_main.py
only when docs or integrity features need it.
"""

from __future__ import annotations

import ast
import html
import json
import re

_CT_REF_SECTION_RE = re.compile(r"^\s*§\s*\d+\s+(.+?)\s*$")
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
_DOC_WORD_RE = re.compile(r"^\s*(?:inline\s+)?word\s+([^\s]+)\b")
_DOC_ASM_RE = re.compile(r"^\s*:asm\s+([^\s{]+)")
_DOC_PY_RE = re.compile(r"^\s*:py\s+([^\s{]+)")
_DOC_MACRO_RE = re.compile(r"^\s*macro\s+([^\s]+)(?:\s+(\d+))?")


def _extract_stack_comment(text: str) -> Optional[Tuple[str, str]]:
    match = _DOC_STACK_RE.match(text)
    if match is None:
        return None
    name = match.group(1).strip()
    tail = match.group(2).strip()
    if not name:
        return None
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
            if comment_name == name and not stack_effect:
                stack_effect = effect
            idx -= 1
            continue

        text = stripped[1:].strip()
        if text:
            comments.append(text)
        idx -= 1

    comments.reverse()
    return stack_effect, " ".join(comments)


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
        for path in resolved.rglob("*.sl"):
            if _should_skip(path):
                continue
            candidate = path.resolve()
            root_files.append(candidate)
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
) -> List[DocEntry]:
    entries: List[DocEntry] = []
    for doc_file in _iter_doc_files(roots, include_tests=include_tests, include_libs=include_libs):
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
        "Macro definitions support three styles:\n"
        "  1. Legacy positional:  macro <name> <count> <tokens...> ;\n"
        "  2. Named signature:    macro <name> (<params...>) <tokens...> ;\n"
        "  3. Pattern clauses:    macro <name> ... => ... ; ... ;\n\n"
        "Compatibility shorthand for generated nested macros:\n"
        "  macro <name> <value> ;\n"
        "When emitted by macro expansion, this defines a 0-arg macro whose\n"
        "body is <value>.\n\n"
        "Placeholders:\n"
        "  $0, $1, ...  legacy positional arguments\n"
        "  $name        named argument\n"
        "  $*name       splice variadic capture\n"
        "A bare '$' token is preserved unchanged (for asm-heavy macros).\n\n"
        "Call styles:\n"
        "  prefix:  m a b\n"
        "  call:    m(a, b, expr)\n"
        "Prefix style consumes fixed args first; variadic tails consume\n"
        "remaining arguments from the same source line.\n\n"
        "Pattern macros are enabled when the body contains => clauses and\n"
        "the definition closes with trailing ';'. Clauses are matched\n"
        "top-to-bottom and compile into grammar-stage rewrite rules.\n"
        "Capture forms:\n"
        "  $x       single-token capture\n"
        "  $*xs     variadic capture\n"
        "  $x:int   constrained capture\n"
        "Repeated capture names enforce equality.\n\n"
        "Use --macro-preview to trace macro/rewrite steps and --preview to\n"
        "inspect the final transformed source after macro + CT execution."
    )
    _L2_MACRO_LANG_DETAIL = (
        _L2_MACRO_CANONICAL_TEXT
        + "\n\n"
        + "Example:\n"
        + "  macro max2 (lhs rhs) $lhs $rhs > if $lhs else $rhs end ;\n"
        + "  max2(5, 3)   # leaves 5 on stack\n\n"
        + "Pattern example:\n"
        + "  macro simplify\n"
        + "    $x:int + 0 => $x ;\n"
        + "    0 + $x:int => $x ;\n"
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
                "Redefinitions overwrite the previous entry with a warning.\n\n"
                "Example:\n"
                "  word square dup * end\n"
                "  word greet \"hello world\" puts end"
            ),
        },
        {
            "name": "inline word ... end",
            "category": "Definitions",
            "syntax": "inline word <name> <body...> end",
            "summary": "Define an inlined word (body is expanded at call sites).",
            "detail": (
                "Marks the definition for inline expansion. "
                "Every call site gets a copy of the body rather than a function call. "
                "Recursive inline calls are rejected at compile time.\n\n"
                "Example:\n"
                "  inline word inc 1 + end"
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
            "summary": "Infinite loop (use `exit` or `goto` to break out).",
            "detail": (
                "Creates an unconditional loop. The body repeats forever.\n"
                "Available only at compile time.\n\n"
                "Example:\n"
                "  begin\n"
                "    read_stdin\n"
                "    dup 0 == if drop exit end\n"
                "    process\n"
                "  again"
            ),
        },
        {
            "name": "continue",
            "category": "Control Flow",
            "syntax": "continue",
            "summary": "Jump to the next iteration of a begin/again loop.",
            "detail": (
                "Valid only inside `begin ... again`. Emits a jump to the loop head. "
                "Using it outside a begin/again loop is a parse error."
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
            "syntax": "flags <token...> | flags \"<token...>\"",
            "summary": "Provide linker/include flags from source.",
            "detail": (
                "Processed during source loading before tokenization. "
                "Supports shell-like token splitting. "
                "`-I`/`--include` update import search paths (relative to the "
                "current file when not absolute). Other tokens are forwarded "
                "as linker/runtime library flags.\n\n"
                "Examples:\n"
                "  flags -lc -lm -L. -I.\n"
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
            "summary": "Execute a word at compile time but still emit it.",
            "detail": (
                "Runs the named word immediately during compilation, "
                "but its definition is also emitted for runtime use."
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
        "  parsing. Code marked `compile-time`, immediate words, and\n"
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
        "    - Text macros: token substitution (fast syntax shaping).\n"
        "    - Pattern macros: grammar rewrite clauses with captures/guards.\n"
        "    - ct-call: bridge from macro templates to CT words with context.\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 0  MACRO TEMPLATE DIRECTIVES\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  These directives run during macro-template expansion (parse time).\n"
        "  They are not runtime words, but they are part of the compile-time\n"
        "  authoring surface and are indexed here for discoverability.\n"
        "\n"
        "  ct-call            [template, ctx | word] -> [template | tokens]\n"
        "    Invoke compile-time word `word` from a template and splice\n"
        "    its return value as emitted tokens.\n"
        "\n"
        "  ct-if              [template | condition] -> [template]\n"
        "    Conditional template branch with optional `else`.\n"
        "\n"
        "  ct-when            [template | condition] -> [template]\n"
        "    Single-branch shorthand for `ct-if ... then ... end`.\n"
        "\n"
        "  ct-unless          [template | condition] -> [template]\n"
        "    Negated single-branch conditional template gate.\n"
        "\n"
        "  ct-for             [template, capture | name] -> [template]\n"
        "    Iterate capture values with optional separator clauses.\n"
        "\n"
        "  ct-each            [template, capture | name] -> [template]\n"
        "    Iterate capture values (supports index+value mode).\n"
        "\n"
        "  ct-let             [template, expr | name] -> [template]\n"
        "    Bind local template symbol for the nested template block.\n"
        "\n"
        "  ct-fn              [template | name] -> [template]\n"
        "    Define a template-local helper callable with `ct-call`.\n"
        "\n"
        "  ct-switch          [template | expr] -> [template]\n"
        "    Multi-branch selector over token-sequence expressions.\n"
        "\n"
        "  ct-case            [template | expr] -> [template]\n"
        "    Case clause inside `ct-switch` or `ct-match`.\n"
        "\n"
        "  ct-default         [template] -> [template]\n"
        "    Fallback clause used by selector directives.\n"
        "\n"
        "  ct-match           [template | expr] -> [template]\n"
        "    Expression-style compact selector with `ct-case` arms.\n"
        "\n"
        "  ct-fold            [template, capture | acc] -> [template]\n"
        "    Accumulator-based template reduction over capture groups.\n"
        "\n"
        "  ct-break           [template] -> [template]\n"
        "    Exit current template loop (`ct-for`/`ct-each`/`ct-fold`).\n"
        "\n"
        "  ct-continue        [template] -> [template]\n"
        "    Skip to next iteration of current template loop.\n"
        "\n"
        "  ct-include         [template | path] -> [template]\n"
        "    Splice template nodes loaded from another source file.\n"
        "\n"
        "  ct-import          [template | path] -> [template]\n"
        "    Import template helpers once per expansion scope.\n"
        "\n"
        "  ct-comment         [template] -> [template]\n"
        "    Start nested template comment block; ends at ct-endcomment.\n"
        "\n"
        "  ct-endcomment      [template] -> [template]\n"
        "    Close `ct-comment` block comment region.\n"
        "\n"
        "  ct-strict          [template] -> [template]\n"
        "    Treat unknown template symbols as hard errors.\n"
        "\n"
        "  ct-permissive      [template] -> [template]\n"
        "    Treat unknown template symbols as empty with warning.\n"
        "\n"
        "  ct-version         [template | tag] -> [template]\n"
        "    Attach version marker metadata to the template program.\n"
        "\n"
        "  ct-error           [template | message] -> [template]\n"
        "    Emit template expansion error diagnostic.\n"
        "\n"
        "  ct-warning         [template | message] -> [template]\n"
        "    Emit template expansion warning diagnostic.\n"
        "\n"
        "  ct-note            [template | message] -> [template]\n"
        "    Emit template expansion note diagnostic.\n"
        "\n"
        "  emit-list          [template | capture] -> [template | tokens]\n"
        "    Splice capture/list value as emitted token sequence.\n"
        "\n"
        "  ct-emit-list       [template | capture] -> [template | tokens]\n"
        "    Alias of emit-list for explicit compile-time naming.\n"
        "\n"
        "  emit-block         [template | block] -> [template | tokens]\n"
        "    Emit inline nested template block as token output.\n"
        "\n"
        "  ct-emit-block      [template | block] -> [template | tokens]\n"
        "    Alias of emit-block for explicit compile-time naming.\n"
        "\n"
        "  Comment aliases: `ct-#`, `ct-#(`, `ct-#)` are also supported for\n"
        "  template comments, matching legacy shorthand forms.\n"
        "\n"
        "  Suggested reading order:\n"
        "    - Start with § 1 (execution mode and hooks).\n"
        "    - Then § 6 (token stream + rewrites).\n"
        "    - Then capture/rewrite sections for advanced macro toolchains.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 1  COMPILE-TIME HOOKS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  compile-time                             [immediate]\n"
        "    Marks a word definition so that its body\n"
        "    runs in the CT VM. The word's definition\n"
        "    is interpreted by the VM when the\n"
        "    word is referenced during compilation.\n"
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
        "  CT                                       [runtime + compile-time]\n"
        "    Pushes 1 when running in compile-time execution and 0 in\n"
        "    emitted runtime code, so words can branch on execution\n"
        "    mode explicitly.\n"
        "\n"
        "      CT puti cr   # prints 1 at compile time and 0 at runtime\n"
        "\n"
        "  use-l2-ct                           [immediate, compile-only]\n"
        "    Replace the built-in CT intrinsic of a word with its L2\n"
        "    definition body. With a name on the stack, targets that\n"
        "    word; with an empty stack, targets the most recently\n"
        "    defined word.\n"
        "\n"
        "      word 3dup dup dup dup end  use-l2-ct\n"
        "\n"
        "  set-token-hook                           [compile-only]\n"
        "    [* | name] -> [*]\n"
        "    Register a word as the token hook. Every token the parser\n"
        "    encounters is pushed onto the CT stack, the hook word is\n"
        "    invoked, and the result (0 = not handled, 1 = handled)\n"
        "    tells the parser whether to skip normal processing.\n"
        "\n"
        "  clear-token-hook                         [compile-only]\n"
        "    [*] -> [*]\n"
        "    Remove the currently active token hook.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 2  LIST OPERATIONS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Lists are dynamic arrays that live in the CT VM. They hold\n"
        "  integers, strings, tokens, other lists, maps, or nil.\n"
        "\n"
        "  list-new          [*] -> [* | list]\n"
        "    Create a new empty list.\n"
        "\n"
        "  list-clone         [* | list] -> [* | copy]\n"
        "    Shallow-copy a list.\n"
        "\n"
        "  list-append        [*, list | value] -> [* | list]\n"
        "    Append value to the end of list (mutates in place).\n"
        "\n"
        "  list-pop            [* | list] -> [*, list | value]\n"
        "    Remove and return the last element.\n"
        "\n"
        "  list-pop-front      [* | list] -> [*, list | value]\n"
        "    Remove and return the first element.\n"
        "\n"
        "  list-peek-front     [* | list] -> [*, list | value]\n"
        "    Return the first element without removing it.\n"
        "\n"
        "  list-push-front     [*, list | value] -> [* | list]\n"
        "    Insert value at the beginning of list.\n"
        "\n"
        "  list-reverse        [* | list] -> [* | list]\n"
        "    Reverse the list in place.\n"
        "\n"
        "  list-length         [* | list] -> [* | n]\n"
        "    Push the number of elements.\n"
        "\n"
        "  list-empty?         [* | list] -> [* | flag]\n"
        "    Push 1 if the list is empty, 0 otherwise.\n"
        "\n"
        "  list-get            [*, list | index] -> [* | value]\n"
        "    Get element at index (0-based). Errors on out-of-range.\n"
        "\n"
        "  list-set            [*, list, index | value] -> [* | list]\n"
        "    Set element at index. Errors on out-of-range.\n"
        "\n"
        "  list-clear          [* | list] -> [* | list]\n"
        "    Remove all elements from the list.\n"
        "\n"
        "  list-extend         [*, target | source] -> [* | target]\n"
        "    Append all elements of source to target.\n"
        "\n"
        "  list-last           [* | list] -> [* | value]\n"
        "    Push the last element without removing it.\n"
        "\n"
        "  list-insert         [*, list, index | value] -> [* | list]\n"
        "    Insert value at index, shifting following elements right.\n"
        "\n"
        "  list-remove         [*, list | index] -> [*, list | value]\n"
        "    Remove and return element at index.\n"
        "\n"
        "  list-slice          [*, list, start | end] -> [* | sublist]\n"
        "    Push a new list with list[start:end].\n"
        "\n"
        "  list-find           [*, list | value] -> [*, index | found]\n"
        "    Search list for value. Returns (index, 1) or (-1, 0).\n"
        "\n"
        "  list-contains?      [*, list | value] -> [* | flag]\n"
        "    Push 1 if value exists in list, 0 otherwise.\n"
        "\n"
        "  list-join           [*, list | separator] -> [* | str]\n"
        "    Join list elements (string-compatible) with separator.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 3  MAP OPERATIONS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Maps are string-keyed dictionaries in the CT VM.\n"
        "\n"
        "  map-new             [*] -> [* | map]\n"
        "    Create a new empty map.\n"
        "\n"
        "  map-set             [*, map, key | value] -> [* | map]\n"
        "    Set key to value in the map (mutates in place).\n"
        "\n"
        "  map-get             [*, map | key] -> [*, map, value | flag]\n"
        "    Look up key. Pushes the map back, then the value\n"
        "    (or nil if absent), then 1 if found or 0 if not.\n"
        "\n"
        "  map-has?            [*, map | key] -> [*, map | flag]\n"
        "    Push 1 if the key exists in the map, 0 otherwise.\n"
        "\n"
        "  map-delete          [*, map | key] -> [*, map | flag]\n"
        "    Delete key if present. Returns 1 when deleted, else 0.\n"
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
        "    Push a list of map keys.\n"
        "\n"
        "  map-values          [* | map] -> [* | list]\n"
        "    Push a list of map values.\n"
        "\n"
        "  map-clone           [* | map] -> [* | copy]\n"
        "    Shallow-copy a map.\n"
        "\n"
        "  map-update          [*, target | source] -> [* | target]\n"
        "    Merge source entries into target (mutates target).\n"
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
        "  Strings in the CT VM are immutable sequences of characters.\n"
        "\n"
        "  string=             [*, a | b] -> [* | flag]\n"
        "    Push 1 if strings a and b are equal, 0 otherwise.\n"
        "\n"
        "  string-length       [* | str] -> [* | n]\n"
        "    Push the length of the string.\n"
        "\n"
        "  string-append       [*, left | right] -> [* | result]\n"
        "    Concatenate two strings.\n"
        "\n"
        "  string>number       [* | str] -> [*, value | flag]\n"
        "    Parse an integer from the string (supports 0x, 0b, 0o\n"
        "    prefixes). Pushes (value, 1) on success or (0, 0) on\n"
        "    failure.\n"
        "\n"
        "  int>string          [* | n] -> [* | str]\n"
        "    Convert an integer to its decimal string representation.\n"
        "\n"
        "  identifier?         [* | value] -> [* | flag]\n"
        "    Push 1 if the value is a valid L2 identifier string,\n"
        "    0 otherwise. Also accepts token objects.\n"
        "\n"
        "  string-contains?    [*, haystack | needle] -> [* | flag]\n"
        "    Push 1 if needle occurs inside haystack, else 0.\n"
        "\n"
        "  string-starts-with? [*, text | prefix] -> [* | flag]\n"
        "    Push 1 if text starts with prefix, else 0.\n"
        "\n"
        "  string-ends-with?   [*, text | suffix] -> [* | flag]\n"
        "    Push 1 if text ends with suffix, else 0.\n"
        "\n"
        "  string-split        [*, text | sep] -> [* | list]\n"
        "    Split text by non-empty separator and return list of parts.\n"
        "\n"
        "  string-join         [*, list | sep] -> [* | str]\n"
        "    Join string-compatible list items with separator.\n"
        "\n"
        "  string-strip        [* | text] -> [* | stripped]\n"
        "    Trim leading/trailing whitespace.\n"
        "\n"
        "  string-replace      [*, text, old | new] -> [* | replaced]\n"
        "    Replace all old substrings with new.\n"
        "\n"
        "  string-upper        [* | text] -> [* | upper]\n"
        "    Uppercase conversion.\n"
        "\n"
        "  string-lower        [* | text] -> [* | lower]\n"
        "    Lowercase conversion.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 6  TOKEN STREAM MANIPULATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  These words give compile-time code direct control over\n"
        "  the token stream the parser reads from.\n"
        "\n"
        "  next-token          [*] -> [* | token]\n"
        "    Consume and push the next token from the parser.\n"
        "\n"
        "  peek-token          [*] -> [* | token]\n"
        "    Push the next token without consuming it.\n"
        "\n"
        "  token-lexeme        [* | token] -> [* | str]\n"
        "    Extract the lexeme (text) from a token or string.\n"
        "\n"
        "  token-from-lexeme   [*, lexeme | template] -> [* | token]\n"
        "    Create a new token with the given lexeme, using source\n"
        "    location from the template token.\n"
        "\n"
        "  token-with-lexeme   [*, token | lexeme] -> [* | token]\n"
        "    Clone token metadata and replace only the lexeme text.\n"
        "\n"
        "  token-clone         [* | token] -> [* | token]\n"
        "    Return a shallow clone of token metadata and lexeme.\n"
        "\n"
        "  token-shift-column  [*, token | delta] -> [* | token]\n"
        "    Clone token with shifted source column/start/end offsets.\n"
        "\n"
        "  token-line          [* | token] -> [* | line]\n"
        "    Return token source line number.\n"
        "\n"
        "  token-column        [* | token] -> [* | column]\n"
        "    Return token source column number.\n"
        "\n"
        "  inject-tokens       [* | list-of-tokens] -> [*]\n"
        "    Insert a list of token objects at the current parser\n"
        "    position. The parser will read them before continuing\n"
        "    with the original stream.\n"
        "\n"
        "  inject-lexemes      [*, list-of-lexemes | template-token] -> [*]\n"
        "    Build token objects from lexeme strings (or token values)\n"
        "    using template source location, then inject them at the\n"
        "    current parser position.\n"
        "\n"
        "  add-token           [* | str] -> [*]\n"
        "    Register a single-character string as a token separator\n"
        "    recognized by the reader.\n"
        "\n"
        "  add-token-chars     [* | str] -> [*]\n"
        "    Register each character of the string as a token\n"
        "    separator character.\n"
        "\n"
        "  ct-add-reader-rewrite [*, pattern-list | replacement-list] -> [* | name]\n"
        "    Install a reader-stage rewrite rule. Pattern/replacement\n"
        "    are token-lexeme lists. Supports captures:\n"
        "      $x / $0        single-token capture\n"
        "      $*xs           variadic capture\n"
        "      $x:int         constrained capture\n"
        "    Repeated capture names enforce equality across matches.\n"
        "\n"
        "  ct-add-grammar-rewrite [*, pattern-list | replacement-list] -> [* | name]\n"
        "    Install a grammar-stage rewrite rule (runs after token hook\n"
        "    and before normal token handling).\n"
        "    Reader rewrites are tokenization-adjacent; grammar rewrites\n"
        "    are syntax-shaping and usually safer for language extensions.\n"
        "\n"
        "  ct-add-reader-rewrite-named  [*, name, pattern | replacement] -> [* | name]\n"
        "\n"
        "  ct-add-grammar-rewrite-named [*, name, pattern | replacement] -> [* | name]\n"
        "    Named variants that replace any existing rule with the same\n"
        "    name (idempotent upsert behavior).\n"
        "\n"
        "    Example (grammar alias):\n"
        "      # rewrite: kw -> 42\n"
        "      list-new \"kw\" list-append\n"
        "      list-new \"42\" list-append\n"
        "      ct-add-grammar-rewrite drop\n"
        "\n"
        "  ct-remove-reader-rewrite   [* | name] -> [* | flag]\n"
        "    Remove a single reader-stage rewrite rule by exact name.\n"
        "    Returns 1 when a rule was found and deleted, else 0.\n"
        "    Use this for idempotent cleanup in setup code.\n"
        "\n"
        "  ct-remove-grammar-rewrite  [* | name] -> [* | flag]\n"
        "    Remove one grammar-stage rewrite rule by exact name.\n"
        "    Returns 1 when removed, or 0 when the name does not exist.\n"
        "    Useful when replacing generated syntax rules dynamically.\n"
        "\n"
        "  ct-clear-reader-rewrites   [*] -> [* | count]\n"
        "    Delete all reader-stage rewrite rules at once.\n"
        "    Returns the number of rules removed so callers can assert\n"
        "    expected cleanup behavior in compile-time tests.\n"
        "\n"
        "  ct-clear-grammar-rewrites  [*] -> [* | count]\n"
        "    Delete all grammar-stage rewrite rules and invalidate\n"
        "    matcher indexes immediately. Returns removed rule count.\n"
        "\n"
        "  ct-list-reader-rewrites    [*] -> [* | list]\n"
        "    Return rule names for the reader stage in active evaluation\n"
        "    order (priority first, then insertion order tie-break).\n"
        "\n"
        "  ct-list-grammar-rewrites   [*] -> [* | list]\n"
        "    Return grammar-stage rule names in current execution order.\n"
        "    Use with get/set priority helpers to inspect final ordering.\n"
        "\n"
        "  ct-add-reader-rewrite-priority [*, priority, pattern | replacement] -> [* | name]\n"
        "\n"
        "  ct-add-grammar-rewrite-priority [*, priority, pattern | replacement] -> [* | name]\n"
        "    Install rewrite rules with explicit priority. Higher priority\n"
        "    rules are tried first. Ties preserve insertion order.\n"
        "\n"
        "  ct-set-reader-rewrite-enabled  [*, name | flag] -> [* | ok]\n"
        "\n"
        "  ct-set-grammar-rewrite-enabled [*, name | flag] -> [* | ok]\n"
        "\n"
        "  ct-get-reader-rewrite-enabled  [* | name] -> [*, flag | found]\n"
        "\n"
        "  ct-get-grammar-rewrite-enabled [* | name] -> [*, flag | found]\n"
        "    Enable/disable rules and query rule status. get-* returns\n"
        "    [flag found] so missing names are distinguishable from false.\n"
        "\n"
        "  ct-set-reader-rewrite-priority  [*, name | priority] -> [* | ok]\n"
        "\n"
        "  ct-set-grammar-rewrite-priority [*, name | priority] -> [* | ok]\n"
        "\n"
        "  ct-get-reader-rewrite-priority  [* | name] -> [*, priority | found]\n"
        "\n"
        "  ct-get-grammar-rewrite-priority [* | name] -> [*, priority | found]\n"
        "    Adjust/query rewrite priority after registration.\n"
        "\n"
        "  ct-current-token      [*] -> [* | token]\n"
        "    Return most recently consumed token (or nil before any\n"
        "    parser consumption in current compile-time flow).\n"
        "\n"
        "  ct-parser-pos         [*] -> [* | n]\n"
        "    Return absolute parser cursor index within current token\n"
        "    stream (0-based, inclusive start position).\n"
        "\n"
        "  ct-parser-remaining   [*] -> [* | n]\n"
        "    Return remaining token count from current cursor to stream\n"
        "    end (never negative).\n"
        "\n"
        "  ct-parser-eof?        [*] -> [* | flag]\n"
        "    Push 1 when parser cursor is at/after end-of-stream,\n"
        "    otherwise 0.\n"
        "\n"
        "  ct-parser-peek        [* | n] -> [* | token]\n"
        "    Non-consuming lookahead by offset n from current cursor.\n"
        "    n must be >= 0; out-of-range returns nil.\n"
        "\n"
        "  ct-parser-set-pos     [* | n] -> [* | old-n]\n"
        "    Move cursor to absolute position n (0 <= n <= len(tokens)).\n"
        "    Returns previous cursor; updates ct-current-token coherently.\n"
        "\n"
        "  ct-parser-checkpoint  [*] -> [* | checkpoint-map]\n"
        "    Snapshot parser cursor state for speculative parsing.\n"
        "    Map includes pos, last_token, and remaining keys.\n"
        "\n"
        "  ct-parser-restore     [* | checkpoint] -> [* | flag]\n"
        "    Restore cursor from checkpoint map or raw integer position.\n"
        "    Returns 1 on successful restore; validates bounds/types.\n"
        "\n"
        "  ct-parser-tail        [*] -> [* | list-of-tokens]\n"
        "    Return copy of remaining token objects from current cursor.\n"
        "    Useful for diagnostics and custom parser lookahead.\n"
        "\n"
        "  ct-parser-session-begin [*] -> [* | depth]\n"
        "    Push full parser state snapshot (tokens, pos, last_token)\n"
        "    onto nested session stack and return new depth.\n"
        "\n"
        "  ct-parser-session-commit [*] -> [* | flag]\n"
        "    Commit top session (discard snapshot) and keep current\n"
        "    parser state. Returns 1 when session existed, else 0.\n"
        "\n"
        "  ct-parser-session-rollback [*] -> [* | flag]\n"
        "    Restore parser to top session snapshot and pop it.\n"
        "    Returns 1 when restored, else 0 if no active session.\n"
        "\n"
        "  ct-parser-collect-until [* | delimiter] -> [* | tokens found]\n"
        "    Consume tokens until delimiter lexeme is encountered.\n"
        "    Delimiter is consumed; output excludes delimiter token.\n"
        "\n"
        "  ct-parser-collect-balanced [*, open | close] -> [* | tokens found]\n"
        "    Consume balanced token region for open/close delimiters.\n"
        "    Tracks nested open tokens; closing token at depth 0 ends read.\n"
        "\n"
        "  ct-parser-mark [* | name] -> [*, prev-pos | had-prev]\n"
        "    Save current cursor under string mark name. Returns previous\n"
        "    position and had-prev flag (0 when mark was newly created).\n"
        "\n"
        "  ct-parser-diff [*, start | end] -> [* | map]\n"
        "    Compare two parser positions/marks/checkpoints and return map\n"
        "    with start,end,delta,forward,count,lexemes fields.\n"
        "\n"
        "  ct-parser-expected [* | expected] -> [* | token]\n"
        "    Validate next token lexeme against expected value(s).\n"
        "    expected can be string/token or list of strings/tokens;\n"
        "    raises parse-error on mismatch and returns matched token.\n"
        "\n"
        "  ct-set-macro-expansion-limit [* | n] -> [*]\n"
        "\n"
        "  ct-get-macro-expansion-limit [*] -> [* | n]\n"
        "    Configure/query parser macro+rewrite expansion guard limit.\n"
        "\n"
        "  ct-set-macro-preview   [* | flag] -> [*]\n"
        "\n"
        "  ct-get-macro-preview   [*] -> [* | flag]\n"
        "    Enable/query preview tracing for macro+rewrite expansions.\n"
        "    Preview output includes the expanded token stream and a\n"
        "    source-context window around the expansion site.\n"
        "\n"
        "  ct-register-text-macro [*, name, params | expansion-list] -> [*]\n"
        "    Programmatically register a text macro from compile-time code.\n"
        "\n"
        "  ct-register-text-macro-signature [*, name, param-spec | expansion-list] -> [*]\n"
        "    Register a text macro with explicit parameter names.\n"
        "    `param-spec` is a list of identifiers. Prefix one with\n"
        "    `*` or `...` to mark it variadic (must be last).\n"
        "\n"
        "  ct-register-pattern-macro  [*, name | clauses] -> [*]\n"
        "    Register a pattern macro from compile-time data. Existing\n"
        "    rules under the same macro name are replaced first.\n"
        "    `clauses` accepts:\n"
        "      [pattern replacement]\n"
        "      [pattern replacement guard]\n"
        "      {pattern, replacement, guard?, group?, scope?, metadata?}\n"
        "    Internally each clause becomes grammar rewrite rule\n"
        "    pattern-macro:<name>:<index> with provenance metadata.\n"
        "\n"
        "  ct-unregister-pattern-macro  [* | name] -> [* | removed]\n"
        "    Remove all generated rewrite rules for a pattern macro and\n"
        "    clear associated grouping/scope bookkeeping. Returns 1 when\n"
        "    any rule was removed, else 0.\n"
        "\n"
        "  ct-word-is-text-macro    [* | name] -> [* | flag]\n"
        "    Return 1 when `name` resolves to a text macro\n"
        "    (word with macro expansion tokens), else 0.\n"
        "\n"
        "  ct-word-is-pattern-macro  [* | name] -> [* | flag]\n"
        "    Return 1 when `name` has registered pattern-macro rewrite\n"
        "    clauses, else 0.\n"
        "\n"
        "  ct-get-macro-signature   [* | name] -> [*, params, variadic | found]\n"
        "    Query text-macro parameter shape. On success pushes:\n"
        "      - params: ordered parameter-name list\n"
        "      - variadic: variadic parameter name or nil\n"
        "      - found: 1\n"
        "    For legacy count-based macros, params are synthesized as\n"
        "    [\"0\", \"1\", ...]. Missing/non-text macro returns nil nil 0.\n"
        "\n"
        "  ct-get-macro-expansion   [* | name] -> [*, expansion | found]\n"
        "    Return text-macro expansion token list and found flag.\n"
        "    Missing macro returns nil 0.\n"
        "\n"
        "  ct-set-macro-expansion   [*, name | expansion] -> [* | ok]\n"
        "    Replace expansion token list for an existing text macro.\n"
        "    Also resets cached template AST/program and profiling state\n"
        "    so future expansions recompile with new content.\n"
        "\n"
        "  ct-clone-macro           [*, source | target] -> [* | ok]\n"
        "    Clone macro behavior from source to target. Text and pattern\n"
        "    macros are supported, including docs/attrs/schema/taint and\n"
        "    ct-call contract metadata when present. Returns 0 on conflicts.\n"
        "\n"
        "  ct-rename-macro          [*, source | target] -> [* | ok]\n"
        "    Rename by cloning then removing source macro state.\n"
        "    If source was active token hook, hook binding is cleared.\n"
        "\n"
        "  ct-macro-doc-get         [* | name] -> [*, doc | found]\n"
        "    Read optional free-form documentation text attached to macro.\n"
        "    Returns nil 0 when no doc exists.\n"
        "\n"
        "  ct-macro-doc-set         [*, name | doc-or-nil] -> [* | ok]\n"
        "    Attach or remove macro documentation. Passing nil deletes\n"
        "    existing doc entry. Returns 1 on success, 0 when macro missing.\n"
        "\n"
        "  ct-macro-attrs-get       [* | name] -> [*, attrs | found]\n"
        "    Read structured attribute map attached to macro. Returned map\n"
        "    is deep-cloned to avoid accidental shared mutable state.\n"
        "\n"
        "  ct-macro-attrs-set       [*, name | attrs-or-nil] -> [* | ok]\n"
        "    Attach or remove macro attribute map. Keys are normalized to\n"
        "    strings and values are deep-cloned for isolation.\n"
        "\n"
        "  ct-get-macro-template-mode  [* | name] -> [*, mode | found]\n"
        "    Introspect compiled template mode for text macros using\n"
        "    directives (strict/permissive). Triggers lazy template parse\n"
        "    if needed. Missing macro returns nil 0.\n"
        "\n"
        "  ct-get-macro-template-version  [* | name] -> [*, version-or-nil | found]\n"
        "    Return optional ct-version marker captured from macro template.\n"
        "    Missing macro returns nil 0.\n"
        "\n"
        "  ct-get-macro-template-program-size  [* | name] -> [*, count | found]\n"
        "    Return top-level node count for precompiled template program.\n"
        "    Useful for complexity checks and tooling diagnostics.\n"
        "\n"
        "  ct-set-ct-call-contract  [*, word | map-or-nil] -> [* | ok]\n"
        "    Set ABI/shape contract for template `ct-call` target `word`.\n"
        "    Passing nil removes the contract. Returns 0 when target word\n"
        "    does not exist in dictionary.\n"
        "\n"
        "  ct-get-ct-call-contract  [* | word] -> [*, map | found]\n"
        "    Read ct-call contract map for word (deep copy). Returns nil 0\n"
        "    when no contract is configured.\n"
        "\n"
        "  ct-set-ct-call-exception-policy  [* | policy] -> [*]\n"
        "    Set ct-call failure behavior. Allowed values:\n"
        "      - raise: propagate as parse error\n"
        "      - warn: emit warning and continue\n"
        "      - empty: treat failure as empty expansion\n"
        "      - ignore: synonym for empty behavior\n"
        "\n"
        "  ct-get-ct-call-exception-policy  [*] -> [* | policy]\n"
        "    Return current ct-call exception policy string.\n"
        "\n"
        "  ct-set-ct-call-sandbox-mode  [* | mode] -> [*]\n"
        "    Set ct-call execution sandbox mode:\n"
        "      - off: no restrictions\n"
        "      - allowlist: only names in sandbox allowlist\n"
        "      - compile-only: only compile-only target words\n"
        "\n"
        "  ct-get-ct-call-sandbox-mode  [*] -> [* | mode]\n"
        "    Return active sandbox mode string.\n"
        "\n"
        "  ct-set-ct-call-sandbox-allowlist  [* | words] -> [* | count]\n"
        "    Replace allowlist set from lexeme list. Duplicates are removed\n"
        "    by set semantics. Returns resulting unique word count.\n"
        "\n"
        "  ct-get-ct-call-sandbox-allowlist  [*] -> [* | words]\n"
        "    Return sorted list of allowlisted ct-call target names.\n"
        "\n"
        "  ct-ctrand-seed           [* | seed] -> [*]\n"
        "    Seed deterministic compile-time RNG used by ct-ctrand-* APIs.\n"
        "\n"
        "  ct-ctrand-int            [* | bound] -> [* | n]\n"
        "    Return random integer n in range [0, bound). Requires bound > 0.\n"
        "\n"
        "  ct-ctrand-range          [*, lo | hi] -> [* | n]\n"
        "    Return random integer n in inclusive range [lo, hi].\n"
        "    Raises parse error when hi < lo.\n"
        "\n"
        "  ct-set-ct-call-memo      [* | flag] -> [*]\n"
        "    Enable/disable ct-call memoization cache lookups and writes.\n"
        "    This toggles behavior only; existing cache entries are not\n"
        "    automatically cleared. Use ct-clear-ct-call-memo for reset.\n"
        "\n"
        "  ct-get-ct-call-memo      [*] -> [* | flag]\n"
        "    Return 1 when ct-call memoization is enabled, else 0.\n"
        "\n"
        "  ct-clear-ct-call-memo    [*] -> [* | count]\n"
        "    Clear all memoized ct-call entries and return removed count.\n"
        "\n"
        "  ct-get-ct-call-memo-size [*] -> [* | n]\n"
        "    Return the current number of entries in ct-call memo cache.\n"
        "\n"
        "  ct-set-ct-call-side-effects [* | flag] -> [*]\n"
        "    Enable/disable side-effect tracking for ct-call execution.\n"
        "    When enabled, ct-call emits structured log entries.\n"
        "\n"
        "  ct-get-ct-call-side-effects [*] -> [* | flag]\n"
        "    Return 1 when ct-call side-effect tracking is enabled, else 0.\n"
        "\n"
        "  ct-get-ct-call-side-effect-log [*] -> [* | list]\n"
        "    Return deep-copied side-effect log entries collected from\n"
        "    ct-call execution.\n"
        "\n"
        "  ct-clear-ct-call-side-effect-log [*] -> [* | count]\n"
        "    Clear side-effect log and return number of removed entries.\n"
        "\n"
        "  ct-set-ct-call-recursion-limit [* | n] -> [*]\n"
        "    Set recursion guard for nested ct-call expansion.\n"
        "    Requires n >= 1 (otherwise parse error).\n"
        "\n"
        "  ct-get-ct-call-recursion-limit [*] -> [* | n]\n"
        "    Return current ct-call recursion guard limit.\n"
        "\n"
        "  ct-set-ct-call-timeout-ms [* | ms] -> [*]\n"
        "    Set per-ct-call timeout budget in milliseconds.\n"
        "    Use 0 to disable timeout checks.\n"
        "\n"
        "  ct-get-ct-call-timeout-ms [*] -> [* | ms]\n"
        "    Return current ct-call timeout budget (ms).\n"
        "\n"
        "  ct-gensym                [* | prefix] -> [* | symbol]\n"
        "    Hygienic name generator for macro pipelines. Prefix is\n"
        "    sanitized to identifier-safe text and a counter suffix is\n"
        "    incremented until dictionary name collision is avoided.\n"
        "\n"
        "  ct-capture-args          [* | ctx] -> [* | map]\n"
        "    Return deep-cloned capture `args` namespace from context.\n"
        "\n"
        "  ct-capture-locals        [* | ctx] -> [* | map]\n"
        "    Return deep-cloned capture `locals` namespace from context.\n"
        "\n"
        "  ct-capture-globals       [* | ctx] -> [* | map]\n"
        "    Return deep-cloned capture `globals` namespace from context.\n"
        "\n"
        "  ct-capture-get           [*, ctx | name] -> [*, value | found]\n"
        "    Lookup capture by name from ctx.captures. Missing capture\n"
        "    returns nil 0.\n"
        "\n"
        "  ct-capture-has?          [*, ctx | name] -> [* | flag]\n"
        "    Return 1 when capture exists in ctx.captures, else 0.\n"
        "\n"
        "  ct-capture-shape         [* | value] -> [* | shape]\n"
        "    Return normalized shape tag: none/single/tokens/multi/scalar.\n"
        "\n"
        "  ct-capture-assert-shape  [*, value | shape] -> [*]\n"
        "    Assert expected shape against ct-capture-shape result and\n"
        "    raise parse error on mismatch.\n"
        "\n"
        "  ct-capture-count         [* | value] -> [* | n]\n"
        "    Return element/group count for list/group-list captures.\n"
        "    Nil yields 0; scalar values raise parse error.\n"
        "\n"
        "  ct-capture-slice         [*, value, start | end] -> [* | sliced]\n"
        "    Slice list/group-list using Python-style [start:end].\n"
        "    Nil yields empty list.\n"
        "\n"
        "  ct-capture-map           [*, value | op] -> [* | value]\n"
        "    Apply token transform op over capture values. Supported ops:\n"
        "    upper, lower, strip, int, int-normalize.\n"
        "\n"
        "  ct-capture-filter        [*, value | predicate] -> [* | value]\n"
        "    Filter token values by predicate (`nonempty`, `ident`, `int`,\n"
        "    `number`, `string`, `char`, or rewrite-constraint names).\n"
        "\n"
        "  ct-capture-separate      [*, value | sep] -> [* | tokens]\n"
        "    Flatten value into token list; for variadic group-lists\n"
        "    inserts separator token between groups.\n"
        "\n"
        "  ct-capture-join          [*, value | sep] -> [* | text]\n"
        "    Join capture tokens/groups into a string with separator.\n"
        "\n"
        "  ct-capture-equal?        [*, left | right] -> [* | flag]\n"
        "    Deep equality over normalized capture values (1/0).\n"
        "\n"
        "  ct-capture-normalize     [* | value] -> [* | normalized]\n"
        "    Convert capture value into stable plain data form\n"
        "    (tokens to lexemes, map keys to strings, tuples to lists).\n"
        "\n"
        "  ct-capture-pretty        [* | value] -> [* | json]\n"
        "    Pretty-print normalized capture value as indented JSON text.\n"
        "\n"
        "  ct-capture-clone         [* | value] -> [* | value]\n"
        "    Deep clone capture value to avoid aliasing/mutation leaks.\n"
        "\n"
        "  ct-capture-coerce-tokens [* | value] -> [* | tokens]\n"
        "    Coerce value into flat token-lexeme list.\n"
        "\n"
        "  ct-capture-coerce-string [* | value] -> [* | text]\n"
        "    Coerce value into string. Existing strings pass through;\n"
        "    other values are flattened and joined with spaces.\n"
        "\n"
        "  ct-capture-coerce-number [* | value] -> [*, n | found]\n"
        "    Coerce bool/int/token/string/single-token capture into int.\n"
        "    On failure returns 0 0.\n"
        "\n"
        "  ct-capture-origin        [* | ctx] -> [* | map]\n"
        "    Return deep-cloned origin metadata map from capture context.\n"
        "\n"
        "  ct-capture-lifetime      [* | ctx] -> [* | id]\n"
        "    Return integer lifetime identifier recorded in context.\n"
        "\n"
        "  ct-capture-lifetime-live? [* | ctx] -> [* | flag]\n"
        "    Return 1 when context lifetime matches active parser lifetime.\n"
        "\n"
        "  ct-capture-lifetime-assert [* | ctx] -> [*]\n"
        "    Raise parse error when context lifetime is stale or missing.\n"
        "\n"
        "  ct-capture-lint          [* | ctx] -> [* | warnings]\n"
        "    Return lint warnings for suspicious capture context content\n"
        "    (invalid names, empty groups, taint, stale lifetime).\n"
        "\n"
        "  ct-capture-global-set    [*, name | value] -> [*]\n"
        "    Set parser-global capture value under name (deep-cloned).\n"
        "\n"
        "  ct-capture-global-get    [* | name] -> [*, value | found]\n"
        "    Read parser-global capture value by name; missing -> nil 0.\n"
        "\n"
        "  ct-capture-global-delete [* | name] -> [* | removed]\n"
        "    Delete parser-global capture value and return removed flag.\n"
        "\n"
        "  ct-capture-global-clear  [*] -> [* | count]\n"
        "    Clear all parser-global capture values and return removed count.\n"
        "\n"
        "  ct-capture-freeze        [*, macro | name] -> [*]\n"
        "    Freeze capture mutability for (macro, name).\n"
        "\n"
        "  ct-capture-thaw          [*, macro | name] -> [* | removed]\n"
        "    Remove frozen mutability entry for (macro, name).\n"
        "\n"
        "  ct-capture-mutable?      [*, macro | name] -> [* | flag]\n"
        "    Return 1 when capture is mutable, 0 when frozen.\n"
        "\n"
        "  ct-capture-schema-put    [*, macro, name, shape, type | required] -> [*]\n"
        "    Define capture schema rule for macro capture name.\n"
        "    Shape must be any/single/tokens/multi/none/scalar.\n"
        "\n"
        "  ct-capture-schema-get    [* | macro] -> [*, schema | found]\n"
        "    Fetch deep-cloned capture schema for macro. Missing -> nil 0.\n"
        "\n"
        "  ct-capture-schema-validate [* | ctx] -> [* | ok]\n"
        "    Validate context captures against registered schema for ctx.macro.\n"
        "    Pushes 1 on success; parse error on violations.\n"
        "\n"
        "  ct-capture-taint-set     [*, macro, name | flag] -> [*]\n"
        "    Set taint flag for named capture under macro scope.\n"
        "\n"
        "  ct-capture-taint-get     [*, macro | name] -> [*, flag | found]\n"
        "    Read taint flag for (macro, capture). Missing -> 0 0.\n"
        "\n"
        "  ct-capture-tainted?      [*, ctx | name] -> [* | flag]\n"
        "    Read taint state directly from context taint map.\n"
        "\n"
        "  ct-capture-serialize     [* | value] -> [* | json]\n"
        "    Serialize normalized value to canonical compact JSON.\n"
        "\n"
        "  ct-capture-deserialize   [* | json] -> [* | value]\n"
        "    Parse JSON payload into compile-time value (parse error on invalid).\n"
        "\n"
        "  ct-capture-compress      [* | text] -> [* | blob]\n"
        "    Compress UTF-8 text with zlib and encode as base64 ASCII blob.\n"
        "\n"
        "  ct-capture-decompress    [* | blob] -> [* | text]\n"
        "    Decode base64 + zlib blob and return UTF-8 text.\n"
        "    Invalid payload raises parse error.\n"
        "\n"
        "  ct-capture-hash          [* | value] -> [* | sha256]\n"
        "    SHA-256 hash of canonical serialized normalized value.\n"
        "\n"
        "  ct-capture-diff          [*, left | right] -> [* | list]\n"
        "    Return deep structural diff list with path-qualified mismatch text.\n"
        "\n"
        "  ct-capture-replay-log    [*] -> [* | list]\n"
        "    Return deep-cloned capture replay log entries.\n"
        "\n"
        "  ct-capture-replay-clear  [*] -> [* | count]\n"
        "    Clear capture replay log and return removed entry count.\n"
        "\n"
        "  ct-list-pattern-macros   [*] -> [* | names]\n"
        "    Return sorted list of pattern macro names with registered clauses.\n"
        "\n"
        "  ct-set-pattern-macro-enabled  [*, name | flag] -> [* | ok]\n"
        "    Enable/disable named pattern macro. Returns 0 when missing.\n"
        "\n"
        "  ct-get-pattern-macro-enabled  [* | name] -> [*, flag | found]\n"
        "    Read enabled flag for named pattern macro. Missing -> 0 0.\n"
        "\n"
        "  ct-set-pattern-macro-priority [*, name | priority] -> [* | ok]\n"
        "    Set numeric clause-priority bias for pattern macro.\n"
        "\n"
        "  ct-get-pattern-macro-priority [* | name] -> [*, priority | found]\n"
        "    Read pattern macro priority. Missing -> 0 0.\n"
        "\n"
        "  ct-get-pattern-macro-clauses  [* | name] -> [*, clauses | found]\n"
        "    Return simplified clause payload as list of\n"
        "    [pattern-list replacement-list] pairs.\n"
        "\n"
        "  ct-get-pattern-macro-clause-details [* | name] -> [*, details | found]\n"
        "    Return full clause detail maps (guards/group/scope/metadata).\n"
        "\n"
        "  ct-set-pattern-macro-group [*, name | group] -> [* | ok]\n"
        "    Assign macro to activation group. Returns 0 when missing.\n"
        "\n"
        "  ct-get-pattern-macro-group [* | name] -> [*, group | found]\n"
        "    Read pattern macro group label. Missing -> nil 0.\n"
        "\n"
        "  ct-set-pattern-macro-scope [*, name | scope] -> [* | ok]\n"
        "    Assign activation scope label to pattern macro.\n"
        "\n"
        "  ct-get-pattern-macro-scope [* | name] -> [*, scope | found]\n"
        "    Read pattern macro scope label. Missing -> nil 0.\n"
        "\n"
        "  ct-set-pattern-group-active [*, group | flag] -> [* | ok]\n"
        "    Enable/disable entire pattern group for rewrite matching.\n"
        "\n"
        "  ct-set-pattern-scope-active [*, scope | flag] -> [* | ok]\n"
        "    Enable/disable entire pattern scope for rewrite matching.\n"
        "\n"
        "  ct-list-active-pattern-groups [*] -> [* | groups]\n"
        "    List currently enabled pattern groups.\n"
        "\n"
        "  ct-list-active-pattern-scopes [*] -> [* | scopes]\n"
        "    List currently enabled pattern scopes.\n"
        "\n"
        "  ct-set-pattern-macro-clause-guard [*, name, idx | guard-or-nil] -> [* | ok]\n"
        "    Set/clear guard for clause index (0-based) in named pattern macro.\n"
        "    Returns 0 for invalid macro/index.\n"
        "\n"
        "  ct-detect-pattern-conflicts [*] -> [* | conflicts]\n"
        "    Detect conflicting pattern clauses across all macros.\n"
        "\n"
        "  ct-detect-pattern-conflicts-named [* | name] -> [* | conflicts]\n"
        "    Detect conflicts only for named pattern macro.\n"
        "\n"
        "  ct-get-rewrite-specificity [*, stage | name] -> [*, score | found]\n"
        "    Query computed rewrite specificity score for stage/rule name.\n"
        "    Missing rule returns 0 0.\n"
        "\n"
        "  ct-set-rewrite-pipeline [*, stage, name | pipeline] -> [* | ok]\n"
        "    Assign named rewrite rule to pipeline bucket within stage.\n"
        "\n"
        "  ct-get-rewrite-pipeline [*, stage | name] -> [*, pipeline | found]\n"
        "    Read pipeline assignment for rewrite rule. Missing -> nil 0.\n"
        "\n"
        "  ct-set-rewrite-pipeline-active [*, stage, pipeline | flag] -> [*]\n"
        "    Enable/disable pipeline for a stage.\n"
        "\n"
        "  ct-list-rewrite-active-pipelines [* | stage] -> [* | pipelines]\n"
        "    List active pipeline names for stage.\n"
        "\n"
        "  ct-rebuild-rewrite-index [* | stage] -> [* | size]\n"
        "    Rebuild rewrite stage index caches and return indexed-rule count\n"
        "    (keyed + wildcard).\n"
        "\n"
        "  ct-get-rewrite-index-stats [* | stage] -> [* | map]\n"
        "    Return index statistics map: {stage, keys, keyed_rules,\n"
        "    wildcard_rules}.\n"
        "\n"
        "  ct-rewrite-txn-begin [*] -> [* | depth]\n"
        "    Begin rewrite transaction scope and return nesting depth.\n"
        "\n"
        "  ct-rewrite-txn-commit [*] -> [* | ok]\n"
        "    Commit top rewrite transaction snapshot.\n"
        "\n"
        "  ct-rewrite-txn-rollback [*] -> [* | ok]\n"
        "    Roll back top rewrite transaction snapshot.\n"
        "\n"
        "  ct-export-rewrite-pack [*] -> [* | pack]\n"
        "    Export all rewrite configuration/rules into portable pack map.\n"
        "\n"
        "  ct-import-rewrite-pack [* | pack] -> [* | count]\n"
        "    Import rewrite pack entries (merge mode) and return imported count.\n"
        "\n"
        "  ct-import-rewrite-pack-replace [* | pack] -> [* | count]\n"
        "    Import rewrite pack entries with replacement semantics and\n"
        "    return imported count.\n"
        "\n"
        "  ct-get-rewrite-provenance [*, stage | name] -> [*, map | found]\n"
        "    Read provenance metadata for rewrite rule. Missing -> nil 0.\n"
        "\n"
        "  ct-rewrite-dry-run [*, stage, tokens | max-steps] -> [*, tokens | patches]\n"
        "    Simulate rewrite pass without mutating parser state. Returns\n"
        "    final tokens and patch trace list.\n"
        "\n"
        "  ct-rewrite-generate-fixture [*, stage, tokens | max-steps] -> [* | fixture-map]\n"
        "    Build fixture map {stage,input,output,patches} from dry-run result.\n"
        "\n"
        "  ct-set-rewrite-saturation [* | strategy] -> [*]\n"
        "    Set saturation strategy: first, specificity, or single-pass.\n"
        "\n"
        "  ct-get-rewrite-saturation [*] -> [* | strategy]\n"
        "    Return active rewrite saturation strategy.\n"
        "\n"
        "  ct-set-rewrite-max-steps [* | n] -> [*]\n"
        "    Set max rewrite-step budget (n >= 1).\n"
        "\n"
        "  ct-get-rewrite-max-steps [*] -> [* | n]\n"
        "    Return configured rewrite-step budget.\n"
        "\n"
        "  ct-set-rewrite-loop-detection [* | flag] -> [*]\n"
        "    Enable/disable loop-detection checks in rewrite engine.\n"
        "\n"
        "  ct-get-rewrite-loop-detection [*] -> [* | flag]\n"
        "    Return loop-detection enabled flag (1/0).\n"
        "\n"
        "  ct-get-rewrite-loop-reports [*] -> [* | reports]\n"
        "    Return captured rewrite loop report entries.\n"
        "\n"
        "  ct-clear-rewrite-loop-reports [*] -> [* | count]\n"
        "    Clear loop report list and return removed count.\n"
        "\n"
        "  ct-set-rewrite-trace [* | flag] -> [*]\n"
        "    Enable/disable rewrite trace logging.\n"
        "\n"
        "  ct-get-rewrite-trace [*] -> [* | flag]\n"
        "    Return rewrite trace enabled flag (1/0).\n"
        "\n"
        "  ct-get-rewrite-trace-log [*] -> [* | list]\n"
        "    Return rewrite trace event log list.\n"
        "\n"
        "  ct-clear-rewrite-trace-log [*] -> [* | count]\n"
        "    Clear rewrite trace log and return removed count.\n"
        "\n"
        "  ct-get-rewrite-profile [*] -> [* | map]\n"
        "    Return rewrite profile snapshot map (counters/timings).\n"
        "\n"
        "  ct-clear-rewrite-profile [*] -> [*]\n"
        "    Reset rewrite profiler counters and timings.\n"
        "\n"
        "  ct-rewrite-compatibility-matrix [* | stage] -> [* | matrix]\n"
        "    Build stage compatibility matrix for rewrite rules/constraints.\n"
        "\n"
        "  ct-rewrite-scope-push [*] -> [* | depth]\n"
        "    Push current rewrite activation snapshot (pipelines, groups,\n"
        "    scopes) onto CT scope stack.\n"
        "\n"
        "  ct-rewrite-scope-pop [*] -> [* | flag]\n"
        "    Restore top rewrite activation snapshot. Returns 0 when\n"
        "    scope stack is empty.\n"
        "\n"
        "  ct-rewrite-run-on-list [*, stage | tokens] -> [*, tokens | patches]\n"
        "    Run rewrite engine on explicit token-lexeme list using current\n"
        "    stage activation/saturation settings, without mutating parser\n"
        "    token stream.\n"
        "\n"
        "    Template helper introspection: see ct-get-macro-template-mode,\n"
        "    ct-get-macro-template-version, and\n"
        "    ct-get-macro-template-program-size above.\n"
        "\n"
        "  ct-list-words        [*] -> [* | list]\n"
        "    Return sorted list of all currently registered dictionary words.\n"
        "\n"
        "  ct-unregister-word    [* | name] -> [* | flag]\n"
        "    Remove named word/macro definition from dictionary.\n"
        "    Returns 1 when removal occurred, else 0.\n"
        "\n"
        "  ct-word-exists?       [* | name] -> [* | flag]\n"
        "    Return 1 when dictionary contains named word, else 0.\n"
        "\n"
        "  ct-get-word-body      [* | name] -> [* | body-or-nil]\n"
        "    Introspect high-level word body as op maps, or macro body as\n"
        "    expansion token list. Missing/unavailable returns nil.\n"
        "\n"
        "  ct-get-word-asm       [* | name] -> [* | asm-or-nil]\n"
        "    Return raw asm body string for :asm-defined words.\n"
        "    Non-asm/missing words return nil.\n"
        "\n"
        "  emit-definition     [*, name | body-list] -> [*]\n"
        "    Emit a word definition dynamically. `name` is a token or\n"
        "    string; `body-list` is a list of tokens/strings that form\n"
        "    the word body. Injects the equivalent of\n"
        "      word <name> <body...> end\n"
        "    into the parser's token stream.\n"
        "\n"
        "  ── Control-frame helpers (for custom control structures)\n"
        "\n"
        "  ct-control-frame-new [* | type] -> [* | frame]\n"
        "    Create a control frame map with a `type` field.\n"
        "\n"
        "  ct-control-get       [*, frame | key] -> [* | value]\n"
        "    Read key from a control frame map.\n"
        "\n"
        "  ct-control-set       [*, frame, key | value] -> [* | frame]\n"
        "    Write key/value into a control frame map.\n"
        "\n"
        "  ct-control-push      [* | frame] -> [*]\n"
        "    Push a frame onto the parser control stack.\n"
        "\n"
        "  ct-control-pop       [*] -> [* | frame]\n"
        "    Pop and return the top parser control frame.\n"
        "\n"
        "  ct-control-peek      [*] -> [* | frame] || [* | nil]\n"
        "    Return the top parser control frame without popping.\n"
        "\n"
        "  ct-control-depth     [*] -> [* | n]\n"
        "    Return parser control-stack depth.\n"
        "\n"
        "  ct-control-add-close-op [*, frame, op | data] -> [* | frame]\n"
        "    Append a close operation descriptor to frame.close_ops.\n"
        "\n"
        "  ct-new-label         [* | prefix] -> [* | label]\n"
        "    Allocate a fresh internal label with the given prefix.\n"
        "\n"
        "  ct-emit-op           [*, op | data] -> [*]\n"
        "    Emit an internal op node directly into the current body.\n"
        "\n"
        "  ct-last-token-line   [*] -> [* | line]\n"
        "    Return line number of the last parser token (or 0).\n"
        "\n"
        "  ct-register-block-opener [* | name] -> [*]\n"
        "    Mark a word name as a block opener for `with` nesting.\n"
        "\n"
        "  ct-unregister-block-opener [* | name] -> [*]\n"
        "    Remove a word name from block opener registration.\n"
        "\n"
        "  ct-register-control-override [* | name] -> [*]\n"
        "    Register a control word override so parser can delegate\n"
        "    built-in control handling to custom compile-time words.\n"
        "\n"
        "  ct-unregister-control-override [* | name] -> [*]\n"
        "    Remove a control word override registration.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 7  LEXER OBJECTS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Lexer objects provide structured token parsing with custom\n"
        "  separator characters. They wrap the main parser and let\n"
        "  macros build mini-DSLs that tokenize differently.\n"
        "\n"
        "  lexer-new           [* | separators] -> [* | lexer]\n"
        "    Create a lexer object with the given separator characters\n"
        "    (e.g. \",;\" to split on commas and semicolons).\n"
        "\n"
        "  lexer-pop           [* | lexer] -> [*, lexer | token]\n"
        "    Consume and return the next token from the lexer.\n"
        "\n"
        "  lexer-peek          [* | lexer] -> [*, lexer | token]\n"
        "    Return the next token without consuming it.\n"
        "\n"
        "  lexer-expect        [*, lexer | str] -> [*, lexer | token]\n"
        "    Consume the next token and assert its lexeme matches str.\n"
        "    Raises a parse error on mismatch.\n"
        "\n"
        "  lexer-collect-brace [* | lexer] -> [*, lexer | list]\n"
        "    Collect all tokens between matching { } braces into a\n"
        "    list. The opening { must be the next token.\n"
        "\n"
        "  lexer-push-back     [* | lexer] -> [* | lexer]\n"
        "    Push the most recently consumed token back onto the\n"
        "    lexer's stream.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 8  ASSEMBLY OUTPUT CONTROL\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  These words let compile-time code modify the generated\n"
        "  assembly: the prelude (code inside _start), custom\n"
        "  .data lines, and the BSS section (uninitialized data).\n"
        "\n"
        "  prelude-clear       [*] -> [*]\n"
        "    Discard the entire custom prelude.\n"
        "\n"
        "  prelude-append      [* | line] -> [*]\n"
        "    Append a line of assembly to the custom prelude.\n"
        "\n"
        "  prelude-set         [* | list-of-strings] -> [*]\n"
        "    Replace the custom prelude with the given list of\n"
        "    assembly lines.\n"
        "\n"
        "  data-clear          [*] -> [*]\n"
        "    Discard all custom .data section additions.\n"
        "\n"
        "  data-append         [* | line] -> [*]\n"
        "    Append a line to the custom .data section additions.\n"
        "\n"
        "  data-set            [* | list-of-strings] -> [*]\n"
        "    Replace custom .data additions with the given list\n"
        "    of lines.\n"
        "\n"
        "  bss-clear           [*] -> [*]\n"
        "    Discard all custom BSS declarations.\n"
        "\n"
        "  bss-append          [* | line] -> [*]\n"
        "    Append a line to the custom BSS section.\n"
        "\n"
        "  bss-set             [* | list-of-strings] -> [*]\n"
        "    Replace the custom BSS with the given list of lines.\n"
        "\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 9  EXPRESSION HELPER\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  shunt               [* | token-list] -> [* | postfix-list]\n"
        "    Shunting-yard algorithm. Takes a list of infix token\n"
        "    strings (numbers, identifiers, +, -, *, /, %, parentheses)\n"
        "    and returns the equivalent postfix (RPN) token list.\n"
        "    Useful for building expression-based DSLs.\n"
        "\n"
        "      [\"3\" \"+\" \"4\" \"*\" \"2\"] shunt\n"
        "      # => [\"3\" \"4\" \"2\" \"*\" \"+\"]\n"
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
        "  static_assert       [* | condition] -> [*]\n"
        "    If condition is zero or false, abort compilation with a\n"
        "    static assertion failure (includes source location).\n"
        "\n"
        "  parse-error         [* | message] -> (aborts)\n"
        "    Abort compilation with the given error message.\n"
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
        "  § 13  MACRO & TEXT MACRO DEFINITION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        + _L2_MACRO_CT_BLOCK
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
        "\n"
        "    You can also mark words as compile-time:\n"
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
        "    - Dead code elimination (automatic)\n"
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
        "    The L2 compiler is a single-pass, single-file Python\n"
        "    program (~13K lines) with these major stages:\n"
        "\n"
        "    1. READER/TOKENIZER\n"
        "       Splits source into whitespace-delimited tokens.\n"
        "       Tracks line, column, and byte offsets per token.\n"
        "       Line comments (starting with #) are discarded by the\n"
        "       tokenizer and do not become runtime operations.\n"
        "\n"
        "    2. IMPORT RESOLUTION\n"
        "       'import' and 'cimport' directives are resolved\n"
        "       recursively. Each file is loaded once. Imports are\n"
        "       concatenated into a single token stream with\n"
        "       FileSpan markers for error reporting.\n"
        "\n"
        "    3. PARSER\n"
        "       Walks the token stream and builds an IR Module of\n"
        "       Op lists (one per word definition). Key features:\n"
        "       - Word/asm/py/extern definitions -> dictionary\n"
        "       - Control flow (if/else/end, while/do/end, for)\n"
        "         compiled to label-based jumps\n"
        "       - Macro expansion (text macros with $N params)\n"
        "       - Token hooks for user-extensible syntax\n"
        "       - Compile-time VM execution of immediate words\n"
        "\n"
        "    4. ASSEMBLER / CODE GENERATOR\n"
        "       Converts the Op IR into NASM x86-64 assembly.\n"
        "       Handles calling conventions, extern C FFI with\n"
        "       full System V ABI support (register classification,\n"
        "       struct passing, SSE arguments).\n"
        "\n"
        "    5. NASM + LINKER\n"
        "       The assembly is assembled by NASM into an object\n"
        "       file, then linked (via ld or ld.lld) into the final\n"
        "       binary.\n"
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
        "    - Macro expansion depth: macros can expand macros,\n"
        "      but there's a limit (default 256, configurable via\n"
        "      --macro-expansion-limit).\n"
        "      Use --macro-preview to trace each expansion and --preview\n"
        "      to print the final transformed source.\n"
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

    def _parse_sig_counts(effect: str) -> Tuple[int, int]:
        """Parse stack effect to (n_args, n_returns).

        Counts all named items (excluding ``*``) on each side of ``->``.
        Items before ``|`` are deeper stack elements; items after are top.
        Both count as args/returns.

        Handles dual-return with ``||``:
          ``[* | x] -> [* | y] || [*, x | z]``
        Takes the first branch for counting.
        Returns (-1, -1) for unparseable effects.
        """
        if not effect or "->" not in effect:
            return (-1, -1)
        # Split off dual-return: take first branch
        main = effect.split("||")[0].strip()
        parts = main.split("->", 1)
        if len(parts) != 2:
            return (-1, -1)
        lhs, rhs = parts[0].strip(), parts[1].strip()

        def _count_items(side: str) -> int:
            s = side.strip()
            if s.startswith("["):
                s = s[1:]
            if s.endswith("]"):
                s = s[:-1]
            s = s.strip()
            if not s:
                return 0
            # Flatten both sides of pipe and count all non-* items
            all_items = s.replace("|", ",")
            return len([x.strip() for x in all_items.split(",")
                        if x.strip() and x.strip() != "*"])

        return (_count_items(lhs), _count_items(rhs))

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
                    n_args, n_rets = _parse_sig_counts(e.stack_effect)
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
    entries = collect_docs(
        roots,
        include_undocumented=include_undocumented,
        include_private=include_private,
        include_tests=include_tests,
        include_macros=include_macros,
        include_libs=include_libs,
    )

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

    def _href(tab_id: str, q_text: str, sel_text: str = "") -> str:
        params: Dict[str, str] = {"tab": tab_id}
        if q_text:
            params["q"] = q_text
        if sel_text:
            params["sel"] = sel_text
        return "/?" + urlencode(params)

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

    active_label = next((label for tab_id, label in tabs if tab_id == active_tab), active_tab)
    return (
        "<!doctype html>"
        "<html lang=\"en\"><head><meta charset=\"utf-8\" />"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />"
        "<title>L2 Docs Explorer</title>"
        "<style>"
        "body{margin:0;padding:16px;font-family:Segoe UI,Arial,sans-serif;background:#f5f8fc;color:#0f2238;}"
        ".shell{max-width:1320px;margin:0 auto;background:#fff;border:1px solid #cbd8ea;border-radius:12px;overflow:hidden;}"
        ".head{padding:14px 16px;border-bottom:1px solid #d6e1ef;background:#f8fbff;}"
        ".tabs{display:flex;flex-wrap:wrap;gap:8px;padding:10px 12px;border-bottom:1px solid #d6e1ef;background:#fbfdff;}"
        ".tab{display:inline-block;padding:6px 10px;border:1px solid #c2d2e6;border-radius:999px;text-decoration:none;color:#204364;background:#fff;}"
        ".tab.active{background:#dbeaff;border-color:#1d5aa8;font-weight:600;}"
        ".controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 12px;border-bottom:1px solid #d6e1ef;background:#f8fbff;}"
        ".field{min-width:260px;flex:1;border:1px solid #bdcde1;border-radius:8px;padding:8px 10px;}"
        ".btn{border:1px solid #b9cde4;background:#eef5ff;color:#1f4265;border-radius:8px;padding:8px 10px;text-decoration:none;display:inline-block;}"
        ".content{padding:12px;}"
        ".hint{margin:0 0 8px;color:#4a6382;font-size:13px;}"
        ".split{display:grid;grid-template-columns:minmax(340px,40%) 1fr;gap:12px;height:min(74vh,820px);min-height:520px;}"
        ".listcol{min-width:0;overflow:auto;overscroll-behavior:contain;border:1px solid #d7e2ef;border-radius:10px;background:#fff;padding:8px;}"
        ".detailcol{min-width:0;overflow:auto;overscroll-behavior:contain;border:1px solid #d7e2ef;border-radius:10px;background:#fbfdff;padding:10px;}"
        ".rows{list-style:none;margin:0;padding:0;}"
        ".rows li{padding:0;border-bottom:1px solid #ecf2fb;border-left:3px solid transparent;}"
        ".rows li:last-child{border-bottom:0;}"
        ".rows li:hover{background:#f2f8ff;}"
        ".rows li.item-active{background:#eef6ff;border-left-color:#1d5aa8;}"
        ".entrybox{display:block;padding:10px 10px;text-decoration:none;color:inherit;}"
        ".entry{font-family:Consolas,monospace;font-weight:600;color:#1a3f64;text-decoration:none;}"
        ".entrybox:hover .entry{text-decoration:underline;}"
        ".name{font-family:Consolas,monospace;font-weight:600;}"
        ".meta{font-size:12px;color:#345676;margin-top:4px;white-space:pre-wrap;word-break:break-word;}"
        ".desc{font-size:13px;color:#35516f;margin-top:4px;white-space:pre-wrap;word-break:break-word;}"
        ".kv{font-size:13px;color:#2f4c69;margin-top:4px;white-space:pre-wrap;word-break:break-word;}"
        ".label{margin:10px 0 6px;font-size:12px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:#566f8d;}"
        ".mini{margin:6px 0 0;padding:8px;border:1px solid #d7e2ef;border-radius:8px;background:#f7fbff;white-space:pre-wrap;word-break:break-word;}"
        ".full{margin:0;padding:10px;border:1px solid #d7e2ef;border-radius:8px;background:#f7fbff;white-space:pre-wrap;word-break:break-word;overflow:auto;}"
        "@media (max-width: 980px){.split{grid-template-columns:1fr;height:auto;min-height:0;}.listcol,.detailcol{overflow:visible;overscroll-behavior:auto;}}"
        "</style></head><body>"
        "<div class=\"shell\">"
        "<div class=\"head\"><h1 style=\"margin:0;font-size:20px;\">L2 Docs Explorer</h1>"
        "<div style=\"margin-top:6px;color:#486381;font-size:13px;\">"
        f"Showing: {_esc(active_label)} | {_esc(meta_text)}"
        "</div></div>"
        f"<nav class=\"tabs\">{''.join(nav_links)}</nav>"
        "<form class=\"controls\" method=\"get\" action=\"/\">"
        f"<input type=\"hidden\" name=\"tab\" value=\"{_esc(active_tab)}\" />"
        f"{selected_input}"
        f"<input class=\"field\" type=\"search\" name=\"q\" value=\"{_esc(q)}\" placeholder=\"Search current tab...\" />"
        "<button class=\"btn\" type=\"submit\">Search</button>"
        f"<a class=\"btn\" href=\"{_esc(_href(active_tab, '', ''))}\">Clear</a>"
        "<a class=\"btn\" href=\"/api/docs\">Raw API</a>"
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
                static_html = _build_docs_web_static_html(
                    payload,
                    tab=static_tab,
                    query=static_query,
                    selected=static_sel,
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
    for prefix in ("ct-", "list-", "map-", "string-", "token-", "lexer-", "prelude-", "data-", "bss-"):
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
        or word_name
        in {
            "next-token",
            "peek-token",
            "inject-tokens",
            "inject-lexemes",
            "add-token",
            "add-token-chars",
            "ct-current-token",
        }
    ):
        target = _name_phrase(word_name)
        return f"Token-stream helper for {target}; reads, inspects, or injects parser tokens during compile time."

    if word_name.startswith("lexer-"):
        target = _target_phrase(word_name, "lexer-")
        return f"Lexer-object helper for {target}; supports mini-DSL parsing with custom separators and lookahead."

    if word_name.startswith("prelude-") or word_name.startswith("data-") or word_name.startswith("bss-"):
        target = _name_phrase(word_name)
        return f"Assembly emission helper for {target}; updates generated prelude/data/BSS sections before final code emission."

    if word_name in {"nil", "nil?"}:
        return "Nil sentinel helper used for optional values and missing-key semantics in CT list/map workflows."

    if stack_effect:
        return (
            f"{category} operation used during compile-time execution with stack contract {stack_effect}; "
            "designed for explicit and deterministic parser/VM state changes."
        )
    return None


def category_for_word(word_name: str) -> str:
    if word_name in _CT_TEMPLATE_DIRECTIVE_NAMES:
        return "Template"
    if word_name in ("nil", "nil?"):
        return "Nil"
    if word_name.startswith("list-"):
        return "List"
    if word_name.startswith("map-"):
        return "Map"
    if word_name.startswith("string-") or word_name in ("int>string", "string>number", "identifier?"):
        return "String"
    if word_name.startswith("token-") or word_name.startswith("ct-parser-") or word_name in (
        "next-token",
        "peek-token",
        "inject-tokens",
        "inject-lexemes",
        "add-token",
        "add-token-chars",
        "emit-definition",
        "ct-current-token",
    ):
        return "Token"
    if word_name.startswith("lexer-"):
        return "Lexer"
    if word_name.startswith("ct-control-") or word_name in (
        "ct-new-label",
        "ct-emit-op",
        "ct-last-token-line",
        "ct-register-block-opener",
        "ct-unregister-block-opener",
        "ct-register-control-override",
        "ct-unregister-control-override",
    ):
        return "Control"
    if word_name.startswith("prelude-") or word_name.startswith("data-") or word_name.startswith("bss-"):
        return "Assembly"
    if word_name.startswith("ct-capture-") or word_name == "ct-gensym":
        return "Capture"
    if (
        word_name.startswith("ct-set-ct-call-")
        or word_name.startswith("ct-get-ct-call-")
        or word_name.startswith("ct-clear-ct-call-")
        or word_name.startswith("ct-ctrand-")
    ):
        return "CT-Call"
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
        word_name.startswith("ct-set-rewrite-")
        or word_name.startswith("ct-get-rewrite-")
        or word_name.startswith("ct-list-rewrite-")
        or word_name.startswith("ct-rewrite-")
        or word_name.startswith("ct-import-rewrite-")
        or word_name.startswith("ct-export-rewrite-")
    ):
        return "Rewrite"
    if (
        word_name.startswith("ct-register-")
        or word_name.startswith("ct-unregister-")
        or word_name.startswith("ct-word-is-")
        or word_name.startswith("ct-get-macro-")
        or word_name.startswith("ct-set-macro-")
        or word_name.startswith("ct-clone-macro")
        or word_name.startswith("ct-rename-macro")
    ):
        return "Macro"
    if word_name in ("set-token-hook", "clear-token-hook", "compile-time", "compile-only", "immediate", "runtime", "runtime-only", "use-l2-ct", "CT"):
        return "Hook"
    if word_name in ("static_assert", "parse-error"):
        return "Assert"
    if word_name == "eval":
        return "Eval"
    if word_name == "i":
        return "Loop"
    return "Meta"


_CT_TEMPLATE_DIRECTIVE_METADATA: Tuple[Dict[str, Any], ...] = (
    {"name": "ct-call", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-if", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-when", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-unless", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-for", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-each", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-let", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-fn", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-switch", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-case", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-default", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-match", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-fold", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-break", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-continue", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-include", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-import", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-comment", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-endcomment", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-strict", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-permissive", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-version", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-error", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-warning", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-note", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "emit-list", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-emit-list", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "emit-block", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
    {"name": "ct-emit-block", "compile_only": True, "immediate": False, "runtime_only": False, "has_runtime_intrinsic": False},
)

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
    "CT": "Pushes 1 when running inside compile-time execution and 0 in emitted runtime code, so words can branch explicitly on execution mode.",
    "ct-capture-shape": "Returns normalized shape tag for capture values: none, single, tokens, multi, or scalar.",
    "ct-capture-assert-shape": "Asserts expected shape against ct-capture-shape and raises parse error when they differ.",
    "ct-capture-count": "Returns element/group count for list and variadic group-list captures; nil returns 0 and scalar input is rejected.",
    "ct-capture-slice": "Slices list/group-list capture values using Python-style [start:end] semantics.",
    "ct-capture-map": "Applies a named transform over capture tokens (upper, lower, strip, int, int-normalize).",
    "ct-capture-filter": "Filters capture tokens by built-in predicates or rewrite-constraint predicate names.",
    "ct-capture-separate": "Flattens capture values into a token list and inserts separator token between variadic groups.",
    "ct-parser-eof?": "Returns 1 when parser cursor is at or beyond end-of-stream and 0 otherwise, enabling safe loop termination in custom parser macros.",
    "ct-parser-peek": "Performs non-consuming lookahead by offset from current parser cursor; out-of-range reads return nil instead of raising.",
    "ct-parser-set-pos": "Repositions parser cursor to an absolute token index, returns previous position, and keeps ct-current-token state consistent.",
    "ct-parser-checkpoint": "Captures parser cursor snapshot map (pos, last_token, remaining) for speculative parsing and branch rollback.",
    "ct-parser-restore": "Restores parser cursor from checkpoint map or raw position with bounds/type validation and returns success flag.",
    "ct-parser-tail": "Returns a copy of remaining token objects from current parser cursor for diagnostics, lookahead, or external analyzers.",
    "ct-parser-session-begin": "Pushes full parser snapshot (tokens, pos, last_token) onto nested session stack and returns session depth.",
    "ct-parser-session-commit": "Commits top parser session by discarding snapshot while preserving current parser state; returns 0 when stack is empty.",
    "ct-parser-session-rollback": "Rolls back parser to top session snapshot and pops it, restoring tokens/cursor atomically for safe speculative parsing.",
    "ct-parser-collect-until": "Consumes tokens until delimiter lexeme appears, returning collected token list and found flag while consuming delimiter itself.",
    "ct-parser-collect-balanced": "Consumes nested balanced open/close regions, returning collected tokens and found flag when outer close delimiter is reached.",
    "ct-parser-mark": "Stores current parser cursor under a named mark and returns previous mark position plus had-previous flag.",
    "ct-parser-diff": "Builds parser position diff map (start/end/delta/count/lexemes/forward) between marks, checkpoints, or explicit indices.",
    "ct-parser-expected": "Asserts next token lexeme is in expected set (string/token/list forms), raising detailed parse errors on mismatch.",
    "token-clone": "Clones token object preserving lexeme and source coordinates, useful when edits require immutable-style token workflows.",
    "token-with-lexeme": "Clones token metadata but substitutes lexeme text, preserving source anchors while retokenizing generated constructs.",
    "token-shift-column": "Clones token and shifts column/start/end coordinates by delta, helping synthetic tokens keep stable relative source spans.",
    "ct-rewrite-scope-push": "Pushes active rewrite pipeline/group/scope activation snapshot so temporary DSL rewrites can be scoped safely.",
    "ct-rewrite-scope-pop": "Restores rewrite activation snapshot from scope stack, preventing temporary rewrite settings from leaking globally.",
    "ct-rewrite-run-on-list": "Runs rewrite engine against explicit lexeme list using current stage settings and returns rewritten output plus patch trace.",
    "ct-lang-create": "Creates or updates a named language-extension pack record and returns whether it was newly created.",
    "ct-lang-exists?": "Returns 1 when a named language-extension pack exists.",
    "ct-lang-list": "Returns sorted list of registered language-extension pack names.",
    "ct-lang-activate": "Activates a named language-extension pack.",
    "ct-lang-deactivate": "Deactivates a named language-extension pack.",
    "ct-lang-active?": "Returns 1 when a named language-extension pack is active.",
    "ct-lang-active-list": "Returns sorted list of currently active language-extension pack names.",
    "ct-lang-meta-set": "Stores metadata map for a language-extension pack; nil clears metadata.",
    "ct-lang-meta-get": "Returns metadata map and found flag for a language-extension pack.",
    "ct-lang-set-auto-validate": "Enables or disables automatic validator runs for a language-extension pack.",
    "ct-lang-get-auto-validate": "Returns auto-validate flag and found flag for a language-extension pack.",
    "ct-lang-add-validator": "Registers a validator word name on a language-extension pack and returns validator count.",
    "ct-lang-run-validators": "Runs validators for one language-extension pack and returns executed count.",
    "ct-lang-run-active-validators": "Runs validators across active language-extension packs and returns total executed count.",
    "ct-lang-set-token-hook": "Sets or clears token hook ownership for a language-extension pack.",
    "ct-lang-add-reader-rewrite-named": "Registers a named reader-stage rewrite under a language-extension pack.",
    "ct-lang-add-grammar-rewrite-named": "Registers a named grammar-stage rewrite under a language-extension pack.",
    "ct-lang-register-text-macro-signature": "Registers a text macro signature and tracks ownership in a language-extension pack.",
    "ct-lang-register-pattern-macro": "Registers a pattern macro and tracks ownership in a language-extension pack.",
    "ct-lang-status": "Returns status map for a language-extension pack (active state, resources, counts, metadata).",
    "ct-lang-remove": "Removes a language-extension pack and cleans owned rewrites/macros/hooks.",
}


def _compose_overview(
    word_name: str,
    raw_description: str,
    *,
    stack_effect: str,
    category: str,
) -> str:
    override = _OVERVIEW_OVERRIDES.get(word_name)
    if override is not None:
        return override
    semantic = _semantic_overview_from_name(word_name, category, stack_effect)
    text = raw_description.strip()
    if text:
        rendered = text if text.endswith(".") else text + "."
        if len(rendered) < 40:
            if semantic:
                rendered += f" {semantic}"
            elif stack_effect:
                rendered += (
                    f" In typical {category.lower()} workflows it is used with the "
                    f"stack contract {stack_effect}."
                )
            else:
                rendered += (
                    f" It is commonly used in {category.lower()} compile-time flows "
                    "for explicit parser/VM state transitions."
                )
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
    "CT": "CT puti cr   # prints 1 at compile time and 0 at runtime",
    "compile-time": "word build_only 1 puti cr end  compile-time build_only",
    "immediate": "word stamp 99 puti cr end immediate",
    "compile-only": "word helper 42 end compile-only",
    "runtime": "word runtime_word 7 end runtime",
    "runtime-only": "word runtime_word 7 end runtime-only",
    "inline": "inline word inc 1 + end",
    "use-l2-ct": "word dup2 dup dup end use-l2-ct",
    "set-token-hook": '"trace_hook" set-token-hook',
    "clear-token-hook": '"trace_hook" set-token-hook clear-token-hook',
    "add-token": '"@" add-token',
    "add-token-chars": '",;" add-token-chars',

    "ct-call": "macro bridge 1 ct-call emit-from-capture ;",
    "ct-if": "macro maybe (x) ct-if has x then $x else 0 end ;",
    "ct-when": "macro only_when (x) ct-when has x $x end ;",
    "ct-unless": "macro only_unless (x) ct-unless has x 0 end ;",
    "ct-for": "macro sum_tail (head *tail) $head ct-for item in tail do $item + end ;",
    "ct-each": "macro emit_each (*xs) ct-each x in xs do $x end ;",
    "ct-let": "macro bind_tmp 1 ct-let t $0 do $t $t + end ;",
    "ct-fn": "macro local_helper 0 ct-fn twice do $0 $0 end ct-call twice ;",
    "ct-switch": "macro choose (x) ct-switch $x do ct-case 0 do 0 end ct-default do 1 end end ;",
    "ct-case": "macro classify (x) ct-switch $x do ct-case 1 do 11 end ct-default do 0 end end ;",
    "ct-default": "macro classify_default (x) ct-switch $x do ct-default do 99 end end ;",
    "ct-match": "macro map_bool (x) ct-match $x do ct-case 0 then 0 ct-default then 1 end ;",
    "ct-fold": "macro fold_plus (*xs) ct-fold acc item in xs with 0 do $acc $item + end ;",
    "ct-break": "macro first_only (*xs) ct-for x in xs do $x ct-break end ;",
    "ct-continue": "macro skip_zero (*xs) ct-for x in xs do ct-if $x 0 == then ct-continue end $x end ;",
    "ct-include": 'macro use_shared 0 ct-include "shared_template.sl" ;',
    "ct-import": 'macro use_pack 0 ct-import "macro_pack.sl" ;',
    "ct-comment": "macro clean 0 ct-comment ignored tokens ct-endcomment 1 ;",
    "ct-endcomment": "macro clean_end 0 ct-comment nested ct-endcomment 1 ;",
    "ct-strict": "macro strict_mode 0 ct-strict 1 ;",
    "ct-permissive": "macro permissive_mode 0 ct-permissive 1 ;",
    "ct-version": 'macro ver 0 ct-version "1.0" 1 ;',
    "ct-error": 'macro fail_now 0 ct-error "bad expansion" ;',
    "ct-warning": 'macro warn_now 0 ct-warning "check this" ;',
    "ct-note": 'macro note_now 0 ct-note "info" ;',
    "emit-list": "macro emit_tail (*xs) emit-list xs ;",
    "ct-emit-list": "macro emit_tail_ct (*xs) ct-emit-list xs ;",
    "emit-block": "macro block_out 0 emit-block do 1 2 + end ;",
    "ct-emit-block": "macro block_out_ct 0 ct-emit-block do 1 2 + end ;",

    "ct-control-frame-new": '"if" ct-control-frame-new ct-control-push',
    "ct-control-add-close-op": '"if" ct-control-frame-new dup "false" "if_false" ct-new-label ct-control-set dup "false" ct-control-get "label" swap ct-control-add-close-op',
    "ct-register-block-opener": "next-token token-lexeme ct-register-block-opener",
    "ct-unregister-block-opener": '"if" ct-unregister-block-opener',
    "ct-register-control-override": "next-token token-lexeme ct-register-control-override",
    "ct-unregister-control-override": '"if" ct-unregister-control-override',
    "ct-detect-pattern-conflicts": "ct-detect-pattern-conflicts list-length",
    "ct-set-macro-expansion-limit": "256 ct-set-macro-expansion-limit ct-get-macro-expansion-limit 256 == static_assert",
    "ct-set-rewrite-saturation": '"specificity" ct-set-rewrite-saturation ct-get-rewrite-saturation "specificity" string= static_assert',
    "ct-set-rewrite-max-steps": "64 ct-set-rewrite-max-steps ct-get-rewrite-max-steps 64 == static_assert",
    "ct-set-rewrite-loop-detection": "1 ct-set-rewrite-loop-detection ct-get-rewrite-loop-detection static_assert",
    "ct-set-rewrite-trace": "1 ct-set-rewrite-trace ct-get-rewrite-trace static_assert",
    "string-starts-with?": '"alpha:beta:gamma" "alpha:" string-starts-with? static_assert',

    "ct-capture-shape": 'list-new "x" list-append ct-capture-shape',
    "ct-capture-assert-shape": 'list-new "x" list-append "tokens" ct-capture-assert-shape',
    "ct-capture-count": 'list-new "a" list-append "b" list-append ct-capture-count',
    "ct-capture-slice": 'list-new "a" list-append "b" list-append "c" list-append 0 2 ct-capture-slice',
    "ct-capture-map": 'list-new "aa" list-append "bb" list-append "upper" ct-capture-map',
    "ct-capture-filter": 'list-new "x" list-append "1" list-append "identifier" ct-capture-filter',
    "ct-capture-separate": 'list-new list-new "x" list-append list-append list-new "y" list-append list-append "|" ct-capture-separate',
    "ct-capture-coerce-tokens": 'list-new "x" list-append ct-capture-coerce-tokens',
    "ct-capture-coerce-string": 'list-new "x" list-append "y" list-append ct-capture-coerce-string',
    "ct-capture-coerce-number": '"123" ct-capture-coerce-number',
    "ct-capture-args": 'ctx ct-capture-args map-length 0 >= static_assert',
    "ct-capture-locals": 'ctx ct-capture-locals map-length 0 >= static_assert',
    "ct-capture-globals": 'ctx ct-capture-globals map-length 0 >= static_assert',
    "ct-capture-get": 'ctx "x" ct-capture-get swap drop drop',
    "ct-capture-has?": 'ctx "x" ct-capture-has? static_assert',
    "ct-capture-origin": 'ctx ct-capture-origin',
    "ct-capture-lifetime": 'ctx ct-capture-lifetime',
    "ct-capture-lifetime-live?": 'ctx ct-capture-lifetime-live? static_assert',
    "ct-capture-lifetime-assert": 'ctx ct-capture-lifetime-assert',
    "ct-capture-lint": 'ctx ct-capture-lint list-length 0 >= static_assert',
    "ct-capture-schema-validate": 'ctx ct-capture-schema-validate static_assert',
    "ct-capture-tainted?": 'ctx "x" ct-capture-tainted?',
    "ct-capture-global-set": '"scratch" list-new "x" list-append ct-capture-global-set',
    "ct-capture-global-get": '"scratch" ct-capture-global-get',
    "ct-capture-global-delete": '"scratch" ct-capture-global-delete',
    "ct-capture-global-clear": 'ct-capture-global-clear drop',
    "ct-capture-schema-put": '"my_macro" "x" "single" "int" 1 ct-capture-schema-put',
    "ct-capture-schema-get": '"my_macro" ct-capture-schema-get',
    "ct-capture-taint-set": '"my_macro" "x" 1 ct-capture-taint-set',
    "ct-capture-taint-get": '"my_macro" "x" ct-capture-taint-get',
    "ct-capture-serialize": 'list-new "x" list-append ct-capture-serialize',
    "ct-capture-deserialize": '"[\"x\",\"y\"]" ct-capture-deserialize',
    "ct-capture-compress": '"payload text" ct-capture-compress',
    "ct-capture-decompress": '"eJyrVkrLz1eyUkpKLFKqBQAQKQXK" ct-capture-decompress',
    "ct-capture-hash": 'list-new "x" list-append ct-capture-hash',
    "ct-capture-diff": 'list-new "x" list-append list-new "y" list-append ct-capture-diff',
    "ct-capture-replay-log": 'ct-capture-replay-log list-length',
    "ct-capture-replay-clear": 'ct-capture-replay-clear drop',
    "ct-macro-doc-get": '"my_macro" ct-macro-doc-get',
    "ct-macro-doc-set": '"my_macro" "One-line macro documentation" ct-macro-doc-set',
    "ct-macro-attrs-get": '"my_macro" ct-macro-attrs-get',
    "ct-macro-attrs-set": '"my_macro" map-new ct-macro-attrs-set',
    "ct-register-text-macro": '"m" 2 list-new ct-register-text-macro',
    "ct-register-text-macro-signature": '"m" list-new list-new ct-register-text-macro-signature',
    "ct-register-pattern-macro": '"pm_rule" list-new ct-register-pattern-macro',
    "ct-gensym": '"tmp" ct-gensym',
    "ct-rewrite-dry-run": '"grammar" list-new 64 ct-rewrite-dry-run',
    "ct-ctrand-seed": "12345 ct-ctrand-seed 10 ct-ctrand-int drop",
    "ct-ctrand-int": "100 ct-ctrand-int",
    "ct-ctrand-range": "1 10 ct-ctrand-range",
    "ct-import-rewrite-pack": "ct-export-rewrite-pack ct-import-rewrite-pack drop",
    "ct-import-rewrite-pack-replace": "ct-export-rewrite-pack ct-import-rewrite-pack-replace drop",
    "list-new": "list-new 1 list-append 2 list-append",
    "map-new": 'map-new "key" 42 map-set',
    "next-token": "next-token token-lexeme",
    "peek-token": "peek-token token-lexeme",
    "token-with-lexeme": 'peek-token "renamed" token-with-lexeme token-lexeme',
    "token-clone": "peek-token token-clone token-lexeme",
    "token-shift-column": "peek-token 2 token-shift-column token-column",
    "inject-lexemes": 'list-new "tmp_word" list-append ct-current-token inject-lexemes',
    "ct-parser-eof?": "ct-parser-eof?",
    "ct-parser-peek": "0 ct-parser-peek token-lexeme",
    "ct-parser-set-pos": "ct-parser-pos ct-parser-set-pos drop",
    "ct-parser-checkpoint": "ct-parser-checkpoint map-length",
    "ct-parser-restore": "ct-parser-checkpoint ct-parser-restore static_assert",
    "ct-parser-tail": "ct-parser-tail list-length",
    "ct-parser-session-begin": "ct-parser-session-begin 0 > static_assert",
    "ct-parser-session-commit": "ct-parser-session-begin drop ct-parser-session-commit static_assert",
    "ct-parser-session-rollback": "ct-parser-session-begin drop ct-parser-session-rollback static_assert",
    "ct-parser-collect-until": '"end" ct-parser-collect-until swap drop',
    "ct-parser-collect-balanced": '"(" ")" ct-parser-collect-balanced swap drop',
    "ct-parser-mark": '"m0" ct-parser-mark swap drop drop',
    "ct-parser-diff": '"m0" ct-parser-mark drop drop "m0" ct-parser-pos ct-parser-diff map-length',
    "ct-parser-expected": 'list-new "word" list-append ct-parser-expected token-lexeme',
    "ct-rewrite-scope-push": "ct-rewrite-scope-push 0 > static_assert",
    "ct-rewrite-scope-pop": "ct-rewrite-scope-push drop ct-rewrite-scope-pop static_assert",
    "ct-rewrite-run-on-list": '"grammar" list-new "kw" list-append ct-rewrite-run-on-list swap drop',
    "ct-lang-create": '"demo.lang" ct-lang-create drop',
    "ct-lang-exists?": '"demo.lang" ct-lang-exists? static_assert',
    "ct-lang-list": "ct-lang-list list-length 0 >= static_assert",
    "ct-lang-activate": '"demo.lang" ct-lang-activate static_assert',
    "ct-lang-deactivate": '"demo.lang" ct-lang-deactivate static_assert',
    "ct-lang-active?": '"demo.lang" ct-lang-active? static_assert',
    "ct-lang-active-list": "ct-lang-active-list list-length 0 >= static_assert",
    "ct-lang-meta-set": '"demo.lang" map-new ct-lang-meta-set static_assert',
    "ct-lang-meta-get": '"demo.lang" ct-lang-meta-get swap drop',
    "ct-lang-set-auto-validate": '"demo.lang" 1 ct-lang-set-auto-validate static_assert',
    "ct-lang-get-auto-validate": '"demo.lang" ct-lang-get-auto-validate swap drop',
    "ct-lang-add-validator": '"demo.lang" "demo-validator" ct-lang-add-validator drop',
    "ct-lang-run-validators": '"demo.lang" ct-lang-run-validators drop',
    "ct-lang-run-active-validators": "ct-lang-run-active-validators drop",
    "ct-lang-set-token-hook": '"demo.lang" "demo-hook" ct-lang-set-token-hook drop',
    "ct-lang-add-reader-rewrite-named": '"demo.lang" "r" list-new list-new ct-lang-add-reader-rewrite-named drop',
    "ct-lang-add-grammar-rewrite-named": '"demo.lang" "g" list-new list-new ct-lang-add-grammar-rewrite-named drop',
    "ct-lang-register-text-macro-signature": '"demo.lang" "id" list-new "x" list-append list-new "$x" list-append ct-lang-register-text-macro-signature drop',
    "ct-lang-register-pattern-macro": '"demo.lang" "pm" list-new ct-lang-register-pattern-macro drop',
    "ct-lang-status": '"demo.lang" ct-lang-status swap drop',
    "ct-lang-remove": '"demo.lang" ct-lang-remove drop',
}


_EXAMPLE_VARIANT_OVERRIDES: Dict[str, List[str]] = {
    "add-token": ['"@" add-token'],
    "add-token-chars": ['",;" add-token-chars'],
    "ct-capture-args": ["ctx ct-capture-args map-length 0 >= static_assert"],
    "ct-capture-locals": ["ctx ct-capture-locals map-length 0 >= static_assert"],
    "ct-capture-globals": ["ctx ct-capture-globals map-length 0 >= static_assert"],
    "ct-capture-origin": ["ctx ct-capture-origin map-length 0 >= static_assert"],
    "ct-capture-get": ['ctx "x" ct-capture-get swap drop drop'],
    "ct-capture-has?": ['ctx "x" ct-capture-has? static_assert'],
    "ct-capture-lifetime": ["ctx ct-capture-lifetime 0 >= static_assert"],
    "ct-capture-lifetime-live?": ["ctx ct-capture-lifetime-live? static_assert"],
    "ct-capture-lifetime-assert": ["ctx ct-capture-lifetime-assert"],
    "ct-capture-lint": ["ctx ct-capture-lint list-length 0 >= static_assert"],
    "ct-capture-schema-validate": ["ctx ct-capture-schema-validate static_assert"],
    "ct-capture-tainted?": ['ctx "x" ct-capture-tainted?'],
    "ct-control-push": ['"if" ct-control-frame-new dup ct-control-push ct-control-pop drop'],
    "ct-control-set": ['"if" ct-control-frame-new "end" "if_end" ct-new-label ct-control-set drop'],
    "ct-emit-op": ['"jump" "L1" ct-emit-op'],
    "ct-ctrand-seed": ["12345 ct-ctrand-seed 10 ct-ctrand-int drop"],
    "ct-import-rewrite-pack": ["ct-export-rewrite-pack ct-import-rewrite-pack drop"],
    "ct-import-rewrite-pack-replace": ["ct-export-rewrite-pack ct-import-rewrite-pack-replace drop"],
}


_STACK_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_?.:+\-*/<>=!&]*")


def _stack_input_items(stack_effect: str) -> List[str]:
    effect = stack_effect.strip()
    if not effect:
        return []
    left = effect.split("->", 1)[0].strip()
    match = re.search(r"\[(.*?)\]", left)
    if match is None:
        return []
    inner = match.group(1).strip()
    if "|" in inner:
        left_part, right_part = inner.split("|", 1)
        tokens = [
            tok
            for tok in (_STACK_TOKEN_RE.findall(left_part) + _STACK_TOKEN_RE.findall(right_part))
            if tok != "*"
        ]
    else:
        tokens = [tok for tok in _STACK_TOKEN_RE.findall(inner) if tok != "*"]
    normalized = [tok.lower() for tok in tokens]
    if normalized and all(tok in {"immediate", "runtime", "compile", "time"} for tok in normalized):
        return []
    return tokens


def _stack_output_items(stack_effect: str) -> List[str]:
    effect = stack_effect.strip()
    if not effect or "->" not in effect:
        return []
    right = effect.split("->", 1)[1].strip()
    match = re.search(r"\[(.*?)\]", right)
    if match is None:
        return []
    inner = match.group(1).strip()
    if "|" in inner:
        inner = inner.split("|", 1)[1].strip()
    tokens = [tok for tok in _STACK_TOKEN_RE.findall(inner) if tok != "*"]
    normalized = [tok.lower() for tok in tokens]
    if normalized and all(tok in {"immediate", "runtime", "compile", "time"} for tok in normalized):
        return []
    return tokens


def _placeholder_for_stack_item(item: str) -> str:
    token = item.strip().lower()
    if not token:
        return "0"
    if token in {"ctx", "context"}:
        return "ctx"
    if token in {"frame"}:
        return '"if" ct-control-frame-new'
    if token in {"op"}:
        return '"jump"'
    if token in {"data"}:
        return '"L1"'
    if token in {"macro", "macro_name"}:
        return '"my_macro"'
    if token in {"shape"}:
        return '"single"'
    if token in {"type", "ctype"}:
        return '"int"'
    if token in {"required"}:
        return "1"
    if token in {"key"}:
        return '"key"'
    if token in {"value"}:
        return '"value"'
    if token in {"list", "lists", "tokens", "words", "clauses", "groups", "scopes", "pipelines", "reports", "patches", "details", "allowlist", "expansion", "body"}:
        return "list-new"
    if "list" in token or "tokens" in token or "clauses" in token or token.endswith("s") and token not in {"ms"}:
        return "list-new"
    if token in {"map", "ctx", "frame", "pack", "schema", "attrs", "metadata", "provenance", "fixture", "contract"} or "map" in token:
        return "map-new"
    if token in {"name", "word", "stage", "pipeline", "group", "scope", "policy", "mode", "strategy", "prefix", "suffix", "key", "guard", "path", "json", "blob", "text", "str", "string", "label", "id"}:
        return '"x"'
    if "name" in token or "word" in token or "stage" in token or "scope" in token or "group" in token:
        return '"x"'
    if token in {"token", "tok", "template"} or "token" in token:
        return "next-token"
    if token in {"flag", "ok", "found", "enabled", "required"}:
        return "1"
    if "flag" in token or "enabled" in token:
        return "1"
    if token in {"n", "count", "idx", "index", "start", "end", "priority", "bound", "lo", "hi", "ms", "seed", "depth", "line", "column", "timeout"}:
        return "1"
    if token in {"left", "right", "a", "b", "x", "y", "item"}:
        return "1"
    return "0"


def _alternate_placeholder_for_stack_item(item: str) -> str:
    token = item.strip().lower()
    if not token:
        return "0"
    if token in {"ctx", "context"}:
        return "ctx"
    if token in {"frame"}:
        return '"while" ct-control-frame-new'
    if token in {"op"}:
        return '"label"'
    if token in {"data"}:
        return '"L2"'
    if token in {"macro", "macro_name"}:
        return '"other_macro"'
    if token in {"shape"}:
        return '"tokens"'
    if token in {"type", "ctype"}:
        return '"identifier"'
    if token in {"required"}:
        return "0"
    if token in {"key"}:
        return '"name"'
    if token in {"value"}:
        return '"other"'
    if token in {"list", "lists", "tokens", "words", "clauses", "groups", "scopes", "pipelines", "reports", "details", "allowlist", "expansion", "body"}:
        return 'list-new "alt" list-append'
    if "list" in token or "tokens" in token or "clauses" in token or token.endswith("s") and token not in {"ms"}:
        return 'list-new "alt" list-append'
    if token in {"map", "ctx", "frame", "pack", "schema", "attrs", "metadata", "provenance", "fixture", "contract"} or "map" in token:
        return 'map-new "k" 2 map-set'
    if token in {"name", "word", "stage", "pipeline", "group", "scope", "policy", "mode", "strategy", "prefix", "suffix", "key", "guard", "path", "json", "blob", "text", "str", "string", "label", "id"}:
        return '"alt"'
    if "name" in token or "word" in token or "stage" in token or "scope" in token or "group" in token:
        return '"alt"'
    if token in {"token", "tok", "template"} or "token" in token:
        return "peek-token"
    if token in {"flag", "ok", "found", "enabled", "required"}:
        return "0"
    if "flag" in token or "enabled" in token:
        return "0"
    if token in {"n", "count", "idx", "index", "start", "end", "priority", "bound", "lo", "hi", "ms", "seed", "depth", "line", "column", "timeout"}:
        return "2"
    if token in {"left", "right", "a", "b", "x", "y", "item"}:
        return "2"
    return "1"


def _consumer_for_stack_outputs(outputs: Sequence[str]) -> str:
    lowered = [item.strip().lower() for item in outputs]
    if any(tok in {"list", "lists", "tokens", "clauses", "groups"} or "list" in tok for tok in lowered):
        return "list-length"
    if any(tok in {"map", "maps", "attrs", "schema", "metadata"} or "map" in tok for tok in lowered):
        return "map-length"
    if any(tok in {"token", "tok", "lexeme"} or "token" in tok for tok in lowered):
        return "token-lexeme"
    if any(tok in {"flag", "ok", "found", "enabled", "required", "match", "hit"} or "flag" in tok for tok in lowered):
        return 'if "ok" puts end'
    if any(tok in {"name", "word", "label", "path", "text", "string", "json", "blob"} or "str" in tok for tok in lowered):
        return "puts"
    if any(tok in {"n", "count", "idx", "index", "start", "end", "line", "column", "priority"} for tok in lowered):
        return "puti cr"
    return "drop" if lowered else ""


def _build_invocation_example(word_name: str, args: Sequence[str], consume: str) -> str:
    if args:
        base = f"{' '.join(args)} {word_name}"
    else:
        base = word_name
    return f"{base} {consume}".strip()


def _example_from_stack_signature(word_name: str, stack_effect: str) -> str:
    args = [_placeholder_for_stack_item(item) for item in _stack_input_items(stack_effect)]
    outputs = _stack_output_items(stack_effect)
    consume = _consumer_for_stack_outputs(outputs)
    return _build_invocation_example(word_name, args, consume)


def _example_from_stack_signature_alt(word_name: str, stack_effect: str) -> str:
    args = [_alternate_placeholder_for_stack_item(item) for item in _stack_input_items(stack_effect)]
    outputs = _stack_output_items(stack_effect)
    consume = _consumer_for_stack_outputs(outputs)
    return _build_invocation_example(word_name, args, consume)


def _example_has_flag_output(stack_effect: str) -> bool:
    lowered = [item.strip().lower() for item in _stack_output_items(stack_effect)]
    return any(
        tok in {"flag", "ok", "found", "enabled", "required", "match", "hit"}
        or "flag" in tok
        for tok in lowered
    )


def _sanitize_demo_name(word_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", word_name)
    safe = safe.strip("_")
    if not safe:
        safe = "word"
    if safe[0].isdigit():
        safe = f"w_{safe}"
    return safe.lower()


def _normalize_example_line(text: str) -> str:
    return " ".join(part.strip() for part in str(text).splitlines() if part.strip())


def _compile_time_wrapped_example(word_name: str, invocation: str, suffix: str) -> str:
    body = _normalize_example_line(invocation)
    if "#" in body:
        body = body.split("#", 1)[0].rstrip()
    if not body:
        body = word_name
    demo_name = f"demo_{_sanitize_demo_name(word_name)}_{suffix}"
    return f"word {demo_name} {body} end compile-time {demo_name}"


def _append_unique_example(examples: List[str], seen: Set[str], candidate: str) -> None:
    line = _normalize_example_line(candidate)
    if not line:
        return
    key = line.lower()
    if key in seen:
        return
    seen.add(key)
    examples.append(line)


def _example_for_word(word_name: str, stack_effect: str, category: str) -> str:
    override = _EXAMPLE_OVERRIDES.get(word_name)
    if override is not None:
        return override

    if word_name in {"ct-add-reader-rewrite", "ct-add-grammar-rewrite"}:
        return f'list-new "kw" list-append list-new "42" list-append {word_name}'
    if word_name in {"ct-add-reader-rewrite-named", "ct-add-grammar-rewrite-named"}:
        return f'"rule_kw" list-new "kw" list-append list-new "42" list-append {word_name}'
    if word_name in {"ct-add-reader-rewrite-priority", "ct-add-grammar-rewrite-priority"}:
        return f'10 list-new "kw" list-append list-new "42" list-append {word_name}'
    if word_name in {"ct-remove-reader-rewrite", "ct-remove-grammar-rewrite"}:
        return f'"rule_kw" {word_name}'
    if word_name in {"ct-list-reader-rewrites", "ct-list-grammar-rewrites"}:
        return f"{word_name} list-length"
    if word_name in {"ct-set-reader-rewrite-enabled", "ct-set-grammar-rewrite-enabled"}:
        return f'"rule_kw" 1 {word_name}'
    if word_name in {"ct-get-reader-rewrite-enabled", "ct-get-grammar-rewrite-enabled"}:
        return f'"rule_kw" {word_name}'
    if word_name in {"ct-set-reader-rewrite-priority", "ct-set-grammar-rewrite-priority"}:
        return f'"rule_kw" 50 {word_name}'
    if word_name in {"ct-get-reader-rewrite-priority", "ct-get-grammar-rewrite-priority"}:
        return f'"rule_kw" {word_name}'

    if word_name == "ct-set-rewrite-pipeline":
        return '"grammar" "rule_kw" "default" ct-set-rewrite-pipeline'
    if word_name == "ct-get-rewrite-pipeline":
        return '"grammar" "rule_kw" ct-get-rewrite-pipeline'
    if word_name == "ct-set-rewrite-pipeline-active":
        return '"grammar" "default" 1 ct-set-rewrite-pipeline-active'
    if word_name == "ct-list-rewrite-active-pipelines":
        return '"grammar" ct-list-rewrite-active-pipelines'
    if word_name == "ct-rebuild-rewrite-index":
        return '"grammar" ct-rebuild-rewrite-index'
    if word_name == "ct-get-rewrite-index-stats":
        return '"grammar" ct-get-rewrite-index-stats'
    contextual_noarg_examples: Dict[str, str] = {
        "ct-export-rewrite-pack": "ct-export-rewrite-pack map-length",
        "ct-rewrite-txn-begin": "ct-rewrite-txn-begin drop",
        "ct-rewrite-txn-commit": "ct-rewrite-txn-commit drop",
        "ct-rewrite-txn-rollback": "ct-rewrite-txn-rollback drop",
        "ct-get-rewrite-loop-reports": "ct-get-rewrite-loop-reports list-length",
        "ct-clear-rewrite-loop-reports": "ct-clear-rewrite-loop-reports drop",
        "ct-get-rewrite-trace-log": "ct-get-rewrite-trace-log list-length",
        "ct-clear-rewrite-trace-log": "ct-clear-rewrite-trace-log drop",
        "ct-get-rewrite-profile": "ct-get-rewrite-profile map-length",
        "ct-clear-rewrite-profile": "ct-clear-rewrite-profile ct-get-rewrite-profile drop",
        "ct-list-pattern-macros": "ct-list-pattern-macros list-length",
        "ct-list-active-pattern-groups": "ct-list-active-pattern-groups list-length",
        "ct-list-active-pattern-scopes": "ct-list-active-pattern-scopes list-length",
        "ct-list-words": "ct-list-words list-length",
        "ct-clear-ct-call-memo": "ct-clear-ct-call-memo drop",
        "ct-get-ct-call-memo-size": "ct-get-ct-call-memo-size puti cr",
        "ct-get-ct-call-side-effect-log": "ct-get-ct-call-side-effect-log list-length",
        "ct-clear-ct-call-side-effect-log": "ct-clear-ct-call-side-effect-log drop",
        "ct-get-ct-call-side-effects": "ct-get-ct-call-side-effects puti cr",
        "ct-get-ct-call-memo": "ct-get-ct-call-memo puti cr",
        "ct-get-macro-preview": "ct-get-macro-preview puti cr",
        "ct-get-macro-expansion-limit": "ct-get-macro-expansion-limit puti cr",
        "ct-get-ct-call-recursion-limit": "ct-get-ct-call-recursion-limit puti cr",
        "ct-get-ct-call-timeout-ms": "ct-get-ct-call-timeout-ms puti cr",
        "ct-get-ct-call-exception-policy": "ct-get-ct-call-exception-policy puts",
        "ct-get-ct-call-sandbox-mode": "ct-get-ct-call-sandbox-mode puts",
        "ct-get-ct-call-sandbox-allowlist": "ct-get-ct-call-sandbox-allowlist list-length",
        "ct-get-rewrite-saturation": "ct-get-rewrite-saturation puts",
        "ct-get-rewrite-max-steps": "ct-get-rewrite-max-steps puti cr",
        "ct-get-rewrite-loop-detection": "ct-get-rewrite-loop-detection puti cr",
        "ct-get-rewrite-trace": "ct-get-rewrite-trace puti cr",
        "ct-capture-global-clear": "ct-capture-global-clear drop",
        "ct-capture-replay-clear": "ct-capture-replay-clear drop",
        "ct-capture-replay-log": "ct-capture-replay-log list-length",
    }
    if word_name in contextual_noarg_examples:
        return contextual_noarg_examples[word_name]

    if word_name == "ct-import-rewrite-pack":
        return "map-new ct-import-rewrite-pack"
    if word_name == "ct-import-rewrite-pack-replace":
        return "map-new ct-import-rewrite-pack-replace"
    if word_name == "ct-get-rewrite-provenance":
        return '"grammar" "rule_kw" ct-get-rewrite-provenance'
    if word_name == "ct-get-rewrite-specificity":
        return '"grammar" "rule_kw" ct-get-rewrite-specificity'
    if word_name == "ct-rewrite-generate-fixture":
        return '"grammar" list-new "kw" list-append 64 ct-rewrite-generate-fixture'
    if word_name == "ct-rewrite-compatibility-matrix":
        return '"grammar" ct-rewrite-compatibility-matrix'

    if word_name == "ct-set-pattern-macro-enabled":
        return '"pm_rule" 1 ct-set-pattern-macro-enabled'
    if word_name == "ct-get-pattern-macro-enabled":
        return '"pm_rule" ct-get-pattern-macro-enabled'
    if word_name == "ct-set-pattern-macro-priority":
        return '"pm_rule" 10 ct-set-pattern-macro-priority'
    if word_name == "ct-get-pattern-macro-priority":
        return '"pm_rule" ct-get-pattern-macro-priority'
    if word_name == "ct-get-pattern-macro-clauses":
        return '"pm_rule" ct-get-pattern-macro-clauses'
    if word_name == "ct-get-pattern-macro-clause-details":
        return '"pm_rule" ct-get-pattern-macro-clause-details'
    if word_name == "ct-set-pattern-macro-group":
        return '"pm_rule" "arith" ct-set-pattern-macro-group'
    if word_name == "ct-get-pattern-macro-group":
        return '"pm_rule" ct-get-pattern-macro-group'
    if word_name == "ct-set-pattern-macro-scope":
        return '"pm_rule" "scope_a" ct-set-pattern-macro-scope'
    if word_name == "ct-get-pattern-macro-scope":
        return '"pm_rule" ct-get-pattern-macro-scope'
    if word_name == "ct-set-pattern-group-active":
        return '"arith" 1 ct-set-pattern-group-active'
    if word_name == "ct-set-pattern-scope-active":
        return '"scope_a" 1 ct-set-pattern-scope-active'
    if word_name == "ct-set-pattern-macro-clause-guard":
        return '"pm_rule" 0 "guard_nonzero" ct-set-pattern-macro-clause-guard'
    if word_name == "ct-detect-pattern-conflicts-named":
        return '"pm_rule" ct-detect-pattern-conflicts-named'

    if word_name == "ct-set-ct-call-contract":
        return '"my_ct_word" map-new ct-set-ct-call-contract'
    if word_name == "ct-get-ct-call-contract":
        return '"my_ct_word" ct-get-ct-call-contract'
    if word_name == "ct-set-ct-call-exception-policy":
        return '"warn" ct-set-ct-call-exception-policy'
    if word_name == "ct-set-ct-call-sandbox-mode":
        return '"allowlist" ct-set-ct-call-sandbox-mode'
    if word_name == "ct-set-ct-call-sandbox-allowlist":
        return 'list-new "safe_word" list-append ct-set-ct-call-sandbox-allowlist'
    if word_name == "ct-set-ct-call-memo":
        return '1 ct-set-ct-call-memo'
    if word_name == "ct-set-ct-call-side-effects":
        return '1 ct-set-ct-call-side-effects'
    if word_name == "ct-set-ct-call-recursion-limit":
        return '8 ct-set-ct-call-recursion-limit'
    if word_name == "ct-set-ct-call-timeout-ms":
        return '50 ct-set-ct-call-timeout-ms'

    if word_name.startswith("lexer-"):
        if word_name == "lexer-new":
            return '",;" lexer-new'
        if word_name == "lexer-pop":
            return '",;" lexer-new lexer-pop'
        if word_name == "lexer-peek":
            return '",;" lexer-new lexer-peek'
        if word_name == "lexer-expect":
            return '",;" lexer-new "{" lexer-expect'
        if word_name == "lexer-collect-brace":
            return '",;" lexer-new lexer-collect-brace'
        if word_name == "lexer-push-back":
            return '",;" lexer-new lexer-push-back'

    if word_name.startswith("prelude-"):
        if word_name == "prelude-clear":
            return 'prelude-clear "mov rax, 60" prelude-append'
        if word_name == "prelude-append":
            return '"mov rax, 60" prelude-append'
        if word_name == "prelude-set":
            return 'list-new "mov rax, 60" list-append prelude-set'
    if word_name.startswith("data-"):
        if word_name == "data-clear":
            return 'data-clear "scratch_dq: dq 42" data-append'
        if word_name == "data-append":
            return '"scratch_dq: dq 42" data-append'
        if word_name == "data-set":
            return 'list-new "scratch_dq: dq 42" list-append data-set'
    if word_name.startswith("bss-"):
        if word_name == "bss-clear":
            return 'bss-clear "scratch: resb 64" bss-append'
        if word_name == "bss-append":
            return '"scratch: resb 64" bss-append'
        if word_name == "bss-set":
            return 'list-new "scratch: resb 64" list-append bss-set'

    if word_name in {"set-token-hook", "clear-token-hook", "emit-definition"}:
        if word_name == "emit-definition":
            return '"dyn_word" list-new "1" list-append emit-definition'
        if word_name == "set-token-hook":
            return '"hook_word" set-token-hook'
        return '"hook_word" set-token-hook clear-token-hook'

    if word_name in {"shunt", "eval", "parse-error", "static_assert", "here", "i"}:
        if word_name == "shunt":
            return 'list-new "3" list-append "+" list-append "4" list-append shunt'
        if word_name == "eval":
            return '"1 2 +" eval'
        if word_name == "parse-error":
            return '"build failed" parse-error'
        if word_name == "static_assert":
            return "1 static_assert"
        if word_name == "here":
            return "here"
        return "10 for i puti cr end"

    # Final fallback still shows a concrete call with stack-shaped dummy values.
    del category
    return _example_from_stack_signature(word_name, stack_effect)


def _examples_for_word(word_name: str, stack_effect: str, category: str) -> List[str]:
    primary = _example_for_word(word_name, stack_effect, category)
    signature = _example_from_stack_signature(word_name, stack_effect)
    signature_alt = _example_from_stack_signature_alt(word_name, stack_effect)
    stack_inputs = _stack_input_items(stack_effect)
    raw_signature = _build_invocation_example(
        word_name,
        [_placeholder_for_stack_item(item) for item in stack_inputs],
        "",
    )
    raw_signature_alt = _build_invocation_example(
        word_name,
        [_alternate_placeholder_for_stack_item(item) for item in stack_inputs],
        "",
    )

    primary_norm = _normalize_example_line(primary).lower()
    signature_norm = _normalize_example_line(signature).lower()
    signature_alt_norm = _normalize_example_line(signature_alt).lower()
    raw_signature_norm = _normalize_example_line(raw_signature).lower()
    raw_signature_alt_norm = _normalize_example_line(raw_signature_alt).lower()
    has_curated_primary = bool(primary_norm) and primary_norm != signature_norm

    out: List[str] = []
    seen: Set[str] = set()
    _append_unique_example(out, seen, primary)

    for extra in _EXAMPLE_VARIANT_OVERRIDES.get(word_name, []):
        _append_unique_example(out, seen, extra)

    if has_curated_primary:
        if len(stack_inputs) == 0:
            _append_unique_example(out, seen, signature)
            if signature_alt_norm != signature_norm:
                _append_unique_example(out, seen, signature_alt)
        if len(stack_inputs) >= 1:
            _append_unique_example(out, seen, raw_signature)
            if raw_signature_alt_norm != raw_signature_norm:
                _append_unique_example(out, seen, raw_signature_alt)
        if len(stack_inputs) >= 2:
            _append_unique_example(out, seen, signature)
            if signature_alt_norm != signature_norm:
                _append_unique_example(out, seen, signature_alt)
        _append_unique_example(out, seen, _compile_time_wrapped_example(word_name, primary, "ct"))
    else:
        if raw_signature_alt_norm != raw_signature_norm:
            _append_unique_example(out, seen, raw_signature_alt)
        if signature_alt_norm != signature_norm:
            _append_unique_example(out, seen, signature_alt)
        _append_unique_example(out, seen, _compile_time_wrapped_example(word_name, signature, "ct"))

    if _example_has_flag_output(stack_effect):
        existing_blob = " ".join(out).lower()
        if "static_assert" not in existing_blob:
            assert_example = _build_invocation_example(
                word_name,
                [_placeholder_for_stack_item(item) for item in stack_inputs],
                "static_assert",
            )
            _append_unique_example(out, seen, assert_example)

            assert_example_alt = _build_invocation_example(
                word_name,
                [_alternate_placeholder_for_stack_item(item) for item in stack_inputs],
                "static_assert",
            )
            if _normalize_example_line(assert_example_alt).lower() != _normalize_example_line(assert_example).lower():
                _append_unique_example(out, seen, assert_example_alt)

    if not out:
        out.append(word_name)
    return out


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
        example_lines = _examples_for_word(name, stack_effect, category)
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
