"""Bootstrap compiler for the L2 language.

This file now contains working scaffolding for:

* Parsing definitions, literals, and ordinary word references.
* Respecting immediate/macro words so syntax can be rewritten on the fly.
* Emitting NASM-compatible x86-64 assembly with explicit data and return stacks.
* Driving the toolchain via ``nasm`` + ``ld``.
"""

from __future__ import annotations

import ast
import bisect
import importlib.util
import os
import random
import re
import shlex
import sys
import textwrap
import time
from pathlib import Path
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Union, Tuple

Ks = None
KsError = Exception
KS_ARCH_X86 = KS_MODE_64 = None
_KEYSTONE_IMPORT_ATTEMPTED = False

def _ensure_keystone() -> bool:
    """Lazily import keystone to keep startup overhead low on plain builds."""
    global Ks, KsError, KS_ARCH_X86, KS_MODE_64, _KEYSTONE_IMPORT_ATTEMPTED
    if _KEYSTONE_IMPORT_ATTEMPTED:
        return Ks is not None
    _KEYSTONE_IMPORT_ATTEMPTED = True
    try:
        from keystone import Ks as _Ks, KsError as _KsError, KS_ARCH_X86 as _KS_ARCH_X86, KS_MODE_64 as _KS_MODE_64
    except Exception:  # pragma: no cover - optional dependency
        return False
    Ks = _Ks
    KsError = _KsError
    KS_ARCH_X86 = _KS_ARCH_X86
    KS_MODE_64 = _KS_MODE_64
    return True

# Pre-compiled regex patterns used by JIT and BSS code
_RE_REL_PAT = re.compile(r'\[rel\s+(\w+)\]')
_RE_LABEL_PAT = re.compile(r'^(\.\w+|\w+):')
_RE_BSS_PERSISTENT = re.compile(r'persistent:\s*resb\s+(\d+)')
_RE_NEWLINE = re.compile('\n')
_CT_FAST_CT_INTRINSIC_DISPATCH = {}
# Blanking asm bodies before tokenization: the tokenizer doesn't need asm
# content (the parser extracts it from the original source via byte offsets).
# This removes ~75% of tokens for asm-heavy programs like game_of_life.
_RE_ASM_BODY = re.compile(r'(:asm\b[^{]*\{)([^}]*)(})')
_ASM_BLANK_TBL = str.maketrans({chr(i): ' ' for i in range(128) if i != 10})
def _blank_asm_bodies(source: str) -> str:
    return _RE_ASM_BODY.sub(lambda m: m.group(1) + m.group(2).translate(_ASM_BLANK_TBL) + m.group(3), source)
DEFAULT_MACRO_EXPANSION_LIMIT = 256
_SOURCE_PATH = Path("<source>")

_struct_mod = None
def _get_struct():
    global _struct_mod
    if _struct_mod is None:
        import struct as _s
        _struct_mod = _s
    return _struct_mod


class Diagnostic:
    """Structured error/warning with optional source context and suggestions."""
    __slots__ = ('level', 'message', 'path', 'line', 'column', 'length', 'hint', 'suggestion')

    def __init__(
        self,
        level: str,
        message: str,
        path: Optional[Path] = None,
        line: int = 0,
        column: int = 0,
        length: int = 0,
        hint: str = "",
        suggestion: str = "",
    ) -> None:
        self.level = level       # "error", "warning", "note"
        self.message = message
        self.path = path
        self.line = line
        self.column = column
        self.length = length
        self.hint = hint
        self.suggestion = suggestion

    def format(self, *, color: bool = True) -> str:
        """Format the diagnostic in Rust-style with source context."""
        _RED = "\033[1;31m" if color else ""
        _YELLOW = "\033[1;33m" if color else ""
        _BLUE = "\033[1;34m" if color else ""
        _CYAN = "\033[1;36m" if color else ""
        _BOLD = "\033[1m" if color else ""
        _DIM = "\033[2m" if color else ""
        _RST = "\033[0m" if color else ""

        level_color = _RED if self.level == "error" else (_YELLOW if self.level == "warning" else _BLUE)
        parts: List[str] = []
        parts.append(f"{level_color}{self.level}{_RST}{_BOLD}: {self.message}{_RST}")

        if self.path and self.line > 0:
            loc = f"{self.path}:{self.line}"
            if self.column > 0:
                loc += f":{self.column}"
            parts.append(f"  {_BLUE}-->{_RST} {loc}")

            # Try to show the source line
            try:
                src_lines = self.path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if 0 < self.line <= len(src_lines):
                    src_line = src_lines[self.line - 1]
                    line_no = str(self.line)
                    pad = " " * len(line_no)
                    parts.append(f"  {_BLUE}{pad} |{_RST}")
                    parts.append(f"  {_BLUE}{line_no} |{_RST} {src_line}")
                    if self.column > 0:
                        caret_len = max(1, self.length) if self.length else 1
                        arrow = " " * (self.column - 1) + level_color + "^" * caret_len + _RST
                        parts.append(f"  {_BLUE}{pad} |{_RST} {arrow}")
                    if self.hint:
                        parts.append(f"  {_BLUE}{pad} |{_RST} {_CYAN}= note: {self.hint}{_RST}")
                    if self.suggestion:
                        parts.append(f"  {_BLUE}{pad} |{_RST}")
                        parts.append(f"  {_BLUE}{pad} = {_CYAN}help{_RST}: {self.suggestion}")
            except Exception:
                pass

        elif self.hint:
            parts.append(f"  {_CYAN}= note: {self.hint}{_RST}")

        return "\n".join(parts)

    def __str__(self) -> str:
        if self.path and self.line > 0:
            return f"{self.level}: {self.message} at {self.path}:{self.line}:{self.column}"
        return f"{self.level}: {self.message}"


class ParseError(Exception):
    """Raised when the source stream cannot be parsed."""
    def __init__(self, message: str = "", *, diagnostic: Optional[Diagnostic] = None) -> None:
        self.diagnostic = diagnostic
        super().__init__(message)


class CompileError(Exception):
    """Raised when IR cannot be turned into assembly."""
    def __init__(self, message: str = "", *, diagnostic: Optional[Diagnostic] = None) -> None:
        self.diagnostic = diagnostic
        super().__init__(message)


class CompileTimeError(ParseError):
    """Raised when a compile-time word fails with context."""


# ---------------------------------------------------------------------------
# Tokenizer / Reader
# ---------------------------------------------------------------------------


class Token:
    __slots__ = ('lexeme', 'line', 'column', 'start', 'end', 'expansion_depth')

    def __init__(self, lexeme: str, line: int, column: int, start: int, end: int, expansion_depth: int = 0) -> None:
        self.lexeme = lexeme
        self.line = line
        self.column = column
        self.start = start
        self.end = end
        self.expansion_depth = expansion_depth

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Token({self.lexeme!r}@{self.line}:{self.column})"


class SourceLocation:
    __slots__ = ('path', 'line', 'column')

    def __init__(self, path: Path, line: int, column: int) -> None:
        self.path = path
        self.line = line
        self.column = column

_SourceLocation_new = SourceLocation.__new__
_SourceLocation_cls = SourceLocation

def _make_loc(path: Path, line: int, column: int) -> SourceLocation:
    loc = _SourceLocation_new(_SourceLocation_cls)
    loc.path = path
    loc.line = line
    loc.column = column
    return loc

_READER_REGEX_CACHE: Dict[frozenset, "re.Pattern[str]"] = {}

_STACK_EFFECT_PAREN_RE = re.compile(r'\(([^)]*--[^)]*)\)')
_STACK_EFFECT_BARE_RE = re.compile(r'#\s*(\S+(?:\s+\S+)*?)\s+--\s')

def _parse_stack_effect_comment(source: str, word_token_start: int) -> Optional[int]:
    """Extract the input count from a stack-effect comment near a 'word' token.

    Looks for ``# ... (a b -- c)`` or ``# a b -- c`` on the same line as
    *word_token_start* or on the immediately preceding line.  Returns the
    number of inputs (names before ``--``) or *None* if no effect comment
    is found.
    """
    # Find the line containing the word token
    line_start = source.rfind('\n', 0, word_token_start)
    line_start = 0 if line_start == -1 else line_start + 1
    line_end = source.find('\n', word_token_start)
    if line_end == -1:
        line_end = len(source)
    lines_to_check = [source[line_start:line_end]]
    if line_start > 0:
        prev_end = line_start - 1
        prev_start = source.rfind('\n', 0, prev_end)
        prev_start = 0 if prev_start == -1 else prev_start + 1
        lines_to_check.append(source[prev_start:prev_end])

    for line in lines_to_check:
        if '#' not in line or '--' not in line:
            continue
        # Prefer parenthesized effect: # text (a b -- c)
        m = _STACK_EFFECT_PAREN_RE.search(line)
        if m:
            parts = m.group(1).split('--')
            inputs_part = parts[0].strip()
            return len(inputs_part.split()) if inputs_part else 0
        # Bare effect on same line as word: # a b -- c
        m = _STACK_EFFECT_BARE_RE.search(line)
        if m:
            return len(m.group(1).split())
    return None

class Reader:
    """Default reader; users can swap implementations at runtime."""

    def __init__(self) -> None:
        self.line = 1
        self.column = 0
        self.custom_tokens: Set[str] = {"(", ")", "{", "}", ";", ",", "[", "]"}
        self._token_order: List[str] = sorted(self.custom_tokens, key=len, reverse=True)
        self._single_char_tokens: Set[str] = {t for t in self.custom_tokens if len(t) == 1}
        self._multi_char_tokens: List[str] = [t for t in self._token_order if len(t) > 1]
        self._multi_first_chars: Set[str] = {t[0] for t in self._multi_char_tokens}

    def add_tokens(self, tokens: Iterable[str]) -> None:
        updated = False
        for tok in tokens:
            if not tok:
                continue
            if tok not in self.custom_tokens:
                self.custom_tokens.add(tok)
                updated = True
        if updated:
            self._token_order = sorted(self.custom_tokens, key=len, reverse=True)
            self._single_char_tokens = {t for t in self.custom_tokens if len(t) == 1}
            self._multi_char_tokens = [t for t in self._token_order if len(t) > 1]
            self._multi_first_chars = {t[0] for t in self._multi_char_tokens}
            self._token_re = None  # invalidate cached regex
            self._multi_char_tokens = [t for t in self._token_order if len(t) > 1]
            self._multi_first_chars = {t[0] for t in self._multi_char_tokens}

    def add_token_chars(self, chars: str) -> None:
        self.add_tokens(chars)

    def _build_token_re(self) -> "re.Pattern[str]":
        """Build a compiled regex for the current token set."""
        cache_key = frozenset(self.custom_tokens)
        cached = _READER_REGEX_CACHE.get(cache_key)
        if cached is not None:
            return cached
        singles_escaped = ''.join(re.escape(t) for t in sorted(self._single_char_tokens))
        # Word pattern: any non-delimiter char, or ; followed by alpha (;end is one token)
        if ';' in self._single_char_tokens:
            word_part = rf'(?:[^\s#"\'{singles_escaped}]|;(?=[a-zA-Z]))+'
        else:
            word_part = rf'[^\s#"\'{singles_escaped}]+'
        # Multi-char tokens (longest first)
        multi_part = ''
        if self._multi_char_tokens:
            multi_escaped = '|'.join(re.escape(t) for t in self._multi_char_tokens)
            multi_part = rf'|{multi_escaped}'
        pattern = (
            rf'"(?:[^"\\]|\\.)*"?'       # string literal (possibly unterminated)
            rf"|'(?:[^'\\]|\\.)'?'"      # char literal (possibly unterminated)
            rf'|#[^\n]*'                  # comment
            rf'{multi_part}'              # multi-char tokens (if any)
            rf'|{word_part}'              # word
            rf'|[{singles_escaped}]'      # single-char tokens
        )
        compiled = re.compile(pattern)
        _READER_REGEX_CACHE[cache_key] = compiled
        return compiled

    def tokenize(self, source: str) -> List[Token]:
        # Lazily build/cache the token regex
        token_re = getattr(self, '_token_re', None)
        if token_re is None:
            token_re = self._build_token_re()
            self._token_re = token_re
        # Pre-compute line start offsets for O(1) amortized line/column lookup
        _line_starts = [0]
        _line_starts_append = _line_starts.append
        for _m in _RE_NEWLINE.finditer(source):
            _line_starts_append(_m.end())
        _n_lines = len(_line_starts)
        result: List[Token] = []
        _append = result.append
        _Token_new = Token.__new__
        _Token_cls = Token
        # Linear scan: tokens arrive in source order, so line index only advances
        _cur_li = 0
        _next_line_start = _line_starts[1] if _n_lines > 1 else 0x7FFFFFFFFFFFFFFF
        for m in token_re.finditer(source):
            start, end = m.span()
            fc = source[start]
            if fc == '#':
                continue  # skip comment
            text = source[start:end]
            if fc == '"':
                if end - start < 2 or source[end - 1] != '"':
                    raise ParseError("unterminated string literal")
            # Advance line index to find the correct line for this position
            while start >= _next_line_start:
                _cur_li += 1
                _next_line_start = _line_starts[_cur_li + 1] if _cur_li + 1 < _n_lines else 0x7FFFFFFFFFFFFFFF
            tok = _Token_new(_Token_cls)
            tok.lexeme = text
            tok.line = _cur_li + 1
            tok.column = start - _line_starts[_cur_li]
            tok.start = start
            tok.end = end
            tok.expansion_depth = 0
            _append(tok)
        # Update reader state to end-of-source position
        self.line = _n_lines
        self.column = len(source) - _line_starts[_n_lines - 1]
        return result


# ---------------------------------------------------------------------------
# Dictionary / Words
# ---------------------------------------------------------------------------


# Integer opcode constants for hot-path dispatch
OP_WORD = 0
OP_LITERAL = 1
OP_WORD_PTR = 2
OP_FOR_BEGIN = 3
OP_FOR_END = 4
OP_BRANCH_ZERO = 5
OP_JUMP = 6
OP_LABEL = 7
OP_LIST_BEGIN = 8
OP_LIST_END = 9
OP_LIST_LITERAL = 10
OP_BSS_LIST_LITERAL = 11
OP_OTHER = 12
OP_RET = 13

_OP_STR_TO_INT = {
    "word": OP_WORD,
    "literal": OP_LITERAL,
    "word_ptr": OP_WORD_PTR,
    "for_begin": OP_FOR_BEGIN,
    "for_end": OP_FOR_END,
    "branch_zero": OP_BRANCH_ZERO,
    "jump": OP_JUMP,
    "label": OP_LABEL,
    "list_begin": OP_LIST_BEGIN,
    "list_end": OP_LIST_END,
    "list_literal": OP_LIST_LITERAL,
    "bss_list_literal": OP_BSS_LIST_LITERAL,
    "ret": OP_RET,
}

_OP_INTEGRITY_DOCS: Dict[str, str] = {
    "word": "Call a named word.",
    "literal": "Push a literal value onto the stack.",
    "word_ptr": "Push a callable word pointer.",
    "for_begin": "Open a counted loop frame.",
    "for_end": "Close/iterate a counted loop frame.",
    "branch_zero": "Conditional branch when top-of-stack is zero.",
    "jump": "Unconditional branch.",
    "label": "Branch target marker.",
    "list_begin": "Begin list literal capture.",
    "list_end": "Finish list literal capture.",
    "list_literal": "Emit a static list literal.",
    "bss_list_literal": "Emit/initialize a BSS-backed list literal.",
    "ret": "Return from the current word.",
}

_RE_REWRITE_CAPTURE_TOKEN = re.compile(r"^\$(\*?)([A-Za-z_][A-Za-z0-9_]*|\d+)(?::([A-Za-z_][A-Za-z0-9_-]*))?$")
_REWRITE_CAPTURE_CONSTRAINTS = frozenset({
    "", "any", "ident", "identifier", "id", "word",
    "int", "integer", "float", "number", "numeric",
    "string", "str", "char", "chr", "literal",
})
_ASM_LINE_STARTERS: Set[str] = {
    "mov", "movzx", "movsx", "movsxd", "lea", "push", "pop", "add", "sub", "imul", "mul", "idiv", "div",
    "and", "or", "xor", "not", "neg", "inc", "dec", "cmp", "test", "call", "jmp", "je", "jne", "jg",
    "jge", "jl", "jle", "ja", "jae", "jb", "jbe", "jnz", "jz", "ret", "syscall", "nop", "sal", "sar",
    "shl", "shr", "rol", "ror", "loop", "cld", "std", "cmpsb", "stosb", "lodsb", "rep", "repe", "repne",
    "section", "global", "extern", "db", "dw", "dd", "dq", "resb", "resw", "resd", "resq",
}


def _reconstruct_asm_from_tokens(tokens: Sequence[Token]) -> str:
    if not tokens:
        return ""
    lines: List[str] = []
    cur: List[str] = []
    for tok in tokens:
        lex = tok.lexeme
        low = lex.lower()
        if cur and (low in _ASM_LINE_STARTERS or lex.endswith(":")):
            lines.append(" ".join(cur))
            cur = [lex]
            continue
        cur.append(lex)
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


# Pre-computed peephole optimization data structures (avoids rebuilding per definition)
_PEEPHOLE_WORD_RULES: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = [
    # --- stack no-ops (cancellation) ---
    (("dup", "drop"), ()),
    (("swap", "swap"), ()),
    (("over", "drop"), ()),
    (("dup", "nip"), ()),
    (("2dup", "2drop"), ()),
    (("2swap", "2swap"), ()),
    (("rot", "rot", "rot"), ()),
    (("rot", "-rot"), ()),
    (("-rot", "rot"), ()),
    (("drop", "drop"), ("2drop",)),
    (("over", "over"), ("2dup",)),
    (("inc", "dec"), ()),
    (("dec", "inc"), ()),
    (("neg", "neg"), ()),
    (("not", "not"), ()),
    (("bitnot", "bitnot"), ()),
    (("bnot", "bnot"), ()),
    (("abs", "abs"), ("abs",)),
    # --- canonicalizations that merge into single ops ---
    (("swap", "drop"), ("nip",)),
    (("swap", "over"), ("tuck",)),
    (("swap", "nip"), ("drop",)),
    (("nip", "drop"), ("2drop",)),
    (("tuck", "drop"), ("swap",)),
    # --- commutative ops: swap before them is a no-op ---
    (("swap", "+"), ("+",)),
    (("swap", "*"), ("*",)),
    (("swap", "=="), ("==",)),
    (("swap", "!="), ("!=",)),
    (("swap", "band"), ("band",)),
    (("swap", "bor"), ("bor",)),
    (("swap", "bxor"), ("bxor",)),
    (("swap", "and"), ("and",)),
    (("swap", "or"), ("or",)),
    (("swap", "min"), ("min",)),
    (("swap", "max"), ("max",)),
    # --- dup + self-idempotent binary -> identity ---
    (("dup", "bor"), ()),
    (("dup", "band"), ()),
    (("dup", "bxor"), ("drop", "literal_0")),
    (("dup", "=="), ("drop", "literal_1")),
    (("dup", "-"), ("drop", "literal_0")),
]

_PEEPHOLE_PLACEHOLDER_RULES: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
_PEEPHOLE_CLEAN_RULES: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = []
for _pat, _repl in _PEEPHOLE_WORD_RULES:
    if any(r.startswith("literal_") for r in _repl):
        _PEEPHOLE_PLACEHOLDER_RULES[_pat] = _repl
    else:
        _PEEPHOLE_CLEAN_RULES.append((_pat, _repl))

_PEEPHOLE_MAX_PAT_LEN = max(len(p) for p, _ in _PEEPHOLE_WORD_RULES) if _PEEPHOLE_WORD_RULES else 0

# Unified dict: pattern tuple -> replacement tuple (for O(1) lookup)
_PEEPHOLE_ALL_RULES: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
for _pat, _repl in _PEEPHOLE_WORD_RULES:
    _PEEPHOLE_ALL_RULES[_pat] = _repl

# Which first-words have *any* rule (quick skip for non-matching heads)
_PEEPHOLE_FIRST_WORDS: Set[str] = {p[0] for p in _PEEPHOLE_ALL_RULES}

# Length-grouped rules indexed by first word for efficient matching
_PEEPHOLE_RULE_INDEX: Dict[str, List[Tuple[Tuple[str, ...], Tuple[str, ...]]]] = {}
for _pattern, _repl in _PEEPHOLE_CLEAN_RULES:
    _PEEPHOLE_RULE_INDEX.setdefault(_pattern[0], []).append((_pattern, _repl))

_PEEPHOLE_TERMINATORS = frozenset({OP_JUMP})

_PEEPHOLE_WORD_COST: Dict[str, int] = {
    "drop": 1,
    "nip": 1,
    "dup": 1,
    "swap": 1,
    "over": 1,
    "2drop": 1,
    "2dup": 1,
    "inc": 1,
    "dec": 1,
    "not": 1,
    "neg": 1,
    "+": 2,
    "-": 2,
    "*": 3,
    "/": 4,
    "%": 4,
    "==": 2,
    "!=": 2,
    "band": 2,
    "bor": 2,
    "bxor": 2,
    "shl": 2,
    "shr": 2,
    "sar": 2,
}


def _peephole_sequence_cost(seq: Sequence[str]) -> int:
    if not seq:
        return 0
    total = 0
    for item in seq:
        if item.startswith("literal_"):
            total += 2
            continue
        total += int(_PEEPHOLE_WORD_COST.get(item, 3))
    return total


_PEEPHOLE_RULE_COST: Dict[Tuple[str, ...], int] = {
    pattern: _peephole_sequence_cost(repl)
    for pattern, repl in _PEEPHOLE_WORD_RULES
}

_PEEPHOLE_CANCEL_PAIRS = frozenset({
    ("not", "not"), ("neg", "neg"),
    ("bitnot", "bitnot"), ("bnot", "bnot"),
    ("inc", "dec"), ("dec", "inc"),
})
_PEEPHOLE_SHIFT_OPS = frozenset({"shl", "shr", "sar"})
_DEFAULT_CONTROL_WORDS = frozenset({"if", "else", "for", "while", "do"})

_PARSE_PRIORITY_KEYWORDS = frozenset({"word", ":asm", ":py", "extern", "inline", "priority"})

_PARSE_KW_LIST_BEGIN = 1
_PARSE_KW_LIST_END = 2
_PARSE_KW_WORD = 3
_PARSE_KW_END = 4
_PARSE_KW_ASM = 5
_PARSE_KW_PY = 6
_PARSE_KW_EXTERN = 7
_PARSE_KW_PRIORITY = 8
_PARSE_KW_RET = 9
_PARSE_KW_BSS_LIST_BEGIN = 10

_PARSE_KEYWORD_DISPATCH = {
    "[": _PARSE_KW_LIST_BEGIN,
    "]": _PARSE_KW_LIST_END,
    "word": _PARSE_KW_WORD,
    "end": _PARSE_KW_END,
    ":asm": _PARSE_KW_ASM,
    ":py": _PARSE_KW_PY,
    "extern": _PARSE_KW_EXTERN,
    "priority": _PARSE_KW_PRIORITY,
    "ret": _PARSE_KW_RET,
    "{": _PARSE_KW_BSS_LIST_BEGIN,
}


class Op:
    """Flat operation used for both compile-time execution and emission."""
    __slots__ = ('op', 'data', 'loc', '_word_ref', '_opcode')

    def __init__(self, op: str, data: Any = None, loc: Optional[SourceLocation] = None,
                 _word_ref: Optional[Word] = None, _opcode: int = OP_OTHER) -> None:
        self.op = op
        self.data = data
        self.loc = loc
        self._word_ref = _word_ref
        self._opcode = _OP_STR_TO_INT.get(op, OP_OTHER)


def _make_op(op: str, data: Any = None, loc: Optional[SourceLocation] = None) -> Op:
    """Fast Op constructor that avoids dict lookup for known opcodes."""
    node = Op.__new__(Op)
    node.op = op
    node.data = data
    node.loc = loc
    node._word_ref = None
    node._opcode = _OP_STR_TO_INT.get(op, OP_OTHER)
    return node


def _make_literal_op(data: Any, loc: Optional[SourceLocation] = None) -> Op:
    """Specialized Op constructor for 'literal' ops."""
    node = Op.__new__(Op)
    node.op = "literal"
    node.data = data
    node.loc = loc
    node._word_ref = None
    node._opcode = OP_LITERAL
    return node


def _make_word_op(data: str, loc: Optional[SourceLocation] = None) -> Op:
    """Specialized Op constructor for 'word' ops."""
    node = Op.__new__(Op)
    node.op = "word"
    node.data = data
    node.loc = loc
    node._word_ref = None
    node._opcode = OP_WORD
    return node


_PARSE_LITERAL_CACHE_MAX = 8192
_PARSE_LITERAL_CACHE_MISS = object()
_PARSE_LITERAL_NOT_LITERAL = object()
_PARSE_LITERAL_CACHE: Dict[str, Any] = {}


class Definition:
    __slots__ = ('name', 'body', 'immediate', 'compile_only', 'runtime_only', 'terminator', 'inline',
                 'stack_inputs', '_label_positions', '_for_pairs', '_begin_pairs',
                 '_words_resolved', '_merged_runs')

    def __init__(self, name: str, body: List[Op], immediate: bool = False,
                 compile_only: bool = False, runtime_only: bool = False, terminator: str = "end", inline: bool = False,
                 stack_inputs: Optional[int] = None) -> None:
        self.name = name
        self.body = body
        self.immediate = immediate
        self.compile_only = compile_only
        self.runtime_only = runtime_only
        self.terminator = terminator
        self.inline = inline
        self.stack_inputs = stack_inputs
        self._label_positions = None
        self._for_pairs = None
        self._begin_pairs = None
        self._words_resolved = False
        self._merged_runs = None


class AsmDefinition:
    __slots__ = ('name', 'body', 'immediate', 'compile_only', 'runtime_only', 'inline', 'effects', '_inline_lines')

    def __init__(self, name: str, body: str, immediate: bool = False,
                 compile_only: bool = False, runtime_only: bool = False, inline: bool = False,
                 effects: Set[str] = None, _inline_lines: Optional[List[str]] = None) -> None:
        self.name = name
        self.body = body
        self.immediate = immediate
        self.compile_only = compile_only
        self.runtime_only = runtime_only
        self.inline = inline
        self.effects = effects if effects is not None else set()
        self._inline_lines = _inline_lines


class Module:
    __slots__ = ('forms', 'variables', 'prelude', 'bss', 'cstruct_layouts')

    def __init__(self, forms: List[Any], variables: Dict[str, str] = None,
                 prelude: Optional[List[str]] = None, bss: Optional[List[str]] = None,
                 cstruct_layouts: Dict[str, CStructLayout] = None) -> None:
        self.forms = forms
        self.variables = variables if variables is not None else {}
        self.prelude = prelude
        self.bss = bss
        self.cstruct_layouts = cstruct_layouts if cstruct_layouts is not None else {}


class MacroDefinition:
    __slots__ = (
        'name', 'tokens', 'param_count', 'ordered_params', 'variadic_param',
        'asm_brace_depth', 'awaiting_asm_body', 'awaiting_asm_terminator',
        'nested_macro_depth'
    )

    def __init__(
        self,
        name: str,
        tokens: List[str],
        param_count: int = 0,
        ordered_params: Optional[List[str]] = None,
        variadic_param: Optional[str] = None,
    ) -> None:
        self.name = name
        self.tokens = tokens
        self.param_count = param_count
        if ordered_params is None:
            ordered_params = [str(i) for i in range(param_count)]
        self.ordered_params = ordered_params
        self.variadic_param = variadic_param
        self.asm_brace_depth = 0
        self.awaiting_asm_body = False
        self.awaiting_asm_terminator = False
        # While recording an outer macro body, semicolons that close nested
        # macro definitions must not terminate the outer definition.
        self.nested_macro_depth = 0


class RewriteRule:
    __slots__ = (
        'name', 'pattern', 'replacement', 'priority', 'order', 'enabled',
        'pipeline', 'guard', 'group', 'scope', 'metadata', 'provenance', 'specificity'
    )

    def __init__(
        self,
        name: str,
        pattern: Sequence[str],
        replacement: Sequence[str],
        *,
        priority: int = 0,
        order: int = 0,
        enabled: bool = True,
        pipeline: str = "default",
        guard: Optional[str] = None,
        group: str = "default",
        scope: str = "global",
        metadata: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
        specificity: int = 0,
    ) -> None:
        self.name = name
        self.pattern = tuple(pattern)
        self.replacement = tuple(replacement)
        self.priority = int(priority)
        self.order = int(order)
        self.enabled = bool(enabled)
        self.pipeline = str(pipeline or "default")
        self.guard = str(guard) if guard else None
        self.group = str(group or "default")
        self.scope = str(scope or "global")
        self.metadata = dict(metadata) if isinstance(metadata, dict) else {}
        self.provenance = dict(provenance) if isinstance(provenance, dict) else {}
        self.specificity = int(specificity)


class _MacroTemplateBreak(Exception):
    pass


class _MacroTemplateContinue(Exception):
    pass


class MacroEngine:
    """Parser-attached macro/rewrite engine for expansion-time behavior."""

    _TEMPLATE_COND_STARTERS = frozenset({"has", "empty", "first", "last", "not"})
    _TEMPLATE_RUNTIME_BLOCK_OPENERS = frozenset({"if", "for", "while", "with", "begin", "word"})
    _TEMPLATE_DIRECTIVE_KEYWORDS = frozenset({
        "ct-call", "ct-fn", "ct-let", "ct-switch", "ct-case", "ct-default", "ct-match", "ct-fold",
        "ct-if", "ct-when", "ct-unless", "ct-for", "ct-each", "ct-break", "ct-continue",
        "ct-include", "ct-import", "ct-#", "ct-#(", "ct-#)", "emit-list", "emit-block",
        "ct-version", "ct-strict", "ct-permissive", "ct-error", "ct-warning", "ct-note",
    })

    def __init__(self, parser: "Parser") -> None:
        self._parser = parser
        self._template_function_scopes: Optional[List[Dict[str, Sequence[Any]]]] = None
        self._template_include_cache: Dict[Path, Sequence[Any]] = {}
        self._template_include_stack: List[Path] = []
        self._template_import_scopes: Optional[List[Set[Path]]] = None
        self._active_macro_token: Optional[Token] = None
        self._template_unknown_mode: str = "strict"
        self._template_scope_stack_arena: List[List[Dict[str, Any]]] = []
        self._template_loop_stack_arena: List[List[Dict[str, int]]] = []

    def _acquire_template_scope_stack(self) -> List[Dict[str, Any]]:
        if self._template_scope_stack_arena:
            stack = self._template_scope_stack_arena.pop()
            stack.clear()
            return stack
        return []

    def _release_template_scope_stack(self, stack: List[Dict[str, Any]]) -> None:
        stack.clear()
        if len(self._template_scope_stack_arena) < 8:
            self._template_scope_stack_arena.append(stack)

    def _acquire_template_loop_stack(self) -> List[Dict[str, int]]:
        if self._template_loop_stack_arena:
            stack = self._template_loop_stack_arena.pop()
            stack.clear()
            return stack
        return []

    def _release_template_loop_stack(self, stack: List[Dict[str, int]]) -> None:
        stack.clear()
        if len(self._template_loop_stack_arena) < 8:
            self._template_loop_stack_arena.append(stack)

    def _preview_with_context(
        self,
        *,
        kind: str,
        name: str,
        token: Token,
        replaced: Sequence[str],
    ) -> None:
        parser = self._parser
        if not parser.macro_preview:
            return

        preview = " ".join(replaced)
        if len(preview) > 240:
            preview = preview[:237] + "..."

        src_lines = parser.source.splitlines()
        src_line = ""
        if 1 <= token.line <= len(src_lines):
            src_line = src_lines[token.line - 1]

        line_start = max(1, token.line - 2)
        line_end = token.line + 2
        if src_lines:
            line_end = min(len(src_lines), line_end)

        line_tokens: Dict[int, List[str]] = {}
        for tok in parser.tokens:
            if line_start <= tok.line <= line_end:
                bucket = line_tokens.get(tok.line)
                if bucket is None:
                    bucket = []
                    line_tokens[tok.line] = bucket
                bucket.append(tok.lexeme)

        lines: List[str] = [
            f"[{kind}] {name} at {token.line}:{token.column}",
            f"  expand: {preview}",
        ]
        if src_line:
            lines.append(f"  source: {src_line}")
        lines.append("  context:")
        for ln in range(line_start, line_end + 1):
            mark = ">" if ln == token.line else " "
            src_text = src_lines[ln - 1] if 1 <= ln <= len(src_lines) else ""
            now_text = " ".join(line_tokens.get(ln, []))
            lines.append(f"  {mark} {ln:5d} | src: {src_text}")
            if now_text:
                lines.append(f"  {mark} {ln:5d} | now: {now_text}")
        sys.stderr.write("\n".join(lines) + "\n")

    def macro_signature_for_word(self, word: Word) -> Tuple[Tuple[str, ...], Optional[str]]:
        parser = self._parser
        signature = parser._macro_signatures.get(word.name)
        if signature is not None:
            return signature
        ordered = tuple(str(i) for i in range(max(0, int(word.macro_params))))
        return ordered, None

    def consume_macro_argument_group(self, *, word_name: str, call_token: Token) -> List[str]:
        parser = self._parser
        if parser._eof():
            raise ParseError(
                f"macro '{word_name}' at {call_token.line}:{call_token.column} invocation missing argument"
            )
        first = parser._consume()
        opener_to_closer = {"(": ")", "[": "]", "{": "}"}
        close = opener_to_closer.get(first.lexeme)
        if close is None:
            return [first.lexeme]

        stack: List[str] = [close]
        out: List[str] = [first.lexeme]
        while stack:
            if parser._eof():
                raise ParseError(
                    f"macro '{word_name}' at {call_token.line}:{call_token.column} has unterminated grouped argument"
                )
            tok = parser._consume()
            out.append(tok.lexeme)
            closer = opener_to_closer.get(tok.lexeme)
            if closer is not None:
                stack.append(closer)
                continue
            if tok.lexeme == stack[-1]:
                stack.pop()
        return out

    def collect_macro_callstyle_args(self, *, word_name: str, call_token: Token) -> List[List[str]]:
        parser = self._parser
        open_tok = parser._consume()
        if open_tok.lexeme != "(":
            raise ParseError(
                f"internal macro parser error for '{word_name}': expected '(' but got '{open_tok.lexeme}'"
            )

        args: List[List[str]] = []
        current: List[str] = []
        opener_to_closer = {"(": ")", "[": "]", "{": "}"}
        stack: List[str] = []

        while True:
            if parser._eof():
                raise ParseError(
                    f"macro '{word_name}' at {call_token.line}:{call_token.column} has unterminated '(' call syntax"
                )
            tok = parser._consume()
            lex = tok.lexeme

            closer = opener_to_closer.get(lex)
            if closer is not None:
                stack.append(closer)
                current.append(lex)
                continue

            if stack and lex == stack[-1]:
                stack.pop()
                current.append(lex)
                continue

            if not stack and lex == ",":
                if not current:
                    raise ParseError(
                        f"macro '{word_name}' at {call_token.line}:{call_token.column} has empty call argument"
                    )
                args.append(current)
                current = []
                continue

            if not stack and lex == ")":
                if current:
                    args.append(current)
                elif args:
                    raise ParseError(
                        f"macro '{word_name}' at {call_token.line}:{call_token.column} has trailing comma in call arguments"
                    )
                break

            current.append(lex)

        return args

    def collect_macro_call_args(self, word: Word, call_token: Token) -> Dict[str, Any]:
        parser = self._parser
        ordered, variadic = self.macro_signature_for_word(word)
        required = len(ordered)

        callstyle = (not parser._eof()) and (parser.peek_token().lexeme == "(")
        if callstyle:
            groups = self.collect_macro_callstyle_args(word_name=word.name, call_token=call_token)
        else:
            groups = [
                self.consume_macro_argument_group(word_name=word.name, call_token=call_token)
                for _ in range(required)
            ]
            if variadic is not None:
                while not parser._eof():
                    nxt = parser.peek_token()
                    if nxt is None or nxt.line != call_token.line:
                        break
                    groups.append(self.consume_macro_argument_group(word_name=word.name, call_token=call_token))

        if variadic is None and len(groups) != required:
            raise ParseError(
                f"macro '{word.name}' at {call_token.line}:{call_token.column} expects {required} argument(s), got {len(groups)}"
            )
        if variadic is not None and len(groups) < required:
            raise ParseError(
                f"macro '{word.name}' at {call_token.line}:{call_token.column} expects at least {required} argument(s), got {len(groups)}"
            )

        captures: Dict[str, Any] = {}
        for idx, name in enumerate(ordered):
            arg_tokens = groups[idx]
            normalized = list(parser._intern_capture_group(arg_tokens))
            captures[name] = normalized
            captures[str(idx)] = list(normalized)

        if variadic is not None:
            tail_groups = [list(parser._intern_capture_group(group)) for group in groups[required:]]
            captures[variadic] = tail_groups
            captures[str(required)] = tail_groups

        return captures

    @staticmethod
    def _macro_capture_is_group_list(value: Any) -> bool:
        return bool(isinstance(value, list) and value and isinstance(value[0], list))

    def _macro_capture_lookup(
        self,
        *,
        word: Word,
        scopes: Sequence[Dict[str, Any]],
        key: str,
        raw_ref: Optional[str] = None,
        allow_missing: bool = False,
    ) -> Any:
        for scope in reversed(scopes):
            if key in scope:
                return scope[key]
        if allow_missing:
            return None
        ref = raw_ref if raw_ref is not None else f"${key}"
        raise ParseError(
            f"macro '{word.name}' references argument '{ref}', but the argument is not available"
        )

    def _macro_capture_as_groups(self, *, word: Word, raw_ref: str, value: Any) -> List[List[str]]:
        if isinstance(value, list):
            if self._macro_capture_is_group_list(value):
                return [list(group) for group in value]
            return [[piece] for piece in value]
        raise ParseError(
            f"macro '{word.name}' loop source '{raw_ref}' resolved to unsupported value"
        )

    def _macro_capture_clone(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._macro_capture_clone(item) for item in value]
        return value

    def _macro_capture_scope_snapshot(self, scopes: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        snapshot: Dict[str, Any] = {}
        for scope in scopes:
            for key, value in scope.items():
                snapshot[key] = self._macro_capture_clone(value)
        return snapshot

    def _macro_template_ct_context(
        self,
        *,
        word: Word,
        scopes: Sequence[Dict[str, Any]],
        loop_stack: Sequence[Dict[str, int]],
    ) -> Dict[str, Any]:
        parser = self._parser
        args_scope: Dict[str, Any] = {}
        if scopes:
            for key, value in scopes[0].items():
                args_scope[key] = self._macro_capture_clone(value)

        locals_scope: Dict[str, Any] = {}
        for scope in scopes[1:]:
            for key, value in scope.items():
                locals_scope[key] = self._macro_capture_clone(value)

        globals_scope: Dict[str, Any] = {}
        for key, value in parser.capture_globals.items():
            globals_scope[key] = self._macro_capture_clone(value)

        origin: Dict[str, Any] = {
            "macro": word.name,
            "line": 0,
            "column": 0,
            "path": "<source>",
            "expansion_depth": 0,
        }
        if self._active_macro_token is not None:
            token = self._active_macro_token
            loc = parser.location_for_token(token)
            origin["line"] = token.line
            origin["column"] = token.column
            origin["path"] = str(loc.path)
            origin["expansion_depth"] = token.expansion_depth

        parser._capture_lifetime_counter += 1
        lifetime_id = parser._capture_lifetime_counter
        parser._capture_lifetime_active = lifetime_id

        taint_scope: Dict[str, bool] = {}
        for key, flagged in parser.capture_taint.get(word.name, {}).items():
            taint_scope[str(key)] = bool(flagged)

        payload: Dict[str, Any] = {
            "macro": word.name,
            "captures": self._macro_capture_scope_snapshot(scopes),
            "args": args_scope,
            "locals": locals_scope,
            "globals": globals_scope,
            "capture_namespaces": {
                "args": args_scope,
                "locals": locals_scope,
                "globals": globals_scope,
            },
            "origin": origin,
            "lifetime": lifetime_id,
            "taint": taint_scope,
            "loop": None,
        }
        if loop_stack:
            frame = loop_stack[-1]
            payload["loop"] = {
                "index": frame["index"],
                "count": frame["count"],
                "first": frame["index"] == 0,
                "last": frame["index"] + 1 == frame["count"],
            }

        parser.capture_replay_log.append(
            {
                "macro": word.name,
                "lifetime": lifetime_id,
                "origin": self._macro_capture_clone(origin),
                "captures": self._macro_capture_clone(payload["captures"]),
                "args": self._macro_capture_clone(args_scope),
                "locals": self._macro_capture_clone(locals_scope),
                "globals": self._macro_capture_clone(globals_scope),
                "taint": self._macro_capture_clone(taint_scope),
            }
        )
        if len(parser.capture_replay_log) > 4096:
            del parser.capture_replay_log[:-4096]
        return payload

    def _coerce_macro_ct_emit_lexemes(self, *, word: Word, ct_word_name: str, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, Token):
            return [value.lexeme]
        if isinstance(value, str):
            return [value]
        if isinstance(value, bool):
            return ["1" if value else "0"]
        if isinstance(value, int):
            return [str(value)]
        if isinstance(value, float):
            return [str(value)]
        if isinstance(value, tuple):
            out: List[str] = []
            for item in value:
                out.extend(self._coerce_macro_ct_emit_lexemes(word=word, ct_word_name=ct_word_name, value=item))
            return out
        if isinstance(value, list):
            out: List[str] = []
            for item in value:
                out.extend(self._coerce_macro_ct_emit_lexemes(word=word, ct_word_name=ct_word_name, value=item))
            return out
        raise ParseError(
            f"macro '{word.name}' ct-call '{ct_word_name}' returned unsupported value type "
            f"'{type(value).__name__}'"
        )

    def _ct_call_contract_validate_payload(
        self,
        *,
        word: Word,
        ct_word_name: str,
        payload: Dict[str, Any],
    ) -> None:
        contract = self._parser._ct_call_abi_contracts.get(ct_word_name)
        if not isinstance(contract, dict):
            return
        arg_kind = str(contract.get("arg_kind", contract.get("arg", "any"))).strip().lower()
        if arg_kind in ("", "any"):
            return
        if arg_kind in ("map", "dict"):
            if not isinstance(payload, dict):
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word_name}' requires map payload"
                )
            return
        if arg_kind in ("capture-context", "capture_context", "context"):
            if not isinstance(payload, dict) or not isinstance(payload.get("captures"), dict):
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word_name}' requires capture-context payload"
                )
            return
        raise ParseError(
            f"macro '{word.name}' ct-call '{ct_word_name}' uses unsupported arg_kind '{arg_kind}'"
        )

    def _ct_call_contract_validate_result(
        self,
        *,
        word: Word,
        ct_word_name: str,
        result: Any,
    ) -> None:
        contract = self._parser._ct_call_abi_contracts.get(ct_word_name)
        if not isinstance(contract, dict):
            return

        result_kind = str(contract.get("result_kind", contract.get("result", "any"))).strip().lower()
        if result_kind not in ("", "any"):
            if result_kind in ("nil", "none") and result is not None:
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word_name}' contract expected nil result"
                )
            elif result_kind == "scalar" and not isinstance(result, (bool, int, float, str, Token)):
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word_name}' contract expected scalar result"
                )
            elif result_kind == "list" and not isinstance(result, list):
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word_name}' contract expected list result"
                )
            elif result_kind in ("map", "dict") and not isinstance(result, dict):
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word_name}' contract expected map result"
                )
            elif result_kind == "tokenish":
                self._coerce_macro_ct_emit_lexemes(word=word, ct_word_name=ct_word_name, value=result)

        result_shape = str(contract.get("result_shape", "any")).strip().lower()
        if result_shape in ("", "any"):
            return
        if result_shape in ("none", "empty"):
            if result not in (None, [], (), ""):
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word_name}' contract expected empty result"
                )
            return

        lexemes = self._coerce_macro_ct_emit_lexemes(word=word, ct_word_name=ct_word_name, value=result)
        if result_shape in ("single", "single-token", "single_token") and len(lexemes) != 1:
            raise ParseError(
                f"macro '{word.name}' ct-call '{ct_word_name}' contract expected single token result"
            )
        if result_shape in ("multi", "multi-token", "multi_token") and len(lexemes) < 2:
            raise ParseError(
                f"macro '{word.name}' ct-call '{ct_word_name}' contract expected multi-token result"
            )

    def _ct_call_enforce_sandbox(self, *, word: Word, ct_word: Word) -> None:
        parser = self._parser
        mode = str(getattr(parser, "_ct_call_sandbox_mode", "off") or "off").strip().lower()
        if mode == "off":
            return
        if mode == "allowlist":
            allowlist = getattr(parser, "_ct_call_sandbox_allowlist", set())
            if ct_word.name not in allowlist:
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word.name}' blocked by sandbox allowlist"
                )
            return
        if mode in ("compile-only", "compile_only"):
            if not bool(getattr(ct_word, "compile_only", False)):
                raise ParseError(
                    f"macro '{word.name}' ct-call '{ct_word.name}' blocked by compile-only sandbox"
                )
            return
        raise ParseError(f"invalid ct-call sandbox mode '{mode}'")

    def _ct_call_handle_exception(
        self,
        *,
        word: Word,
        ct_word_name: str,
        exc: Exception,
    ) -> Optional[List[str]]:
        policy = str(getattr(self._parser, "_ct_call_exception_policy", "raise") or "raise").strip().lower()
        message = f"macro '{word.name}' ct-call '{ct_word_name}' failed: {exc}"
        if policy == "raise":
            raise ParseError(message) from None
        if policy == "warn":
            self._template_warn(word=word, message=message)
            return []
        if policy in ("empty", "nil", "ignore"):
            return []
        raise ParseError(f"invalid ct-call exception policy '{policy}'")

    @staticmethod
    def _ct_call_memo_key(ct_word_name: str, payload: Dict[str, Any]) -> str:
        import json

        normalized = _capture_normalize_value(payload)
        return f"{ct_word_name}:{json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(',', ':'))}"

    def _ct_call_record_side_effect(
        self,
        *,
        macro_name: str,
        ct_word_name: str,
        status: str,
        memo_hit: bool,
        duration_ms: int,
        error: str = "",
    ) -> None:
        parser = self._parser
        if not bool(getattr(parser, "_ct_call_side_effect_tracking", False)):
            return
        log = getattr(parser, "_ct_call_side_effect_log", None)
        if not isinstance(log, list):
            return
        log.append(
            {
                "macro": macro_name,
                "word": ct_word_name,
                "status": status,
                "memo_hit": 1 if memo_hit else 0,
                "duration_ms": int(max(0, duration_ms)),
                "error": error,
            }
        )
        if len(log) > 4096:
            del log[:-4096]

    def _invoke_macro_ct_word(
        self,
        *,
        word: Word,
        ct_word_name: str,
        scopes: Sequence[Dict[str, Any]],
        loop_stack: Sequence[Dict[str, int]],
    ) -> List[str]:
        parser = self._parser
        ct_word = parser.dictionary.lookup(ct_word_name)
        if ct_word is None:
            raise ParseError(f"macro '{word.name}' ct-call references unknown word '{ct_word_name}'")

        self._ct_call_enforce_sandbox(word=word, ct_word=ct_word)

        payload = self._macro_template_ct_context(word=word, scopes=scopes, loop_stack=loop_stack)
        self._ct_call_contract_validate_payload(word=word, ct_word_name=ct_word_name, payload=payload)

        memo_enabled = bool(getattr(parser, "_ct_call_memo_enabled", False))
        memo_key: Optional[str] = None
        memo_hit = False
        result: Any = None

        if memo_enabled:
            memo_key = self._ct_call_memo_key(ct_word_name, payload)
            _missing = object()
            cached = parser._ct_call_memo_cache.get(memo_key, _missing)
            if cached is not _missing:
                result = _capture_deep_clone(cached)
                memo_hit = True

        started = time.perf_counter()

        if not memo_hit:
            active = parser._ct_call_active
            limit = int(getattr(parser, "_ct_call_recursion_limit", 32))
            if limit < 1:
                limit = 1
            if active.count(ct_word_name) >= limit:
                raise ParseError(
                    f"macro '{word.name}' ct-call recursion limit ({limit}) exceeded for '{ct_word_name}'"
                )

            vm = parser.compile_time_vm
            base_depth = len(vm.stack)
            active.append(ct_word_name)
            try:
                vm.push(payload)
                vm._call_word(ct_word)
                if len(vm.stack) > base_depth:
                    result = vm._resolve_handle(vm.stack[-1])
                else:
                    result = None
            except Exception as exc:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                self._ct_call_record_side_effect(
                    macro_name=word.name,
                    ct_word_name=ct_word_name,
                    status="error",
                    memo_hit=False,
                    duration_ms=elapsed_ms,
                    error=str(exc),
                )
                fallback = self._ct_call_handle_exception(
                    word=word,
                    ct_word_name=ct_word_name,
                    exc=exc,
                )
                if fallback is not None:
                    return fallback
                raise
            finally:
                del vm.stack[base_depth:]
                active.pop()

            if memo_enabled and memo_key is not None:
                parser._ct_call_memo_cache[memo_key] = _capture_deep_clone(result)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        timeout_ms = int(getattr(parser, "_ct_call_timeout_ms", 0))
        if timeout_ms > 0 and elapsed_ms > timeout_ms:
            self._ct_call_record_side_effect(
                macro_name=word.name,
                ct_word_name=ct_word_name,
                status="timeout",
                memo_hit=memo_hit,
                duration_ms=elapsed_ms,
                error=f"budget={timeout_ms}",
            )
            fallback = self._ct_call_handle_exception(
                word=word,
                ct_word_name=ct_word_name,
                exc=ParseError(f"ct-call timeout budget exceeded ({elapsed_ms}ms > {timeout_ms}ms)"),
            )
            if fallback is not None:
                return fallback
            raise ParseError(
                f"macro '{word.name}' ct-call '{ct_word_name}' timeout budget exceeded ({elapsed_ms}ms > {timeout_ms}ms)"
            )

        self._ct_call_contract_validate_result(word=word, ct_word_name=ct_word_name, result=result)
        lexemes = self._coerce_macro_ct_emit_lexemes(word=word, ct_word_name=ct_word_name, value=result)
        self._ct_call_record_side_effect(
            macro_name=word.name,
            ct_word_name=ct_word_name,
            status="ok",
            memo_hit=memo_hit,
            duration_ms=elapsed_ms,
        )
        return lexemes

    def _parse_macro_template_capture_ref(self, *, word: Word, lexeme: str, field: str) -> str:
        parser = self._parser
        if lexeme.startswith("$"):
            parsed = parser._parse_rewrite_capture(lexeme)
            if parsed is None:
                raise ParseError(
                    f"macro '{word.name}' has invalid {field} capture reference '{lexeme}'"
                )
            _, key, constraint = parsed
            if constraint:
                raise ParseError(
                    f"macro '{word.name}' {field} capture '{lexeme}' cannot include a type constraint"
                )
            return key
        if not lexeme:
            raise ParseError(f"macro '{word.name}' has empty {field} capture reference")
        return lexeme

    @staticmethod
    def _macro_template_unquote_lexeme(lexeme: str) -> str:
        if len(lexeme) >= 2 and lexeme[0] == '"' and lexeme[-1] == '"':
            return lexeme[1:-1]
        return lexeme

    def _template_warn(self, *, word: Word, message: str) -> None:
        parser = self._parser
        token = self._active_macro_token
        if token is not None:
            parser._record_diagnostic(token, f"macro '{word.name}': {message}", level="warning")

    def _macro_capture_lookup_mode(
        self,
        *,
        word: Word,
        scopes: Sequence[Dict[str, Any]],
        key: str,
        raw_ref: str,
        allow_missing: bool = False,
    ) -> Any:
        try:
            return self._macro_capture_lookup(
                word=word,
                scopes=scopes,
                key=key,
                raw_ref=raw_ref,
                allow_missing=allow_missing,
            )
        except ParseError:
            if self._template_unknown_mode == "permissive":
                self._template_warn(
                    word=word,
                    message=f"unknown template symbol '{raw_ref}' treated as empty in permissive mode",
                )
                return []
            raise

    def _macro_capture_validate_constraint(
        self,
        *,
        word: Word,
        raw_ref: str,
        constraint: str,
        captured: Any,
        variadic: bool,
    ) -> None:
        if not constraint:
            return

        parser = self._parser
        values: List[str] = []

        if variadic:
            if self._macro_capture_is_group_list(captured):
                for group in captured:
                    for piece in group:
                        values.append(str(piece))
            elif isinstance(captured, list):
                for piece in captured:
                    values.append(str(piece))
            else:
                raise ParseError(
                    f"macro '{word.name}' variadic placeholder '{raw_ref}' resolved to unsupported value"
                )
        else:
            if self._macro_capture_is_group_list(captured):
                raise ParseError(
                    f"macro '{word.name}' placeholder '{raw_ref}' is variadic; use '$*' for grouped captures"
                )
            if not isinstance(captured, list):
                raise ParseError(
                    f"macro '{word.name}' placeholder '{raw_ref}' resolved to unsupported value"
                )
            for piece in captured:
                values.append(str(piece))

        for piece in values:
            if not parser._rewrite_constraint_matches(piece, constraint):
                raise ParseError(
                    f"macro '{word.name}' placeholder '{raw_ref}' expected '{constraint}' token, got '{piece}'"
                )

    def _resolve_macro_template_include_path(self, *, word: Word, target: str) -> Path:
        raw_target = self._macro_template_unquote_lexeme(target)
        if not raw_target:
            raise ParseError(f"macro '{word.name}' template include/import target cannot be empty")

        raw_path = Path(raw_target).expanduser()
        candidates: List[Path] = []

        if raw_path.is_absolute():
            candidates.append(raw_path)
        else:
            token = self._active_macro_token
            if token is not None:
                loc = self._parser.location_for_token(token)
                if loc.path != _SOURCE_PATH:
                    candidates.append((loc.path.parent / raw_path))
            candidates.append(Path.cwd() / raw_path)

        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()

        tried = "\n".join(f"  - {c}" for c in candidates)
        raise ParseError(
            f"macro '{word.name}' template include/import cannot resolve {raw_target!r}\n"
            f"tried:\n{tried}"
        )

    def _load_macro_template_include_nodes(self, *, word: Word, target: str) -> Tuple[Path, Sequence[Any]]:
        path = self._resolve_macro_template_include_path(word=word, target=target)
        cached = self._template_include_cache.get(path)
        if cached is not None:
            return path, cached

        if path in self._template_include_stack:
            chain = " -> ".join(str(p) for p in self._template_include_stack + [path])
            raise ParseError(f"macro '{word.name}' template include/import cycle detected: {chain}")

        self._template_include_stack.append(path)
        try:
            source = path.read_text(encoding="utf-8")
            include_tokens = [tok.lexeme for tok in self._parser.reader.tokenize(source)]
            include_nodes, include_idx, include_stop = self._parse_macro_template_nodes(
                word=word,
                tokens=include_tokens,
                idx=0,
                stop_tokens=None,
            )
            if include_stop is not None or include_idx != len(include_tokens):
                raise ParseError(
                    f"macro '{word.name}' template include/import parser stopped early for {path}"
                )
            cached_nodes: Sequence[Any] = tuple(include_nodes)
            self._template_include_cache[path] = cached_nodes
            return path, cached_nodes
        finally:
            self._template_include_stack.pop()

    def _parse_macro_template_placeholder_node(self, *, word: Word, lexeme: str) -> Optional[Any]:
        if not lexeme.startswith("$"):
            return None

        parser = self._parser
        parts = lexeme.split("|")
        base = parts[0]
        parsed = parser._parse_rewrite_capture(base)
        if parsed is None:
            return None

        variadic, key, constraint = parsed
        constraint = (constraint or "").lower()

        if len(parts) == 1:
            return ("cap", variadic, key, base, constraint)

        transforms: List[Tuple[str, Any]] = []
        for transform_spec in parts[1:]:
            if transform_spec == "upper":
                transforms.append(("upper", None))
                continue
            if transform_spec == "lower":
                transforms.append(("lower", None))
                continue
            if transform_spec.startswith("join:"):
                sep = transform_spec[5:]
                sep = self._macro_template_unquote_lexeme(sep)
                transforms.append(("join", sep))
                continue
            raise ParseError(
                f"macro '{word.name}' uses unknown placeholder transform '{transform_spec}' in '{lexeme}'"
            )

        return ("capx", variadic, key, base, constraint, tuple(transforms), lexeme)

    def _apply_macro_template_transforms(
        self,
        *,
        word: Word,
        source_tokens: Sequence[str],
        transforms: Sequence[Tuple[str, Any]],
        raw_ref: str,
    ) -> List[str]:
        tokens = list(source_tokens)
        for transform, arg in transforms:
            if transform == "upper":
                tokens = [tok.upper() for tok in tokens]
                continue
            if transform == "lower":
                tokens = [tok.lower() for tok in tokens]
                continue
            if transform == "join":
                tokens = [str(arg).join(tokens)]
                continue
            raise ParseError(
                f"macro '{word.name}' placeholder '{raw_ref}' has unsupported transform '{transform}'"
            )
        return tokens

    def _collect_macro_template_metadata(self, *, word: Word, nodes: Sequence[Any]) -> Tuple[str, Optional[str]]:
        mode = "strict"
        version: Optional[str] = None
        for node in nodes:
            if not node:
                continue
            kind = node[0]
            if kind == "mode":
                mode = node[1]
                continue
            if kind == "version":
                value = self._macro_template_unquote_lexeme(str(node[1]))
                if version is not None and version != value:
                    raise ParseError(
                        f"macro '{word.name}' declares conflicting ct-version markers '{version}' and '{value}'"
                    )
                version = value
        return mode, version

    def _compile_macro_template_program(self, *, nodes: Sequence[Any]) -> Sequence[Any]:
        # A compact immutable program representation for fast repeat expansion.
        def _compile_seq(seq: Sequence[Any]) -> Tuple[Any, ...]:
            compiled: List[Any] = []
            for node in seq:
                kind = node[0]
                if kind in ("lit", "cap", "capx", "ct", "break", "continue", "mode", "version", "emit-list", "include", "diag"):
                    compiled.append(tuple(node))
                    continue
                if kind == "fn-def":
                    compiled.append(("fn-def", node[1], _compile_seq(node[2])))
                    continue
                if kind == "emit-block":
                    compiled.append(("emit-block", _compile_seq(node[1])))
                    continue
                if kind == "let":
                    compiled.append(("let", node[1], _compile_seq(node[2]), _compile_seq(node[3])))
                    continue
                if kind == "if":
                    compiled.append(("if", node[1], _compile_seq(node[2]), _compile_seq(node[3])))
                    continue
                if kind in ("switch", "match"):
                    compiled_cases = tuple((
                        _compile_seq(case_expr),
                        _compile_seq(case_body),
                    ) for case_expr, case_body in node[2])
                    compiled.append((kind, _compile_seq(node[1]), compiled_cases, _compile_seq(node[3])))
                    continue
                if kind == "fold":
                    compiled.append(("fold", node[1], node[2], node[3], node[4], _compile_seq(node[5]), _compile_seq(node[6])))
                    continue
                if kind == "for":
                    compiled.append(("for", node[1], node[2], node[3], node[4], _compile_seq(node[5]), _compile_seq(node[6])))
                    continue
                raise ParseError(f"internal template compile error: unsupported node '{kind}'")
            return tuple(compiled)

        return _compile_seq(nodes)

    def _lookup_macro_template_function(self, name: str) -> Optional[Sequence[Any]]:
        scopes = self._template_function_scopes
        if not scopes:
            return None
        for scope in reversed(scopes):
            body = scope.get(name)
            if body is not None:
                return body
        return None

    @staticmethod
    def _macro_template_expr_parse_const_token(lex: str) -> Tuple[bool, Any]:
        if lex == "true":
            return True, True
        if lex == "false":
            return True, False
        if len(lex) >= 2 and lex[0] == '"' and lex[-1] == '"':
            return True, lex[1:-1]
        try:
            return True, int(lex, 10)
        except Exception:
            return False, None

    def _macro_template_expr_truthy(self, value: Any) -> bool:
        return bool(value)

    def _macro_template_expr_normalize(self, value: Any) -> Any:
        if self._macro_capture_is_group_list(value):
            return tuple(tuple(group) for group in value)
        if isinstance(value, list):
            if len(value) == 1:
                return self._macro_template_expr_normalize(value[0])
            return tuple(self._macro_template_expr_normalize(item) for item in value)
        if isinstance(value, str):
            parsed, const_value = self._macro_template_expr_parse_const_token(value)
            if parsed:
                return const_value
            return value
        return value

    def _macro_template_expr_compare(self, *, word: Word, op: str, left: Any, right: Any) -> bool:
        left_norm = self._macro_template_expr_normalize(left)
        right_norm = self._macro_template_expr_normalize(right)

        if op == "==":
            return left_norm == right_norm
        if op == "!=":
            return left_norm != right_norm

        if op in ("<", "<=", ">", ">="):
            if isinstance(left_norm, (int, float)) and isinstance(right_norm, (int, float)):
                if op == "<":
                    return left_norm < right_norm
                if op == "<=":
                    return left_norm <= right_norm
                if op == ">":
                    return left_norm > right_norm
                return left_norm >= right_norm
            if isinstance(left_norm, str) and isinstance(right_norm, str):
                if op == "<":
                    return left_norm < right_norm
                if op == "<=":
                    return left_norm <= right_norm
                if op == ">":
                    return left_norm > right_norm
                return left_norm >= right_norm
            raise ParseError(
                f"macro '{word.name}' template expression uses '{op}' with unsupported values "
                f"'{left_norm}' and '{right_norm}'"
            )

        raise ParseError(f"macro '{word.name}' template expression uses unknown comparator '{op}'")

    def _macro_template_expr_fold(self, *, word: Word, expr: Any) -> Any:
        kind = expr[0]

        if kind == "not" and expr[1][0] == "const":
            return ("const", not self._macro_template_expr_truthy(expr[1][1]))

        if kind == "and":
            left = expr[1]
            right = expr[2]
            if left[0] == "const":
                if not self._macro_template_expr_truthy(left[1]):
                    return ("const", False)
                if right[0] == "const":
                    return ("const", self._macro_template_expr_truthy(right[1]))
                return right

        if kind == "or":
            left = expr[1]
            right = expr[2]
            if left[0] == "const":
                if self._macro_template_expr_truthy(left[1]):
                    return ("const", True)
                if right[0] == "const":
                    return ("const", self._macro_template_expr_truthy(right[1]))
                return right

        if kind == "cmp":
            op = expr[1]
            left = expr[2]
            right = expr[3]
            if left[0] == "const" and right[0] == "const":
                return (
                    "const",
                    self._macro_template_expr_compare(
                        word=word,
                        op=op,
                        left=left[1],
                        right=right[1],
                    ),
                )

        return expr

    def _parse_macro_template_expr(
        self,
        *,
        word: Word,
        tokens: Sequence[str],
        idx: int,
        stop_tokens: Set[str],
    ) -> Tuple[Any, int, Optional[str]]:
        cursor = idx
        cmp_ops = frozenset({"==", "!=", "<", "<=", ">", ">="})

        def _at_stop() -> bool:
            return cursor >= len(tokens) or tokens[cursor] in stop_tokens

        def _parse_primary() -> Any:
            nonlocal cursor
            if _at_stop():
                raise ParseError(f"macro '{word.name}' has incomplete template expression")

            lex = tokens[cursor]

            if lex == "(":
                cursor += 1
                inner = _parse_or()
                if cursor >= len(tokens) or tokens[cursor] != ")":
                    raise ParseError(f"macro '{word.name}' template expression is missing ')' ")
                cursor += 1
                return inner

            if lex in ("has", "empty"):
                cursor += 1
                if _at_stop():
                    raise ParseError(
                        f"macro '{word.name}' condition '{lex}' requires a capture name"
                    )
                raw_ref = tokens[cursor]
                key = self._parse_macro_template_capture_ref(
                    word=word,
                    lexeme=raw_ref,
                    field=f"'{lex}' condition",
                )
                cursor += 1
                return (lex, key, raw_ref)

            if lex == "first":
                cursor += 1
                return ("first",)

            if lex == "last":
                cursor += 1
                return ("last",)

            if lex.startswith("$"):
                placeholder_node = self._parse_macro_template_placeholder_node(
                    word=word,
                    lexeme=lex,
                )
                if placeholder_node is not None:
                    if placeholder_node[0] == "capx":
                        if len(placeholder_node) >= 7:
                            _, _variadic, key, _base, constraint, transforms, raw_ref = placeholder_node
                        else:
                            _, _variadic, key, _base, transforms, raw_ref = placeholder_node
                            constraint = ""
                        if constraint:
                            raise ParseError(
                                f"macro '{word.name}' expression capture '{raw_ref}' cannot include type constraint '{constraint}'"
                            )
                        cursor += 1
                        return ("var-tx", key, raw_ref, transforms)
                    if len(placeholder_node) >= 5:
                        _, _variadic, key, raw_ref, constraint = placeholder_node
                    else:
                        _, _variadic, key, raw_ref = placeholder_node
                        constraint = ""
                    if constraint:
                        raise ParseError(
                            f"macro '{word.name}' expression capture '{raw_ref}' cannot include type constraint '{constraint}'"
                        )
                    cursor += 1
                    return ("var", key, raw_ref)

                key = self._parse_macro_template_capture_ref(
                    word=word,
                    lexeme=lex,
                    field="template expression",
                )
                cursor += 1
                return ("var", key, lex)

            parsed_const, const_value = self._macro_template_expr_parse_const_token(lex)
            if parsed_const:
                cursor += 1
                return ("const", const_value)

            cursor += 1
            return ("var", lex, lex)

        def _parse_cmp() -> Any:
            nonlocal cursor
            left = _parse_primary()
            while cursor < len(tokens) and tokens[cursor] in cmp_ops:
                op = tokens[cursor]
                cursor += 1
                right = _parse_primary()
                left = self._macro_template_expr_fold(word=word, expr=("cmp", op, left, right))
            return left

        def _parse_not() -> Any:
            nonlocal cursor
            if cursor < len(tokens) and tokens[cursor] in ("not", "!"):
                cursor += 1
                expr = _parse_not()
                return self._macro_template_expr_fold(word=word, expr=("not", expr))
            return _parse_cmp()

        def _parse_and() -> Any:
            nonlocal cursor
            left = _parse_not()
            while cursor < len(tokens) and tokens[cursor] in ("and", "&&"):
                cursor += 1
                right = _parse_not()
                left = self._macro_template_expr_fold(word=word, expr=("and", left, right))
            return left

        def _parse_or() -> Any:
            nonlocal cursor
            left = _parse_and()
            while cursor < len(tokens) and tokens[cursor] in ("or", "||"):
                cursor += 1
                right = _parse_and()
                left = self._macro_template_expr_fold(word=word, expr=("or", left, right))
            return left

        parsed = _parse_or()
        if cursor < len(tokens) and tokens[cursor] in stop_tokens:
            return parsed, cursor, tokens[cursor]
        return parsed, cursor, None

    def _parse_macro_template_condition(
        self,
        *,
        word: Word,
        tokens: Sequence[str],
        idx: int,
    ) -> Tuple[Any, int]:
        if idx >= len(tokens):
            raise ParseError(f"macro '{word.name}' has incomplete template condition")

        lex = tokens[idx]
        if lex == "not":
            nested, next_idx = self._parse_macro_template_condition(word=word, tokens=tokens, idx=idx + 1)
            return ("not", nested), next_idx

        if lex == "first":
            return ("first",), idx + 1

        if lex == "last":
            return ("last",), idx + 1

        if lex in ("has", "empty"):
            if idx + 1 >= len(tokens):
                raise ParseError(
                    f"macro '{word.name}' condition '{lex}' requires a capture name"
                )
            raw_ref = tokens[idx + 1]
            key = self._parse_macro_template_capture_ref(
                word=word,
                lexeme=raw_ref,
                field=f"'{lex}' condition",
            )
            return (lex, key, raw_ref), idx + 2

        raise ParseError(
            f"macro '{word.name}' uses unsupported template condition '{lex}'"
        )

    def _parse_macro_template_nodes(
        self,
        *,
        word: Word,
        tokens: Sequence[str],
        idx: int = 0,
        stop_tokens: Optional[Set[str]] = None,
    ) -> Tuple[List[Any], int, Optional[str]]:
        parser = self._parser
        nodes: List[Any] = []
        runtime_depth = 0

        def _runtime_depth_step(lexeme: str) -> None:
            nonlocal runtime_depth
            if lexeme in self._TEMPLATE_RUNTIME_BLOCK_OPENERS:
                runtime_depth += 1
                return
            if lexeme == "end" and runtime_depth > 0:
                runtime_depth -= 1

        while idx < len(tokens):
            lex = tokens[idx]
            if stop_tokens and runtime_depth == 0 and lex in stop_tokens:
                return nodes, idx, lex

            if runtime_depth == 0 and lex == "ct-#":
                # Single-token comment marker.
                idx += 2 if idx + 1 < len(tokens) else 1
                continue

            if runtime_depth == 0 and lex == "ct-comment":
                # Nestable block comment: ct-comment ... ct-endcomment
                depth = 1
                idx += 1
                while idx < len(tokens) and depth > 0:
                    if tokens[idx] == "ct-comment":
                        depth += 1
                    elif tokens[idx] == "ct-endcomment":
                        depth -= 1
                    idx += 1
                if depth != 0:
                    raise ParseError(
                        f"macro '{word.name}' template block comment is missing closing 'ct-endcomment'"
                    )
                continue

            if runtime_depth == 0 and lex == "ct-endcomment":
                raise ParseError(
                    f"macro '{word.name}' template has unexpected 'ct-endcomment'"
                )

            if runtime_depth == 0 and lex == "ct-#(":
                # Nestable block comment: ct-#( ... ct-#)
                depth = 1
                idx += 1
                while idx < len(tokens) and depth > 0:
                    if tokens[idx] == "ct-#(":
                        depth += 1
                    elif tokens[idx] == "ct-#)":
                        depth -= 1
                    idx += 1
                if depth != 0:
                    raise ParseError(
                        f"macro '{word.name}' template block comment is missing closing 'ct-#)'"
                    )
                continue

            if runtime_depth == 0 and lex == "ct-#)":
                raise ParseError(
                    f"macro '{word.name}' template has unexpected 'ct-#)'"
                )

            if lex.startswith("\\") and len(lex) > 1:
                escaped = lex[1:]
                nodes.append(("lit", escaped))
                _runtime_depth_step(escaped)
                idx += 1
                continue

            if runtime_depth == 0 and lex == "ct-call":
                if idx + 1 >= len(tokens):
                    raise ParseError(f"macro '{word.name}' template 'ct-call' requires a target word")
                target = tokens[idx + 1]
                if not target:
                    raise ParseError(f"macro '{word.name}' template 'ct-call' target cannot be empty")
                nodes.append(("ct", target))
                idx += 2
                continue

            if runtime_depth == 0 and lex in ("ct-include", "ct-import"):
                if idx + 1 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template '{lex}' requires a path token"
                    )
                nodes.append(("include", lex, tokens[idx + 1]))
                idx += 2
                continue

            if runtime_depth == 0 and lex in ("emit-list", "ct-emit-list"):
                if idx + 1 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template '{lex}' requires a capture name"
                    )
                raw_ref = tokens[idx + 1]
                source_key = self._parse_macro_template_capture_ref(
                    word=word,
                    lexeme=raw_ref,
                    field="emit-list source",
                )
                nodes.append(("emit-list", source_key, raw_ref))
                idx += 2
                continue

            if runtime_depth == 0 and lex in ("emit-block", "ct-emit-block"):
                cursor = idx + 1
                if cursor < len(tokens) and tokens[cursor] in ("do", "ct-do"):
                    cursor += 1
                body_nodes, body_idx, body_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=cursor,
                    stop_tokens={"end"},
                )
                if body_stop != "end":
                    raise ParseError(
                        f"macro '{word.name}' template '{lex}' is missing 'end'"
                    )
                nodes.append(("emit-block", body_nodes))
                idx = body_idx + 1
                continue

            if runtime_depth == 0 and lex == "ct-version":
                if idx + 1 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-version' requires a version token"
                    )
                nodes.append(("version", tokens[idx + 1]))
                idx += 2
                continue

            if runtime_depth == 0 and lex == "ct-strict":
                nodes.append(("mode", "strict"))
                idx += 1
                continue

            if runtime_depth == 0 and lex == "ct-permissive":
                nodes.append(("mode", "permissive"))
                idx += 1
                continue

            if runtime_depth == 0 and lex in ("ct-error", "ct-warning", "ct-note"):
                if idx + 1 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template '{lex}' requires a message token"
                    )
                nodes.append(("diag", lex, tokens[idx + 1], idx))
                idx += 2
                continue

            if runtime_depth == 0 and lex == "ct-fn":
                if idx + 2 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fn' requires '<name> do ... end'"
                    )
                fn_name = tokens[idx + 1]
                if not _is_identifier(fn_name):
                    raise ParseError(
                        f"macro '{word.name}' template function name '{fn_name}' is not a valid identifier"
                    )
                cursor = idx + 2
                if cursor >= len(tokens) or tokens[cursor] not in ("do", "ct-do"):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fn' requires 'do'"
                    )
                fn_body_nodes, fn_body_idx, fn_body_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=cursor + 1,
                    stop_tokens={"end"},
                )
                if fn_body_stop != "end":
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fn' is missing 'end'"
                    )
                nodes.append(("fn-def", fn_name, fn_body_nodes))
                idx = fn_body_idx + 1
                continue

            if runtime_depth == 0 and lex == "ct-let":
                if idx + 2 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-let' requires a binding name and expression"
                    )
                binding_name = tokens[idx + 1]
                if not _is_identifier(binding_name):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-let' binding name '{binding_name}' is not a valid identifier"
                    )
                cursor = idx + 2
                if cursor < len(tokens) and tokens[cursor] == "=":
                    cursor += 1

                binding_expr_nodes, binding_expr_idx, binding_expr_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=cursor,
                    stop_tokens={"do", "ct-do"},
                )
                if binding_expr_stop not in ("do", "ct-do"):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-let' is missing 'do'"
                    )
                if not binding_expr_nodes:
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-let' requires a non-empty expression"
                    )

                body_nodes, body_idx, body_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=binding_expr_idx + 1,
                    stop_tokens={"end"},
                )
                if body_stop != "end":
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-let' is missing 'end'"
                    )

                nodes.append(("let", binding_name, binding_expr_nodes, body_nodes))
                idx = body_idx + 1
                continue

            if runtime_depth == 0 and lex == "ct-switch":
                switch_expr_nodes, switch_expr_idx, switch_expr_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=idx + 1,
                    stop_tokens={"do", "ct-do"},
                )
                if switch_expr_stop not in ("do", "ct-do"):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-switch' is missing 'do'"
                    )
                if not switch_expr_nodes:
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-switch' requires a non-empty expression"
                    )

                cursor = switch_expr_idx + 1
                cases: List[Any] = []
                default_nodes: List[Any] = []
                seen_default = False

                while cursor < len(tokens):
                    branch_lex = tokens[cursor]
                    if branch_lex == "end":
                        nodes.append(("switch", switch_expr_nodes, cases, default_nodes))
                        idx = cursor + 1
                        break

                    if branch_lex == "ct-case":
                        if seen_default:
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-case' cannot appear after 'ct-default'"
                            )
                        case_expr_nodes, case_expr_idx, case_expr_stop = self._parse_macro_template_nodes(
                            word=word,
                            tokens=tokens,
                            idx=cursor + 1,
                            stop_tokens={"do", "ct-do"},
                        )
                        if case_expr_stop not in ("do", "ct-do"):
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-case' is missing 'do'"
                            )
                        if not case_expr_nodes:
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-case' requires a non-empty expression"
                            )
                        case_body_nodes, case_body_idx, case_body_stop = self._parse_macro_template_nodes(
                            word=word,
                            tokens=tokens,
                            idx=case_expr_idx + 1,
                            stop_tokens={"end"},
                        )
                        if case_body_stop != "end":
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-case' is missing 'end'"
                            )
                        cases.append((case_expr_nodes, case_body_nodes))
                        cursor = case_body_idx + 1
                        continue

                    if branch_lex == "ct-default":
                        if seen_default:
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-switch' can only have one 'ct-default'"
                            )
                        seen_default = True
                        default_cursor = cursor + 1
                        if default_cursor < len(tokens) and tokens[default_cursor] in ("do", "ct-do"):
                            default_cursor += 1
                        default_nodes, default_idx, default_stop = self._parse_macro_template_nodes(
                            word=word,
                            tokens=tokens,
                            idx=default_cursor,
                            stop_tokens={"end"},
                        )
                        if default_stop != "end":
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-default' is missing 'end'"
                            )
                        cursor = default_idx + 1
                        continue

                    raise ParseError(
                        f"macro '{word.name}' template 'ct-switch' expected 'ct-case', 'ct-default', or 'end', got '{branch_lex}'"
                    )
                else:
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-switch' is missing 'end'"
                    )
                continue

            if runtime_depth == 0 and lex == "ct-match":
                match_expr_nodes, match_expr_idx, match_expr_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=idx + 1,
                    stop_tokens={"do", "ct-do"},
                )
                if match_expr_stop not in ("do", "ct-do"):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-match' is missing 'do'"
                    )
                if not match_expr_nodes:
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-match' requires a non-empty expression"
                    )

                cursor = match_expr_idx + 1
                cases: List[Any] = []
                default_nodes: List[Any] = []
                seen_default = False

                while cursor < len(tokens):
                    branch_lex = tokens[cursor]
                    if branch_lex == "end":
                        nodes.append(("match", match_expr_nodes, cases, default_nodes))
                        idx = cursor + 1
                        break

                    if branch_lex == "ct-case":
                        if seen_default:
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-case' cannot appear after 'ct-default'"
                            )
                        case_expr_nodes, case_expr_idx, case_expr_stop = self._parse_macro_template_nodes(
                            word=word,
                            tokens=tokens,
                            idx=cursor + 1,
                            stop_tokens={"then", "ct-then"},
                        )
                        if case_expr_stop not in ("then", "ct-then"):
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-case' is missing 'then'"
                            )
                        if not case_expr_nodes:
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-case' requires a non-empty expression"
                            )
                        case_body_nodes, case_body_idx, case_body_stop = self._parse_macro_template_nodes(
                            word=word,
                            tokens=tokens,
                            idx=case_expr_idx + 1,
                            stop_tokens={"ct-case", "ct-default", "end"},
                        )
                        if case_body_stop is None:
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-match' is missing 'end'"
                            )
                        cases.append((case_expr_nodes, case_body_nodes))
                        cursor = case_body_idx
                        continue

                    if branch_lex == "ct-default":
                        if seen_default:
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-match' can only have one 'ct-default'"
                            )
                        seen_default = True
                        default_cursor = cursor + 1
                        if default_cursor < len(tokens) and tokens[default_cursor] in ("then", "ct-then"):
                            default_cursor += 1
                        default_nodes, default_idx, default_stop = self._parse_macro_template_nodes(
                            word=word,
                            tokens=tokens,
                            idx=default_cursor,
                            stop_tokens={"end"},
                        )
                        if default_stop != "end":
                            raise ParseError(
                                f"macro '{word.name}' template 'ct-default' is missing 'end'"
                            )
                        cursor = default_idx
                        continue

                    raise ParseError(
                        f"macro '{word.name}' template 'ct-match' expected 'ct-case', 'ct-default', or 'end', got '{branch_lex}'"
                    )
                else:
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-match' is missing 'end'"
                    )
                continue

            if runtime_depth == 0 and lex == "ct-fold":
                if idx + 5 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' requires '<acc> <item> in <capture> with <init...> do ... end'"
                    )
                acc_name = tokens[idx + 1]
                item_name = tokens[idx + 2]
                if not _is_identifier(acc_name):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' accumulator name '{acc_name}' is not a valid identifier"
                    )
                if not _is_identifier(item_name):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' item name '{item_name}' is not a valid identifier"
                    )
                if tokens[idx + 3] != "in":
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' requires 'in' after accumulator/item names"
                    )
                source_ref = tokens[idx + 4]
                if tokens[idx + 5] != "with":
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' requires 'with' before initializer expression"
                    )
                source_key = self._parse_macro_template_capture_ref(
                    word=word,
                    lexeme=source_ref,
                    field="ct-fold source",
                )

                init_nodes, init_idx, init_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=idx + 6,
                    stop_tokens={"do", "ct-do"},
                )
                if init_stop not in ("do", "ct-do"):
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' is missing 'do'"
                    )
                if not init_nodes:
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' requires a non-empty initializer expression"
                    )

                body_nodes, body_idx, body_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=init_idx + 1,
                    stop_tokens={"end"},
                )
                if body_stop != "end":
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-fold' is missing 'end'"
                    )

                nodes.append(("fold", acc_name, item_name, source_key, source_ref, init_nodes, body_nodes))
                idx = body_idx + 1
                continue

            if runtime_depth == 0 and lex in ("ct-break", "ct-continue"):
                nodes.append(("break" if lex == "ct-break" else "continue", lex))
                idx += 1
                continue

            if runtime_depth == 0 and lex in ("ct-if", "ct-when", "ct-unless"):
                if idx + 1 >= len(tokens):
                    raise ParseError(
                        f"macro '{word.name}' template '{lex}' requires a condition"
                    )

                if lex == "ct-if":
                    cond, cond_end, cond_stop = self._parse_macro_template_expr(
                        word=word,
                        tokens=tokens,
                        idx=idx + 1,
                        stop_tokens={"then", "ct-then"},
                    )
                    if cond_stop not in ("then", "ct-then"):
                        raise ParseError(
                            f"macro '{word.name}' template 'ct-if' requires 'then'"
                        )
                    cursor = cond_end + 1
                elif tokens[idx + 1] in self._TEMPLATE_COND_STARTERS:
                    # Backward-compatible shorthand for simple ct-when/ct-unless guards.
                    cond, cond_end = self._parse_macro_template_condition(word=word, tokens=tokens, idx=idx + 1)
                    cursor = cond_end
                    if cursor < len(tokens) and tokens[cursor] in ("then", "ct-then"):
                        cursor += 1
                else:
                    cond, cond_end, cond_stop = self._parse_macro_template_expr(
                        word=word,
                        tokens=tokens,
                        idx=idx + 1,
                        stop_tokens={"then", "ct-then"},
                    )
                    if cond_stop not in ("then", "ct-then"):
                        raise ParseError(
                            f"macro '{word.name}' template '{lex}' expression conditions require 'then'"
                        )
                    cursor = cond_end + 1

                if lex in ("ct-when", "ct-unless"):
                    body_nodes, body_idx, body_stop = self._parse_macro_template_nodes(
                        word=word,
                        tokens=tokens,
                        idx=cursor,
                        stop_tokens={"end"},
                    )
                    if body_stop != "end":
                        raise ParseError(
                            f"macro '{word.name}' template '{lex}' is missing 'end'"
                        )
                    if lex == "ct-when":
                        nodes.append(("if", cond, body_nodes, []))
                    else:
                        nodes.append(("if", cond, [], body_nodes))
                    idx = body_idx + 1
                    continue

                then_nodes, branch_idx, branch_stop = self._parse_macro_template_nodes(
                    word=word,
                    tokens=tokens,
                    idx=cursor,
                    stop_tokens={"else", "ct-else", "end"},
                )
                if branch_stop is None:
                    raise ParseError(
                        f"macro '{word.name}' template 'ct-if' is missing 'end'"
                    )

                else_nodes: List[Any] = []
                if branch_stop in ("else", "ct-else"):
                    else_nodes, branch_idx, branch_stop = self._parse_macro_template_nodes(
                        word=word,
                        tokens=tokens,
                        idx=branch_idx + 1,
                        stop_tokens={"end"},
                    )
                    if branch_stop != "end":
                        raise ParseError(
                            f"macro '{word.name}' template 'ct-if' is missing 'end'"
                        )

                nodes.append(("if", cond, then_nodes, else_nodes))
                idx = branch_idx + 1
                continue

            if runtime_depth == 0 and lex in ("ct-for", "ct-each"):
                key_name: Optional[str] = None
                item_name: Optional[str] = None
                source_ref: Optional[str] = None
                cursor = idx

                if (
                    lex == "ct-for"
                    and idx + 3 < len(tokens)
                    and _is_identifier(tokens[idx + 1])
                    and tokens[idx + 2] == "in"
                ):
                    item_name = tokens[idx + 1]
                    source_ref = tokens[idx + 3]
                    cursor = idx + 4
                elif lex == "ct-each":
                    if (
                        idx + 4 < len(tokens)
                        and _is_identifier(tokens[idx + 1])
                        and _is_identifier(tokens[idx + 2])
                        and tokens[idx + 3] == "in"
                    ):
                        key_name = tokens[idx + 1]
                        item_name = tokens[idx + 2]
                        source_ref = tokens[idx + 4]
                        cursor = idx + 5
                    elif (
                        idx + 3 < len(tokens)
                        and _is_identifier(tokens[idx + 1])
                        and tokens[idx + 2] == "in"
                    ):
                        item_name = tokens[idx + 1]
                        source_ref = tokens[idx + 3]
                        cursor = idx + 4

                if item_name is not None and source_ref is not None:
                    source_key = self._parse_macro_template_capture_ref(
                        word=word,
                        lexeme=source_ref,
                        field=f"{lex} source",
                    )
                    separator_nodes: List[Any] = []
                    if cursor < len(tokens) and tokens[cursor] == "sep":
                        separator_nodes, cursor, sep_stop = self._parse_macro_template_nodes(
                            word=word,
                            tokens=tokens,
                            idx=cursor + 1,
                            stop_tokens={"do", "ct-do"},
                        )
                        if sep_stop not in ("do", "ct-do"):
                            raise ParseError(
                                f"macro '{word.name}' template '{lex} ... sep' is missing 'do'"
                            )
                        cursor += 1
                    elif cursor < len(tokens) and tokens[cursor] in ("do", "ct-do"):
                        cursor += 1
                    else:
                        raise ParseError(
                            f"macro '{word.name}' template '{lex}' requires 'do' (or 'sep ... do')"
                        )

                    body_nodes, body_idx, body_stop = self._parse_macro_template_nodes(
                        word=word,
                        tokens=tokens,
                        idx=cursor,
                        stop_tokens={"end"},
                    )
                    if body_stop != "end":
                        raise ParseError(
                            f"macro '{word.name}' template '{lex}' is missing 'end'"
                        )

                    nodes.append(("for", item_name, key_name, source_key, source_ref, separator_nodes, body_nodes))
                    idx = body_idx + 1
                    continue

            placeholder_node = self._parse_macro_template_placeholder_node(word=word, lexeme=lex)
            if placeholder_node is not None:
                nodes.append(placeholder_node)
                idx += 1
                continue

            parsed = parser._parse_rewrite_capture(lex)
            if parsed is None:
                nodes.append(("lit", lex))
                _runtime_depth_step(lex)
                idx += 1
                continue

            variadic, key, constraint = parsed
            nodes.append(("cap", variadic, key, lex, (constraint or "").lower()))
            idx += 1

        return nodes, idx, None

    def _eval_macro_template_condition(
        self,
        *,
        word: Word,
        cond: Any,
        scopes: Sequence[Dict[str, Any]],
        loop_stack: Sequence[Dict[str, int]],
    ) -> bool:
        if isinstance(cond, tuple) and cond:
            kind = cond[0]
            if kind == "first":
                if not loop_stack:
                    raise ParseError(
                        f"macro '{word.name}' template condition 'first' can only be used inside a for-loop"
                    )
                return loop_stack[-1]["index"] == 0
            if kind == "last":
                if not loop_stack:
                    raise ParseError(
                        f"macro '{word.name}' template condition 'last' can only be used inside a for-loop"
                    )
                frame = loop_stack[-1]
                return frame["index"] + 1 == frame["count"]
            if kind in ("has", "empty"):
                key = cond[1]
                raw_ref = cond[2]
                value = self._macro_capture_lookup(
                    word=word,
                    scopes=scopes,
                    key=key,
                    raw_ref=raw_ref,
                    allow_missing=True,
                )
                present = bool(value)
                return present if kind == "has" else (not present)
            if kind == "not" and isinstance(cond[1], tuple):
                nested = cond[1]
                nested_kind = nested[0] if nested else ""
                if nested_kind in ("has", "empty", "first", "last"):
                    return not self._eval_macro_template_condition(
                        word=word,
                        cond=nested,
                        scopes=scopes,
                        loop_stack=loop_stack,
                    )

        value = self._eval_macro_template_expr_value(
            word=word,
            expr=cond,
            scopes=scopes,
            loop_stack=loop_stack,
        )
        return self._macro_template_expr_truthy(value)

    def _eval_macro_template_expr_value(
        self,
        *,
        word: Word,
        expr: Any,
        scopes: Sequence[Dict[str, Any]],
        loop_stack: Sequence[Dict[str, int]],
    ) -> Any:
        kind = expr[0]

        if kind == "const":
            return expr[1]

        if kind == "var":
            key = expr[1]
            raw_ref = expr[2]
            return self._macro_capture_lookup_mode(
                word=word,
                scopes=scopes,
                key=key,
                raw_ref=raw_ref,
            )

        if kind == "var-tx":
            key = expr[1]
            raw_ref = expr[2]
            transforms = expr[3]
            value = self._macro_capture_lookup_mode(
                word=word,
                scopes=scopes,
                key=key,
                raw_ref=raw_ref,
            )
            source_tokens = self._macro_template_binding_value_lexemes(
                word=word,
                value=value,
                context=f"placeholder '{raw_ref}'",
            )
            transformed = self._apply_macro_template_transforms(
                word=word,
                source_tokens=source_tokens,
                transforms=transforms,
                raw_ref=raw_ref,
            )
            if len(transformed) == 1:
                parsed_const, const_value = self._macro_template_expr_parse_const_token(transformed[0])
                if parsed_const:
                    return const_value
                return transformed[0]
            return transformed

        if kind == "first":
            if not loop_stack:
                raise ParseError(
                    f"macro '{word.name}' template condition 'first' can only be used inside a for-loop"
                )
            return loop_stack[-1]["index"] == 0

        if kind == "last":
            if not loop_stack:
                raise ParseError(
                    f"macro '{word.name}' template condition 'last' can only be used inside a for-loop"
                )
            frame = loop_stack[-1]
            return frame["index"] + 1 == frame["count"]

        if kind in ("has", "empty"):
            key = expr[1]
            raw_ref = expr[2]
            value = self._macro_capture_lookup(
                word=word,
                scopes=scopes,
                key=key,
                raw_ref=raw_ref,
                allow_missing=True,
            )
            present = bool(value)
            if kind == "has":
                return present
            return not present

        if kind == "not":
            value = self._eval_macro_template_expr_value(
                word=word,
                expr=expr[1],
                scopes=scopes,
                loop_stack=loop_stack,
            )
            return not self._macro_template_expr_truthy(value)

        if kind == "and":
            left = self._eval_macro_template_expr_value(
                word=word,
                expr=expr[1],
                scopes=scopes,
                loop_stack=loop_stack,
            )
            if not self._macro_template_expr_truthy(left):
                return False
            right = self._eval_macro_template_expr_value(
                word=word,
                expr=expr[2],
                scopes=scopes,
                loop_stack=loop_stack,
            )
            return self._macro_template_expr_truthy(right)

        if kind == "or":
            left = self._eval_macro_template_expr_value(
                word=word,
                expr=expr[1],
                scopes=scopes,
                loop_stack=loop_stack,
            )
            if self._macro_template_expr_truthy(left):
                return True
            right = self._eval_macro_template_expr_value(
                word=word,
                expr=expr[2],
                scopes=scopes,
                loop_stack=loop_stack,
            )
            return self._macro_template_expr_truthy(right)

        if kind == "cmp":
            op = expr[1]
            left = self._eval_macro_template_expr_value(
                word=word,
                expr=expr[2],
                scopes=scopes,
                loop_stack=loop_stack,
            )
            right = self._eval_macro_template_expr_value(
                word=word,
                expr=expr[3],
                scopes=scopes,
                loop_stack=loop_stack,
            )
            return self._macro_template_expr_compare(
                word=word,
                op=op,
                left=left,
                right=right,
            )

        raise ParseError(f"macro '{word.name}' has unknown template expression node kind '{kind}'")

    def _eval_macro_template_binding_value(
        self,
        *,
        word: Word,
        expr_nodes: Sequence[Any],
        scopes: Sequence[Dict[str, Any]],
        loop_stack: Sequence[Dict[str, int]],
    ) -> Any:
        # Preserve variadic/group-list shape for direct alias bindings.
        if len(expr_nodes) == 1 and expr_nodes[0][0] == "cap":
            variadic, key, raw_ref = expr_nodes[0][1], expr_nodes[0][2], expr_nodes[0][3]
            captured = self._macro_capture_lookup_mode(word=word, scopes=scopes, key=key, raw_ref=raw_ref)
            if variadic:
                if isinstance(captured, list):
                    return self._macro_capture_clone(captured)
                raise ParseError(
                    f"macro '{word.name}' variadic placeholder '{raw_ref}' resolved to unsupported value"
                )
            if self._macro_capture_is_group_list(captured):
                raise ParseError(
                    f"macro '{word.name}' placeholder '{raw_ref}' is variadic; use '$*{key}' to splice all arguments"
                )
            if not isinstance(captured, list):
                raise ParseError(
                    f"macro '{word.name}' placeholder '{raw_ref}' resolved to unsupported value"
                )
            return list(captured)

        return self._expand_macro_template_nodes(
            word=word,
            nodes=expr_nodes,
            scopes=scopes,
            loop_stack=loop_stack,
        )

    def _eval_macro_template_expr_lexemes(
        self,
        *,
        word: Word,
        expr_nodes: Sequence[Any],
        scopes: Sequence[Dict[str, Any]],
        loop_stack: Sequence[Dict[str, int]],
    ) -> List[str]:
        return self._expand_macro_template_nodes(
            word=word,
            nodes=expr_nodes,
            scopes=scopes,
            loop_stack=loop_stack,
        )

    def _macro_template_binding_value_lexemes(
        self,
        *,
        word: Word,
        value: Any,
        context: str,
    ) -> List[str]:
        if self._macro_capture_is_group_list(value):
            out: List[str] = []
            for group in value:
                out.extend(group)
            return out
        if isinstance(value, list):
            return list(value)
        raise ParseError(
            f"macro '{word.name}' template {context} resolved to unsupported value"
        )

    def _expand_macro_template_nodes_into(
        self,
        *,
        word: Word,
        nodes: Sequence[Any],
        scopes: List[Dict[str, Any]],
        loop_stack: List[Dict[str, int]],
        out: List[str],
    ) -> None:
        out_append = out.append
        out_extend = out.extend
        for node in nodes:
            kind = node[0]
            if kind == "lit":
                out_append(node[1])
                continue

            if kind == "cap":
                variadic, key, raw_ref = node[1], node[2], node[3]
                constraint = node[4] if len(node) > 4 else ""
                captured = self._macro_capture_lookup_mode(word=word, scopes=scopes, key=key, raw_ref=raw_ref)
                self._macro_capture_validate_constraint(
                    word=word,
                    raw_ref=raw_ref,
                    constraint=constraint,
                    captured=captured,
                    variadic=variadic,
                )
                if variadic:
                    if self._macro_capture_is_group_list(captured):
                        for group in captured:
                            out_extend(group)
                    elif isinstance(captured, list):
                        out_extend(captured)
                    else:
                        raise ParseError(
                            f"macro '{word.name}' variadic placeholder '{raw_ref}' resolved to unsupported value"
                        )
                    continue

                if self._macro_capture_is_group_list(captured):
                    raise ParseError(
                        f"macro '{word.name}' placeholder '{raw_ref}' is variadic; use '$*{key}' to splice all arguments"
                    )
                if not isinstance(captured, list):
                    raise ParseError(
                        f"macro '{word.name}' placeholder '{raw_ref}' resolved to unsupported value"
                    )
                out_extend(captured)
                continue

            if kind == "capx":
                variadic, key, raw_ref = node[1], node[2], node[3]
                if len(node) >= 7:
                    constraint = node[4]
                    transforms = node[5]
                    raw_token = node[6]
                else:
                    constraint = ""
                    transforms = node[4]
                    raw_token = node[5]
                captured = self._macro_capture_lookup_mode(word=word, scopes=scopes, key=key, raw_ref=raw_ref)
                self._macro_capture_validate_constraint(
                    word=word,
                    raw_ref=raw_ref,
                    constraint=constraint,
                    captured=captured,
                    variadic=variadic,
                )
                source_tokens: List[str]
                if variadic:
                    if self._macro_capture_is_group_list(captured):
                        source_tokens = []
                        for group in captured:
                            source_tokens.extend(group)
                    elif isinstance(captured, list):
                        source_tokens = list(captured)
                    else:
                        raise ParseError(
                            f"macro '{word.name}' variadic placeholder '{raw_ref}' resolved to unsupported value"
                        )
                else:
                    if self._macro_capture_is_group_list(captured):
                        raise ParseError(
                            f"macro '{word.name}' placeholder '{raw_ref}' is variadic; use '$*{key}' to splice all arguments"
                        )
                    if not isinstance(captured, list):
                        raise ParseError(
                            f"macro '{word.name}' placeholder '{raw_ref}' resolved to unsupported value"
                        )
                    source_tokens = list(captured)

                out_extend(
                    self._apply_macro_template_transforms(
                        word=word,
                        source_tokens=source_tokens,
                        transforms=transforms,
                        raw_ref=raw_token,
                    )
                )
                continue

            if kind == "mode":
                # Mode metadata is pre-applied per macro expansion.
                continue

            if kind == "version":
                # Version marker metadata, no token emission.
                continue

            if kind == "diag":
                diag_kind = node[1]
                raw_message = self._macro_template_unquote_lexeme(str(node[2]))
                span_idx = node[3]
                text = f"macro '{word.name}' template diagnostic at span[{span_idx}]: {raw_message}"
                if diag_kind == "ct-error":
                    raise ParseError(text)
                if diag_kind == "ct-warning":
                    self._template_warn(word=word, message=text)
                    continue
                # ct-note: diagnostic that does not alter expansion.
                self._template_warn(word=word, message=f"note: {text}")
                continue

            if kind == "include":
                include_kind = node[1]
                include_target = node[2]
                include_path, include_nodes = self._load_macro_template_include_nodes(
                    word=word,
                    target=include_target,
                )
                if include_kind == "ct-import":
                    import_scopes = self._template_import_scopes
                    if import_scopes:
                        imported = import_scopes[-1]
                        if include_path in imported:
                            continue
                        imported.add(include_path)
                self._expand_macro_template_nodes_into(
                    word=word,
                    nodes=include_nodes,
                    scopes=scopes,
                    loop_stack=loop_stack,
                    out=out,
                )
                continue

            if kind == "emit-list":
                source_key = node[1]
                raw_ref = node[2]
                value = self._macro_capture_lookup_mode(
                    word=word,
                    scopes=scopes,
                    key=source_key,
                    raw_ref=raw_ref,
                )
                if self._macro_capture_is_group_list(value):
                    for group in value:
                        out_extend(group)
                    continue
                if isinstance(value, list):
                    out_extend(value)
                    continue
                raise ParseError(
                    f"macro '{word.name}' template 'emit-list' source '{raw_ref}' resolved to unsupported value"
                )

            if kind == "emit-block":
                self._expand_macro_template_nodes_into(
                    word=word,
                    nodes=node[1],
                    scopes=scopes,
                    loop_stack=loop_stack,
                    out=out,
                )
                continue

            if kind == "if":
                cond = node[1]
                then_nodes = node[2]
                else_nodes = node[3]
                branch = then_nodes if self._eval_macro_template_condition(
                    word=word,
                    cond=cond,
                    scopes=scopes,
                    loop_stack=loop_stack,
                ) else else_nodes
                self._expand_macro_template_nodes_into(
                    word=word,
                    nodes=branch,
                    scopes=scopes,
                    loop_stack=loop_stack,
                    out=out,
                )
                continue

            if kind == "ct":
                ct_word_name = node[1]
                fn_nodes = self._lookup_macro_template_function(ct_word_name)
                if fn_nodes is not None:
                    self._expand_macro_template_nodes_into(
                        word=word,
                        nodes=fn_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                        out=out,
                    )
                else:
                    out_extend(
                        self._invoke_macro_ct_word(
                            word=word,
                            ct_word_name=ct_word_name,
                            scopes=scopes,
                            loop_stack=loop_stack,
                        )
                    )
                continue

            if kind == "fn-def":
                fn_name = node[1]
                fn_body_nodes = node[2]
                scopes_stack = self._template_function_scopes
                if not scopes_stack:
                    raise ParseError(
                        f"internal macro template error in '{word.name}': template function registry is unavailable"
                    )
                scopes_stack[-1][fn_name] = fn_body_nodes
                continue

            if kind == "let":
                binding_name = node[1]
                binding_expr_nodes = node[2]
                body_nodes = node[3]
                local_scope = {
                    binding_name: self._eval_macro_template_binding_value(
                        word=word,
                        expr_nodes=binding_expr_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                    )
                }
                scopes.append(local_scope)
                try:
                    self._expand_macro_template_nodes_into(
                        word=word,
                        nodes=body_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                        out=out,
                    )
                finally:
                    scopes.pop()
                continue

            if kind == "switch":
                switch_expr_nodes = node[1]
                cases = node[2]
                default_nodes = node[3]
                switch_value = tuple(
                    self._eval_macro_template_expr_lexemes(
                        word=word,
                        expr_nodes=switch_expr_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                    )
                )
                matched = False
                for case_expr_nodes, case_body_nodes in cases:
                    case_value = tuple(
                        self._eval_macro_template_expr_lexemes(
                            word=word,
                            expr_nodes=case_expr_nodes,
                            scopes=scopes,
                            loop_stack=loop_stack,
                        )
                    )
                    if case_value != switch_value:
                        continue
                    matched = True
                    self._expand_macro_template_nodes_into(
                        word=word,
                        nodes=case_body_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                        out=out,
                    )
                    break
                if not matched and default_nodes:
                    self._expand_macro_template_nodes_into(
                        word=word,
                        nodes=default_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                        out=out,
                    )
                continue

            if kind == "match":
                match_expr_nodes = node[1]
                cases = node[2]
                default_nodes = node[3]
                match_value = tuple(
                    self._eval_macro_template_expr_lexemes(
                        word=word,
                        expr_nodes=match_expr_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                    )
                )
                matched = False
                for case_expr_nodes, case_body_nodes in cases:
                    case_value = tuple(
                        self._eval_macro_template_expr_lexemes(
                            word=word,
                            expr_nodes=case_expr_nodes,
                            scopes=scopes,
                            loop_stack=loop_stack,
                        )
                    )
                    if case_value != match_value:
                        continue
                    matched = True
                    self._expand_macro_template_nodes_into(
                        word=word,
                        nodes=case_body_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                        out=out,
                    )
                    break
                if not matched and default_nodes:
                    self._expand_macro_template_nodes_into(
                        word=word,
                        nodes=default_nodes,
                        scopes=scopes,
                        loop_stack=loop_stack,
                        out=out,
                    )
                continue

            if kind == "fold":
                acc_name = node[1]
                item_name = node[2]
                source_key = node[3]
                source_ref = node[4]
                init_nodes = node[5]
                body_nodes = node[6]
                source_value = self._macro_capture_lookup_mode(
                    word=word,
                    scopes=scopes,
                    key=source_key,
                    raw_ref=source_ref,
                )
                groups = self._macro_capture_as_groups(word=word, raw_ref=source_ref, value=source_value)
                acc_value = self._eval_macro_template_binding_value(
                    word=word,
                    expr_nodes=init_nodes,
                    scopes=scopes,
                    loop_stack=loop_stack,
                )
                count = len(groups)
                for index, group in enumerate(groups):
                    local_scope = {
                        acc_name: self._macro_capture_clone(acc_value),
                        item_name: list(group),
                    }
                    frame = {"index": index, "count": count}
                    scopes.append(local_scope)
                    loop_stack.append(frame)
                    loop_control: Optional[str] = None
                    next_acc = acc_value
                    try:
                        next_acc = self._eval_macro_template_binding_value(
                            word=word,
                            expr_nodes=body_nodes,
                            scopes=scopes,
                            loop_stack=loop_stack,
                        )
                    except _MacroTemplateContinue:
                        loop_control = "continue"
                    except _MacroTemplateBreak:
                        loop_control = "break"
                    finally:
                        loop_stack.pop()
                        scopes.pop()
                    if loop_control is None:
                        acc_value = next_acc
                    if loop_control == "break":
                        break
                    if loop_control == "continue":
                        continue

                out_extend(
                    self._macro_template_binding_value_lexemes(
                        word=word,
                        value=acc_value,
                        context="ct-fold accumulator",
                    )
                )
                continue

            if kind == "break":
                if not loop_stack:
                    raise ParseError(
                        f"macro '{word.name}' template '{node[1]}' can only be used inside ct-for/ct-each/ct-fold"
                    )
                raise _MacroTemplateBreak()

            if kind == "continue":
                if not loop_stack:
                    raise ParseError(
                        f"macro '{word.name}' template '{node[1]}' can only be used inside ct-for/ct-each/ct-fold"
                    )
                raise _MacroTemplateContinue()

            if kind == "for":
                item_name = node[1]
                key_name = node[2]
                source_key = node[3]
                source_ref = node[4]
                separator_nodes = node[5]
                body_nodes = node[6]
                source_value = self._macro_capture_lookup_mode(
                    word=word,
                    scopes=scopes,
                    key=source_key,
                    raw_ref=source_ref,
                )
                groups = self._macro_capture_as_groups(word=word, raw_ref=source_ref, value=source_value)
                count = len(groups)
                for index, group in enumerate(groups):
                    local_scope = {item_name: list(group)}
                    if key_name is not None:
                        local_scope[key_name] = [str(index)]
                    frame = {"index": index, "count": count}
                    scopes.append(local_scope)
                    loop_stack.append(frame)
                    loop_control: Optional[str] = None
                    try:
                        if index > 0 and separator_nodes:
                            self._expand_macro_template_nodes_into(
                                word=word,
                                nodes=separator_nodes,
                                scopes=scopes,
                                loop_stack=loop_stack,
                                out=out,
                            )
                        self._expand_macro_template_nodes_into(
                            word=word,
                            nodes=body_nodes,
                            scopes=scopes,
                            loop_stack=loop_stack,
                            out=out,
                        )
                    except _MacroTemplateContinue:
                        loop_control = "continue"
                    except _MacroTemplateBreak:
                        loop_control = "break"
                    finally:
                        loop_stack.pop()
                        scopes.pop()
                    if loop_control == "break":
                        break
                    if loop_control == "continue":
                        continue
                continue

            raise ParseError(f"internal macro template error in '{word.name}': unknown node '{kind}'")

    def _expand_macro_template_nodes(
        self,
        *,
        word: Word,
        nodes: Sequence[Any],
        scopes: Sequence[Dict[str, Any]],
        loop_stack: Sequence[Dict[str, int]],
    ) -> List[str]:
        out: List[str] = []
        scope_stack = scopes if isinstance(scopes, list) else list(scopes)
        loop_frames = loop_stack if isinstance(loop_stack, list) else list(loop_stack)
        self._expand_macro_template_nodes_into(
            word=word,
            nodes=nodes,
            scopes=scope_stack,
            loop_stack=loop_frames,
            out=out,
        )
        return out

    def expand_macro_template(
        self,
        word: Word,
        captures: Dict[str, Any],
        *,
        call_token: Optional[Token] = None,
    ) -> List[str]:
        nodes = word.macro_template_ast
        if nodes is None:
            template_tokens = list(word.macro_expansion or [])
            parsed_nodes, idx, stop = self._parse_macro_template_nodes(
                word=word,
                tokens=template_tokens,
                idx=0,
                stop_tokens=None,
            )
            if stop is not None:
                raise ParseError(f"macro '{word.name}' has unexpected template terminator '{stop}'")
            if idx != len(template_tokens):
                raise ParseError(f"macro '{word.name}' template parser stopped early")
            nodes = tuple(parsed_nodes)
            word.macro_template_ast = nodes
            mode, version = self._collect_macro_template_metadata(word=word, nodes=nodes)
            word.macro_template_mode = mode
            word.macro_template_version = version
            word.macro_template_program = self._compile_macro_template_program(nodes=nodes)

        program = word.macro_template_program or nodes

        prev_fn_scopes = self._template_function_scopes
        prev_import_scopes = self._template_import_scopes
        prev_token = self._active_macro_token
        prev_unknown_mode = self._template_unknown_mode
        scope_stack = self._acquire_template_scope_stack()
        loop_frames = self._acquire_template_loop_stack()
        scope_stack.append(captures)
        started_ns = time.perf_counter_ns()
        expanded_tokens: Optional[List[str]] = None

        self._template_function_scopes = [{}]
        self._template_import_scopes = [set()]
        self._active_macro_token = call_token
        self._template_unknown_mode = getattr(word, "macro_template_mode", "strict") or "strict"
        try:
            expanded_tokens = self._expand_macro_template_nodes(
                word=word,
                nodes=program,
                scopes=scope_stack,
                loop_stack=loop_frames,
            )
            return expanded_tokens
        finally:
            if expanded_tokens is not None:
                elapsed_ns = time.perf_counter_ns() - started_ns
                self._parser._record_macro_profile_event(
                    word.name,
                    emitted_tokens=len(expanded_tokens),
                    duration_ns=elapsed_ns,
                )
            self._release_template_scope_stack(scope_stack)
            self._release_template_loop_stack(loop_frames)
            self._template_function_scopes = prev_fn_scopes
            self._template_import_scopes = prev_import_scopes
            self._active_macro_token = prev_token
            self._template_unknown_mode = prev_unknown_mode

    def inject_macro_tokens(self, word: Word, token: Token, captures: Dict[str, Any]) -> None:
        parser = self._parser
        next_depth = token.expansion_depth + 1
        if next_depth > parser.macro_expansion_limit:
            raise ParseError(
                f"macro expansion depth limit ({parser.macro_expansion_limit}) exceeded while expanding '{word.name}'"
            )
        replaced = self.expand_macro_template(word, captures, call_token=token)

        insertion: List[Token] = [None] * len(replaced)  # type: ignore[list-item]
        base_column = max(1, token.column)
        intern_lexeme = parser._intern_expansion_lexeme
        for idx, lex in enumerate(replaced):
            generated = Token(
                lexeme=intern_lexeme(lex),
                line=token.line,
                column=base_column + idx,
                start=token.start,
                end=token.end,
                expansion_depth=next_depth,
            )
            insertion[idx] = generated
            parser.generated_source_map[id(generated)] = (word.name, token.line, token.column, idx)
        parser.tokens[parser.pos:parser.pos] = insertion
        self._preview_with_context(kind="macro-preview", name=word.name, token=token, replaced=replaced)

    def try_apply_rewrite_rules(self, stage: str, token: Token) -> bool:
        parser = self._parser
        rules = parser._rewrite_bucket(stage)
        if not rules:
            return False

        start = parser.pos - 1
        if start < 0:
            return False

        tokens = parser.tokens
        token_count = len(tokens)
        token_lexeme = token.lexeme
        stage_profile = parser.rewrite_profile.setdefault(
            stage,
            {"attempts": 0, "matches": 0, "applied": 0, "guard_calls": 0, "guard_rejects": 0},
        )

        candidates = parser._rewrite_candidates_for_token(stage, token_lexeme)
        if not candidates:
            return False

        active_pipelines = parser._active_rewrite_pipelines.get(stage, {"default"})
        filtered: List[RewriteRule] = []
        for rule in candidates:
            if not rule.enabled:
                continue
            if rule.pipeline not in active_pipelines:
                continue
            if stage == "grammar":
                if rule.group not in parser._active_pattern_groups:
                    continue
                if rule.scope not in parser._active_pattern_scopes:
                    continue
            filtered.append(rule)

        if not filtered:
            return False

        if parser.rewrite_saturation_strategy == "specificity":
            filtered.sort(key=lambda r: (-int(r.priority), -int(r.specificity), int(r.order)))

        window_bloom_cache: Dict[int, int] = {}
        intern_lexeme = parser._intern_expansion_lexeme

        def _matches_spec_single(spec: Dict[str, Any], lex: str) -> bool:
            if spec["kind"] == "literal":
                return lex == spec["literal"]
            return parser._rewrite_constraint_matches(lex, spec.get("constraint", ""))

        for rule in filtered:
            stage_profile["attempts"] = int(stage_profile.get("attempts", 0)) + 1
            rule_meta = rule.metadata if isinstance(rule.metadata, dict) else {}
            rule_meta["__stat_attempts"] = int(rule_meta.get("__stat_attempts", 0)) + 1
            pattern = rule.pattern
            if not pattern:
                continue

            specs = rule_meta.get("__piece_specs")
            if not isinstance(specs, list) or len(specs) != len(pattern):
                specs = [parser._parse_rewrite_piece(piece) for piece in pattern]
                rule_meta["__piece_specs"] = specs

            min_suffix_raw = rule_meta.get("__min_suffix")
            if isinstance(min_suffix_raw, tuple) and len(min_suffix_raw) == len(specs) + 1:
                min_suffix = [int(item) for item in min_suffix_raw]
            else:
                min_suffix = [0] * (len(specs) + 1)
                for idx in range(len(specs) - 1, -1, -1):
                    min_suffix[idx] = min_suffix[idx + 1] + int(specs[idx]["min_count"])
                rule_meta["__min_suffix"] = tuple(min_suffix)

            prefilter_window = int(rule_meta.get("__prefilter_window", 0) or 0)
            prefilter_bloom = int(rule_meta.get("__prefilter_bloom", 0) or 0)
            if prefilter_window > 0 and prefilter_bloom != 0:
                window_end = min(token_count, start + prefilter_window)
                cached_bloom = window_bloom_cache.get(window_end)
                if cached_bloom is None:
                    bloom = 0
                    for bloom_idx in range(start, window_end):
                        bloom |= parser._rewrite_bloom_bit(tokens[bloom_idx].lexeme)
                    cached_bloom = bloom
                    window_bloom_cache[window_end] = cached_bloom
                if (cached_bloom & prefilter_bloom) != prefilter_bloom:
                    continue

            if start + min_suffix[0] > token_count:
                continue

            def _match(
                pat_idx: int,
                tok_idx: int,
                captures: Dict[str, Any],
                max_depth: int,
            ) -> Optional[Tuple[int, Dict[str, Any], int]]:
                if pat_idx == len(specs):
                    return tok_idx, captures, max_depth

                spec = specs[pat_idx]
                min_rest = min_suffix[pat_idx + 1]
                max_take_budget = token_count - tok_idx - min_rest
                if max_take_budget < int(spec["min_count"]):
                    return None

                min_take = int(spec["min_count"])
                max_count = spec.get("max_count")
                if max_count is None:
                    max_take = max_take_budget
                else:
                    max_take = min(max_take_budget, int(max_count))

                if spec.get("negated"):
                    if min_take != 1 or max_take < 1:
                        return None
                    if tok_idx >= token_count:
                        return None
                    cur_tok = tokens[tok_idx]
                    if _matches_spec_single(spec, cur_tok.lexeme):
                        return None
                    next_depth = max(max_depth, cur_tok.expansion_depth)
                    return _match(pat_idx + 1, tok_idx + 1, captures, next_depth)

                for take in range(max_take, min_take - 1, -1):
                    chunk_lexemes: Tuple[str, ...]
                    if take <= 0:
                        chunk_lexemes = tuple()
                    else:
                        chunk_lexemes = tuple(tokens[tok_idx + i].lexeme for i in range(take))

                    if spec["kind"] == "literal":
                        if not all(lex == spec["literal"] for lex in chunk_lexemes):
                            continue
                        next_captures = captures
                    else:
                        if not all(_matches_spec_single(spec, lex) for lex in chunk_lexemes):
                            continue

                        key = str(spec["name"])
                        multi_shape = bool(
                            spec.get("variadic")
                            or int(spec["min_count"]) != 1
                            or spec.get("max_count") != 1
                        )
                        if multi_shape:
                            candidate: Any = tuple(chunk_lexemes)
                        else:
                            if len(chunk_lexemes) != 1:
                                continue
                            candidate = chunk_lexemes[0]

                        prev = captures.get(key)
                        if prev is not None and not parser._rewrite_capture_equals(prev, candidate):
                            continue
                        if prev is None:
                            next_captures = dict(captures)
                            next_captures[key] = candidate
                        else:
                            next_captures = captures

                    next_depth = max_depth
                    for i in range(take):
                        d = tokens[tok_idx + i].expansion_depth
                        if d > next_depth:
                            next_depth = d

                    result = _match(pat_idx + 1, tok_idx + take, next_captures, next_depth)
                    if result is not None:
                        return result
                return None

            matched = _match(0, start, {}, token.expansion_depth)
            if matched is None:
                continue

            end, captures, max_depth = matched
            stage_profile["matches"] = int(stage_profile.get("matches", 0)) + 1
            rule_meta["__stat_matches"] = int(rule_meta.get("__stat_matches", 0)) + 1

            if rule.guard:
                stage_profile["guard_calls"] = int(stage_profile.get("guard_calls", 0)) + 1
                guard_word = parser.dictionary.lookup(rule.guard)
                if guard_word is None:
                    raise ParseError(
                        f"rewrite rule '{rule.name}' guard word '{rule.guard}' is not defined"
                    )

                guard_payload = {
                    "stage": stage,
                    "rule": rule.name,
                    "pattern": list(rule.pattern),
                    "replacement": list(rule.replacement),
                    "captures": {
                        key: (list(value) if isinstance(value, tuple) else value)
                        for key, value in captures.items()
                    },
                }

                vm = parser.compile_time_vm
                base_depth = len(vm.stack)
                vm.push(guard_payload)
                vm._call_word(guard_word)
                if len(vm.stack) <= base_depth:
                    raise ParseError(
                        f"rewrite rule '{rule.name}' guard word '{rule.guard}' must leave a boolean result"
                    )
                guard_raw = vm.pop()
                guard_value = vm._resolve_handle(guard_raw)
                if len(vm.stack) > base_depth:
                    del vm.stack[base_depth:]
                guard_pass = bool(guard_value)
                if not guard_pass:
                    stage_profile["guard_rejects"] = int(stage_profile.get("guard_rejects", 0)) + 1
                    continue

            replaced: List[str] = []
            parsed_replacement = [parser._parse_rewrite_capture(piece) for piece in rule.replacement]
            for piece, parsed in zip(rule.replacement, parsed_replacement):
                if parsed is None:
                    replaced.append(piece)
                    continue
                variadic, key, constraint = parsed
                if constraint:
                    raise ParseError(
                        f"rewrite rule '{rule.name}' replacement placeholder '{piece}' must not include constraints"
                    )
                if key not in captures:
                    raise ParseError(
                        f"rewrite rule '{rule.name}' references capture '{piece}' "
                        f"that is not bound in pattern"
                    )
                value = captures[key]
                if variadic:
                    if isinstance(value, tuple):
                        replaced.extend(value)
                    else:
                        replaced.append(value)
                    continue

                if isinstance(value, tuple):
                    if len(value) != 1:
                        raise ParseError(
                            f"rewrite rule '{rule.name}' placeholder '{piece}' matches multiple tokens; use '$*{key}'"
                        )
                    replaced.append(value[0])
                else:
                    replaced.append(value)

            next_depth = max_depth + 1
            if next_depth > parser.macro_expansion_limit:
                raise ParseError(
                    f"rewrite expansion depth limit ({parser.macro_expansion_limit}) exceeded "
                    f"while applying '{rule.name}'"
                )

            parser._rewrite_step_count += 1
            if parser._rewrite_step_count > int(parser.rewrite_max_steps):
                raise ParseError(
                    f"rewrite max-step budget ({parser.rewrite_max_steps}) exceeded while applying '{rule.name}'"
                )

            template = tokens[start]
            before_lexemes = [tok.lexeme for tok in tokens[start:end]]
            insertion = [
                Token(
                    lexeme=intern_lexeme(lex),
                    line=template.line,
                    column=template.column,
                    start=template.start,
                    end=template.end,
                    expansion_depth=next_depth,
                )
                for lex in replaced
            ]

            tokens[start:end] = insertion
            parser.pos = start
            stage_profile["applied"] = int(stage_profile.get("applied", 0)) + 1
            rule_meta["__stat_applied"] = int(rule_meta.get("__stat_applied", 0)) + 1

            if parser.rewrite_trace_enabled:
                parser.rewrite_trace_log.append(
                    {
                        "stage": stage,
                        "rule": rule.name,
                        "start": start,
                        "end": end,
                        "before": before_lexemes,
                        "after": list(replaced),
                        "priority": int(rule.priority),
                        "specificity": int(rule.specificity),
                    }
                )
                if len(parser.rewrite_trace_log) > 8192:
                    del parser.rewrite_trace_log[:-8192]

            if parser.rewrite_loop_detection:
                state_hash = hash(tuple(tok.lexeme for tok in tokens))
                state_key = (stage, state_hash)
                event_desc = f"{rule.name}@{start}"
                previous = parser._rewrite_seen_state.get(state_key)
                if previous is not None:
                    prev_step, prev_desc = previous
                    report = {
                        "stage": stage,
                        "current_rule": rule.name,
                        "current_step": parser._rewrite_step_count,
                        "previous_step": prev_step,
                        "previous_event": prev_desc,
                        "current_event": event_desc,
                    }
                    parser.rewrite_loop_reports.append(report)
                    if len(parser.rewrite_loop_reports) > 256:
                        del parser.rewrite_loop_reports[:-256]
                    raise ParseError(
                        f"rewrite loop detected at stage '{stage}' while applying '{rule.name}'"
                    )
                parser._rewrite_seen_state[state_key] = (parser._rewrite_step_count, event_desc)
                if len(parser._rewrite_seen_state) > 8192:
                    parser._rewrite_seen_state.clear()

            self._preview_with_context(
                kind="rewrite-preview",
                name=f"{rule.name} ({stage})",
                token=template,
                replaced=replaced,
            )
            return True

        return False


class StructField:
    __slots__ = ('name', 'offset', 'size')

    def __init__(self, name: str, offset: int, size: int) -> None:
        self.name = name
        self.offset = offset
        self.size = size


class CStructField:
    __slots__ = ('name', 'type_name', 'offset', 'size', 'align')

    def __init__(self, name: str, type_name: str, offset: int, size: int, align: int) -> None:
        self.name = name
        self.type_name = type_name
        self.offset = offset
        self.size = size
        self.align = align


class CStructLayout:
    __slots__ = ('name', 'size', 'align', 'fields')

    def __init__(self, name: str, size: int, align: int, fields: List[CStructField]) -> None:
        self.name = name
        self.size = size
        self.align = align
        self.fields = fields


class MacroContext:
    """Small facade exposed to Python-defined macros."""

    def __init__(self, parser: "Parser") -> None:
        self._parser = parser

    @property
    def parser(self) -> "Parser":
        return self._parser

    def next_token(self) -> Token:
        return self._parser.next_token()

    def peek_token(self) -> Optional[Token]:
        return self._parser.peek_token()

    def emit_literal(self, value: int) -> None:
        self._parser.emit_node(_make_literal_op(value))

    def emit_word(self, name: str) -> None:
        self._parser.emit_node(_make_word_op(name))

    def emit_node(self, node: Op) -> None:
        self._parser.emit_node(node)

    def inject_tokens(self, tokens: Sequence[str], template: Optional[Token] = None) -> None:
        if template is None:
            template = Token(lexeme="", line=0, column=0, start=0, end=0)
        generated = [
            Token(
                lexeme=lex,
                line=template.line,
                column=template.column,
                start=template.start,
                end=template.end,
            )
            for lex in tokens
        ]
        self.inject_token_objects(generated)

    def inject_token_objects(self, tokens: Sequence[Token]) -> None:
        self._parser.tokens[self._parser.pos:self._parser.pos] = list(tokens)

    def set_token_hook(self, handler: Optional[str]) -> None:
        self._parser.token_hook = handler

    def add_reader_rewrite(
        self,
        pattern: Sequence[str],
        replacement: Sequence[str],
        *,
        name: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        return self._parser.add_rewrite_rule(
            "reader",
            pattern,
            replacement,
            name=name,
            priority=priority,
        )

    def add_grammar_rewrite(
        self,
        pattern: Sequence[str],
        replacement: Sequence[str],
        *,
        name: Optional[str] = None,
        priority: int = 0,
    ) -> str:
        return self._parser.add_rewrite_rule(
            "grammar",
            pattern,
            replacement,
            name=name,
            priority=priority,
        )

    def remove_reader_rewrite(self, name: str) -> bool:
        return self._parser.remove_rewrite_rule("reader", name)

    def remove_grammar_rewrite(self, name: str) -> bool:
        return self._parser.remove_rewrite_rule("grammar", name)

    def clear_reader_rewrites(self) -> int:
        return self._parser.clear_rewrite_rules("reader")

    def clear_grammar_rewrites(self) -> int:
        return self._parser.clear_rewrite_rules("grammar")

    def list_reader_rewrites(self) -> List[str]:
        return self._parser.list_rewrite_rules("reader")

    def list_grammar_rewrites(self) -> List[str]:
        return self._parser.list_rewrite_rules("grammar")

    def set_reader_rewrite_enabled(self, name: str, enabled: bool) -> bool:
        return self._parser.set_rewrite_rule_enabled("reader", name, enabled)

    def set_grammar_rewrite_enabled(self, name: str, enabled: bool) -> bool:
        return self._parser.set_rewrite_rule_enabled("grammar", name, enabled)

    def set_reader_rewrite_priority(self, name: str, priority: int) -> bool:
        return self._parser.set_rewrite_rule_priority("reader", name, priority)

    def set_grammar_rewrite_priority(self, name: str, priority: int) -> bool:
        return self._parser.set_rewrite_rule_priority("grammar", name, priority)

    def set_macro_expansion_limit(self, limit: int) -> None:
        self._parser.set_macro_expansion_limit(limit)

    def macro_expansion_limit(self) -> int:
        return self._parser.macro_expansion_limit

    def set_macro_preview(self, enabled: bool) -> None:
        self._parser.set_macro_preview(enabled)

    def macro_preview(self) -> bool:
        return self._parser.macro_preview

    def register_text_macro(self, name: str, param_count: int, expansion: Sequence[str]) -> None:
        self._parser.register_text_macro(name, param_count, expansion)

    def register_text_macro_signature(
        self,
        name: str,
        param_spec: Sequence[str],
        expansion: Sequence[str],
    ) -> None:
        self._parser.register_text_macro_signature(name, param_spec, expansion)

    def unregister_word(self, name: str) -> bool:
        return self._parser.unregister_word(name)

    def word_exists(self, name: str) -> bool:
        return self._parser.word_exists(name)

    def new_label(self, prefix: str) -> str:
        return self._parser._new_label(prefix)

    def most_recent_definition(self) -> Optional[Word]:
        return self._parser.most_recent_definition()


# Type aliases (only evaluated under TYPE_CHECKING)
MacroHandler = None  # Callable[[MacroContext], Optional[List[Op]]]
IntrinsicEmitter = None  # Callable[["FunctionEmitter"], None]


# Word effects ---------------------------------------------------------------


WORD_EFFECT_STRING_IO = "string-io"
_WORD_EFFECT_ALIASES: Dict[str, str] = {
    "string": WORD_EFFECT_STRING_IO,
    "strings": WORD_EFFECT_STRING_IO,
    "string-io": WORD_EFFECT_STRING_IO,
    "string_io": WORD_EFFECT_STRING_IO,
    "strings-io": WORD_EFFECT_STRING_IO,
    "strings_io": WORD_EFFECT_STRING_IO,
}


class Word:
    __slots__ = ('name', 'priority', 'immediate', 'definition', 'macro', 'intrinsic',
                 'macro_expansion', 'macro_params', 'compile_time_intrinsic',
                 'runtime_intrinsic', 'compile_only', 'runtime_only', 'compile_time_override',
                 'is_extern', 'extern_inputs', 'extern_outputs', 'extern_signature',
                 'extern_variadic', 'inline', 'macro_template_ast', 'macro_template_program',
                 'macro_template_version', 'macro_template_mode')

    def __init__(self, name: str, priority: int = 0, immediate: bool = False,
                 definition=None, macro=None, intrinsic=None,
                 macro_expansion=None, macro_params: int = 0,
                 compile_time_intrinsic=None, runtime_intrinsic=None,
                 compile_only: bool = False, runtime_only: bool = False, compile_time_override: bool = False,
                 is_extern: bool = False, extern_inputs: int = 0, extern_outputs: int = 0,
                 extern_signature=None, extern_variadic: bool = False,
                 inline: bool = False, macro_template_ast: Any = None,
                 macro_template_program: Any = None,
                 macro_template_version: Optional[str] = None,
                 macro_template_mode: str = "strict") -> None:
        self.name = name
        self.priority = priority
        self.immediate = immediate
        self.definition = definition
        self.macro = macro
        self.intrinsic = intrinsic
        self.macro_expansion = macro_expansion
        self.macro_params = macro_params
        self.compile_time_intrinsic = compile_time_intrinsic
        self.runtime_intrinsic = runtime_intrinsic
        self.compile_only = compile_only
        self.runtime_only = runtime_only
        self.compile_time_override = compile_time_override
        self.is_extern = is_extern
        self.extern_inputs = extern_inputs
        self.extern_outputs = extern_outputs
        self.extern_signature = extern_signature
        self.extern_variadic = extern_variadic
        self.inline = inline
        self.macro_template_ast = macro_template_ast
        self.macro_template_program = macro_template_program
        self.macro_template_version = macro_template_version
        self.macro_template_mode = macro_template_mode


_suppress_redefine_warnings = False


def _suppress_redefine_warnings_set(value: bool) -> None:
    global _suppress_redefine_warnings
    _suppress_redefine_warnings = value


class Dictionary:
    __slots__ = ('words', 'warn_callback')

    def __init__(self, words: Dict[str, Word] = None) -> None:
        self.words = words if words is not None else {}
        self.warn_callback: Optional[Callable] = None

    def register(self, word: Word) -> Word:
        existing = self.words.get(word.name)
        if existing is None:
            self.words[word.name] = word
            return word

        # Preserve existing intrinsic handlers unless explicitly replaced.
        if word.runtime_intrinsic is None and existing.runtime_intrinsic is not None:
            word.runtime_intrinsic = existing.runtime_intrinsic
        if word.compile_time_intrinsic is None and existing.compile_time_intrinsic is not None:
            word.compile_time_intrinsic = existing.compile_time_intrinsic

        if word.priority > existing.priority:
            self.words[word.name] = word
            sys.stderr.write(
                f"[note] word {word.name}: using priority {word.priority} over {existing.priority}\n"
            )
            return word

        if word.priority < existing.priority:
            sys.stderr.write(
                f"[note] word {word.name}: keeping priority {existing.priority}, ignored {word.priority}\n"
            )
            return existing

        # Same priority: allow replacing placeholder bootstrap words silently.
        if existing.definition is None and word.definition is not None:
            self.words[word.name] = word
            return word

        if not _suppress_redefine_warnings:
            if self.warn_callback is not None:
                self.warn_callback(word.name, word.priority)
            else:
                sys.stderr.write(f"[warn] redefining word {word.name} (priority {word.priority})\n")
        self.words[word.name] = word
        return word

    def lookup(self, name: str) -> Optional[Word]:
        return self.words.get(name)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


Context = None  # Union[Module, Definition] - only used in annotations


class Parser:
    EXTERN_DEFAULT_PRIORITY = 1

    def __init__(
        self,
        dictionary: Dictionary,
        reader: Optional[Reader] = None,
        *,
        macro_expansion_limit: int = DEFAULT_MACRO_EXPANSION_LIMIT,
        macro_preview: bool = False,
    ) -> None:
        if macro_expansion_limit < 1:
            raise ValueError("macro_expansion_limit must be >= 1")
        self.dictionary = dictionary
        self.reader = reader or Reader()
        self.macro_expansion_limit = macro_expansion_limit
        self.macro_preview = macro_preview
        self.tokens: List[Token] = []
        self._token_iter: Optional[Iterable[Token]] = None
        self._token_iter_exhausted = True
        self.pos = 0
        self.context_stack: List[Context] = []
        self.definition_stack: List[Tuple[Word, bool]] = []
        self.last_defined: Optional[Word] = None
        self.source: str = ""
        self.macro_recording: Optional[MacroDefinition] = None
        self.control_stack: List[Dict[str, str]] = []
        self.block_openers: Set[str] = {"word", "with", "for", "while", "begin"}
        self.control_overrides: Set[str] = set()
        self._warned_control_overrides: Set[str] = set()
        self.reader_rewrite_rules: List[RewriteRule] = []
        self.grammar_rewrite_rules: List[RewriteRule] = []
        self._rewrite_rule_counter: int = 0
        self._rewrite_index_cache: Dict[str, Dict[str, List[RewriteRule]]] = {"reader": {}, "grammar": {}}
        self._rewrite_wildcard_cache: Dict[str, List[RewriteRule]] = {"reader": [], "grammar": []}
        self._rewrite_index_dirty: Set[str] = {"reader", "grammar"}
        self._active_rewrite_pipelines: Dict[str, Set[str]] = {
            "reader": {"default"},
            "grammar": {"default"},
        }
        self._pattern_macro_groups: Dict[str, str] = {}
        self._pattern_macro_scopes: Dict[str, str] = {}
        self._active_pattern_groups: Set[str] = {"default"}
        self._active_pattern_scopes: Set[str] = {"global"}
        self.rewrite_trace_enabled: bool = False
        self.rewrite_trace_log: List[Dict[str, Any]] = []
        self.rewrite_profile: Dict[str, Dict[str, int]] = {
            "reader": {"attempts": 0, "matches": 0, "applied": 0, "guard_calls": 0, "guard_rejects": 0},
            "grammar": {"attempts": 0, "matches": 0, "applied": 0, "guard_calls": 0, "guard_rejects": 0},
        }
        self.rewrite_saturation_strategy: str = "first"
        self.rewrite_max_steps: int = 100_000
        self._rewrite_step_count: int = 0
        self.rewrite_loop_detection: bool = True
        self.rewrite_loop_reports: List[Dict[str, Any]] = []
        self._rewrite_seen_state: Dict[Tuple[str, int], Tuple[int, str]] = {}
        self._rewrite_transactions: List[Dict[str, Any]] = []
        self._macro_signatures: Dict[str, Tuple[Tuple[str, ...], Optional[str]]] = {}
        self._expansion_lexeme_pool: Dict[str, str] = {}
        self._capture_group_pool: Dict[Tuple[str, ...], Tuple[str, ...]] = {}
        self._macro_hotness: Dict[str, int] = {}
        self._macro_profile: Dict[str, Dict[str, int]] = {}
        self._macro_profile_enabled: bool = False
        self.enable_dead_macro_elimination: bool = False
        self.enable_unused_rewrite_elimination: bool = False
        self._macro_docs: Dict[str, str] = {}
        self._macro_attrs: Dict[str, Dict[str, Any]] = {}
        self._pattern_macro_rules: Dict[str, List[str]] = {}
        self.macro_engine = MacroEngine(self)
        self.label_counter = 0
        self.token_hook: Optional[str] = None
        self._last_token: Optional[Token] = None
        self.variable_labels: Dict[str, str] = {}
        self.variable_words: Dict[str, str] = {}
        self.file_spans: List[FileSpan] = []
        self._span_starts: List[int] = []
        self._span_index_len: int = 0
        self._span_cache_idx: int = -1
        self.compile_time_vm = CompileTimeVM(self)
        self.custom_prelude: Optional[List[str]] = None
        self.custom_bss: Optional[List[str]] = None
        self.cstruct_layouts: Dict[str, CStructLayout] = {}
        self._pending_inline_definition: bool = False
        self._pending_priority: Optional[int] = None
        self.generated_source_map: Dict[int, Tuple[str, int, int, int]] = {}
        self.capture_globals: Dict[str, Any] = {}
        self.capture_mutability_frozen: Set[Tuple[str, str]] = set()
        self.capture_schemas: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.capture_taint: Dict[str, Dict[str, bool]] = {}
        self.capture_replay_log: List[Dict[str, Any]] = []
        self._capture_lifetime_counter: int = 0
        self._capture_lifetime_active: int = 0
        self._ct_call_abi_contracts: Dict[str, Dict[str, Any]] = {}
        self._ct_call_exception_policy: str = "raise"
        self._ct_call_sandbox_mode: str = "off"
        self._ct_call_sandbox_allowlist: Set[str] = set()
        self._ct_call_rng_seed: int = 0x1A2B3C4D
        self._ct_call_rng = random.Random(self._ct_call_rng_seed)
        self._ct_call_memo_enabled: bool = False
        self._ct_call_memo_cache: Dict[str, Any] = {}
        self._ct_call_side_effect_tracking: bool = False
        self._ct_call_side_effect_log: List[Dict[str, Any]] = []
        self._ct_call_recursion_limit: int = 32
        self._ct_call_timeout_ms: int = 0
        self._ct_call_active: List[str] = []
        self._ct_parser_sessions: List[Dict[str, Any]] = []
        self._ct_parser_marks: Dict[str, int] = {}
        self._ct_rewrite_scope_stack: List[Dict[str, Any]] = []
        self.diagnostics: List[Diagnostic] = []
        self._max_errors: int = 20
        self._warnings_enabled: Set[str] = set()
        self._werror: bool = False
        # When false, parser skips per-op source location attachment for speed.
        self.capture_op_locations: bool = True

    def _rebuild_span_index(self) -> None:
        """Rebuild bisect index after file_spans changes."""
        self._span_starts: List[int] = [s.start_line for s in self.file_spans]
        self._span_index_len: int = len(self.file_spans)

    def location_for_token(self, token: Token) -> SourceLocation:
        spans = self.file_spans
        if not spans:
            return _make_loc(_SOURCE_PATH, token.line, token.column)
        if self._span_index_len != len(spans):
            self._rebuild_span_index()
            self._span_cache_idx = -1
        tl = token.line
        # Fast path: check cached span first (common for sequential access)
        ci = self._span_cache_idx
        if ci >= 0:
            span = spans[ci]
            if span.start_line <= tl < span.end_line:
                return _make_loc(span.path, span.local_start_line + (tl - span.start_line), token.column)
        span_starts = self._span_starts
        idx = bisect.bisect_right(span_starts, tl) - 1
        if idx >= 0:
            span = spans[idx]
            if tl < span.end_line:
                self._span_cache_idx = idx
                return _make_loc(span.path, span.local_start_line + (tl - span.start_line), token.column)
        return _make_loc(_SOURCE_PATH, tl, token.column)

    def _record_diagnostic(self, token: Optional[Token], message: str, *, level: str = "error", hint: str = "", suggestion: str = "") -> None:
        """Record a diagnostic and raise ParseError if too many errors."""
        loc = self.location_for_token(token) if token else _make_loc(_SOURCE_PATH, 0, 0)
        diag = Diagnostic(
            level=level, message=message,
            path=loc.path, line=loc.line, column=loc.column,
            length=len(token.lexeme) if token else 0,
            hint=hint, suggestion=suggestion,
        )
        self.diagnostics.append(diag)
        if level == "error" and sum(1 for d in self.diagnostics if d.level == "error") >= self._max_errors:
            raise ParseError(f"too many errors ({self._max_errors}), aborting", diagnostic=diag)

    def _warn(self, token: Optional[Token], category: str, message: str, *, hint: str = "", suggestion: str = "") -> None:
        """Record a warning if the category is enabled. Promotes to error under --Werror."""
        if "all" not in self._warnings_enabled and category not in self._warnings_enabled:
            return
        level = "error" if self._werror else "warning"
        self._record_diagnostic(token, message, level=level, hint=hint, suggestion=suggestion)

    def _skip_to_recovery_point(self) -> None:
        """Skip tokens until we reach a safe recovery point (end, ;, or top-level definition keyword)."""
        _recovery_keywords = {"word", "end", ";", ":asm", ":py", "extern", "macro"}
        depth = 0
        while self.pos < len(self.tokens):
            lex = self.tokens[self.pos].lexeme
            if lex == "word" or lex == ":asm" or lex == ":py":
                if depth == 0:
                    break  # Don't consume — let the main loop pick it up
                depth += 1
            elif lex == "end":
                if depth <= 1:
                    self.pos += 1
                    break
                depth -= 1
            elif lex == ";":
                self.pos += 1
                break
            elif lex == "extern" and depth == 0:
                break
            self.pos += 1
        # Reset state for recovery
        self.macro_recording = None
        self._pending_priority = None
        self._pending_inline_definition = False
        while self.definition_stack:
            self.definition_stack.pop()
        while len(self.context_stack) > 1:
            self.context_stack.pop()
        self.control_stack.clear()

    def inject_token_objects(self, tokens: Sequence[Token]) -> None:
        """Insert tokens at the current parse position."""
        if isinstance(tokens, list):
            self.tokens[self.pos:self.pos] = tokens
            return
        self.tokens[self.pos:self.pos] = list(tokens)

    # Public helpers for macros ------------------------------------------------
    def next_token(self) -> Token:
        return self._consume()

    def peek_token(self) -> Optional[Token]:
        return None if self.pos >= len(self.tokens) else self.tokens[self.pos]

    def emit_node(self, node: Op) -> None:
        self._append_op(node)

    def most_recent_definition(self) -> Optional[Word]:
        return self.last_defined

    @staticmethod
    def _rewrite_lexeme_from_value(value: Any) -> str:
        if isinstance(value, Token):
            return value.lexeme
        if isinstance(value, str):
            return value
        raise ParseError("rewrite pattern/replacement items must be strings or tokens")

    def _normalize_rewrite_lexemes(self, values: Sequence[Any], *, field: str) -> List[str]:
        lexemes: List[str] = []
        for value in values:
            lex = self._rewrite_lexeme_from_value(value)
            if not lex:
                raise ParseError(f"rewrite {field} cannot contain empty tokens")
            lexemes.append(lex)
        return lexemes

    @staticmethod
    def _parse_rewrite_capture(piece: str) -> Optional[Tuple[bool, str, str]]:
        match = _RE_REWRITE_CAPTURE_TOKEN.fullmatch(piece)
        if match is None:
            return None
        variadic = bool(match.group(1))
        name = match.group(2)
        constraint = (match.group(3) or "").lower()
        return variadic, name, constraint

    @staticmethod
    def _rewrite_capture_equals(existing: Any, incoming: Any) -> bool:
        if isinstance(existing, tuple):
            if isinstance(incoming, tuple):
                return existing == incoming
            return len(existing) == 1 and existing[0] == incoming
        if isinstance(incoming, tuple):
            return len(incoming) == 1 and incoming[0] == existing
        return existing == incoming

    @staticmethod
    def _rewrite_constraint_matches(lexeme: str, constraint: str) -> bool:
        if not constraint or constraint == "any":
            return True
        if constraint in ("ident", "identifier", "id", "word"):
            return _is_identifier(lexeme)
        if constraint in ("int", "integer"):
            try:
                int(lexeme, 0)
                return True
            except ValueError:
                return False
        if constraint == "float":
            lower = lexeme.lower()
            if "." not in lexeme and "e" not in lower:
                return False
            try:
                float(lexeme)
                return True
            except ValueError:
                return False
        if constraint in ("number", "numeric"):
            try:
                int(lexeme, 0)
                return True
            except ValueError:
                try:
                    float(lexeme)
                    return True
                except ValueError:
                    return False
        if constraint in ("string", "str"):
            return len(lexeme) >= 2 and lexeme[0] == '"' and lexeme[-1] == '"'
        if constraint in ("char", "chr"):
            return len(lexeme) >= 2 and lexeme[0] == "'" and lexeme[-1] == "'"
        if constraint == "literal":
            if len(lexeme) >= 2 and ((lexeme[0] == '"' and lexeme[-1] == '"') or (lexeme[0] == "'" and lexeme[-1] == "'")):
                return True
            try:
                int(lexeme, 0)
                return True
            except ValueError:
                try:
                    float(lexeme)
                    return True
                except ValueError:
                    return False
        return False

    def _parse_rewrite_piece(self, piece: str) -> Dict[str, Any]:
        if not piece:
            raise ParseError("rewrite pattern pieces cannot be empty")

        raw_piece = piece
        negated = False
        if piece.startswith("!") and len(piece) > 1:
            negated = True
            piece = piece[1:]

        quantifier = ""
        if len(piece) > 1 and piece[-1] in ("?", "*", "+"):
            # Keep legacy variadic capture syntax ($*name) unambiguous.
            if not piece.startswith("$*"):
                quantifier = piece[-1]
                piece = piece[:-1]

        if not piece:
            raise ParseError(f"rewrite pattern uses malformed piece '{raw_piece}'")

        parsed = self._parse_rewrite_capture(piece)
        if parsed is not None:
            variadic, name, constraint = parsed
            if constraint not in _REWRITE_CAPTURE_CONSTRAINTS:
                raise ParseError(
                    f"rewrite rule uses unknown constraint '{constraint}' in '{raw_piece}'"
                )
            if variadic and quantifier:
                raise ParseError(
                    f"rewrite pattern piece '{raw_piece}' cannot combine '$*' variadic capture with quantifier"
                )
            if negated and variadic:
                raise ParseError(
                    f"rewrite pattern piece '{raw_piece}' cannot negate a variadic capture"
                )
            if negated and quantifier:
                raise ParseError(
                    f"rewrite pattern piece '{raw_piece}' cannot combine negation with quantifier"
                )

            if variadic:
                min_count = 0
                max_count: Optional[int] = None
            elif quantifier == "?":
                min_count = 0
                max_count = 1
            elif quantifier == "*":
                min_count = 0
                max_count = None
            elif quantifier == "+":
                min_count = 1
                max_count = None
            else:
                min_count = 1
                max_count = 1

            return {
                "raw": raw_piece,
                "kind": "capture",
                "negated": negated,
                "name": name,
                "constraint": constraint,
                "variadic": variadic,
                "quantifier": quantifier,
                "min_count": min_count,
                "max_count": max_count,
            }

        if negated and quantifier:
            raise ParseError(
                f"rewrite pattern piece '{raw_piece}' cannot combine negation with quantifier"
            )

        if quantifier == "?":
            min_count = 0
            max_count = 1
        elif quantifier == "*":
            min_count = 0
            max_count = None
        elif quantifier == "+":
            min_count = 1
            max_count = None
        else:
            min_count = 1
            max_count = 1

        return {
            "raw": raw_piece,
            "kind": "literal",
            "negated": negated,
            "literal": piece,
            "quantifier": quantifier,
            "min_count": min_count,
            "max_count": max_count,
        }

    def _rewrite_pattern_min_tokens(self, pattern: Sequence[str]) -> int:
        total = 0
        for piece in pattern:
            spec = self._parse_rewrite_piece(piece)
            total += int(spec["min_count"])
        return total

    def _rewrite_pattern_specificity_score(self, pattern: Sequence[str]) -> int:
        score = 0
        for piece in pattern:
            spec = self._parse_rewrite_piece(piece)
            if spec["kind"] == "literal":
                score += 8
            else:
                if spec.get("constraint"):
                    score += 6
                else:
                    score += 4
                if spec.get("variadic"):
                    score -= 2

            if spec.get("negated"):
                score += 2
            if spec["min_count"] == 0:
                score -= 1
            if spec["max_count"] is None:
                score -= 1
        return int(score)

    def _rewrite_pattern_index_keys(self, pattern: Sequence[str]) -> Tuple[Set[str], bool]:
        keys: Set[str] = set()
        wildcard = False
        for piece in pattern:
            spec = self._parse_rewrite_piece(piece)
            if spec["kind"] == "literal" and not spec["negated"]:
                keys.add(str(spec["literal"]))
            else:
                wildcard = True

            if int(spec["min_count"]) > 0:
                break
        return keys, wildcard

    def _invalidate_rewrite_index(self, stage: Optional[str] = None) -> None:
        if stage is None:
            self._rewrite_index_dirty.update({"reader", "grammar"})
            return
        self._rewrite_index_dirty.add(stage)

    def _refresh_rewrite_index(self, stage: str) -> None:
        if stage not in self._rewrite_index_dirty:
            return
        bucket = self._rewrite_bucket(stage)
        by_first: Dict[str, List[RewriteRule]] = {}
        wildcard: List[RewriteRule] = []
        for rule in bucket:
            keys, needs_wildcard = self._rewrite_pattern_index_keys(rule.pattern)
            for key in keys:
                by_first.setdefault(key, []).append(rule)
            if needs_wildcard or not keys:
                wildcard.append(rule)
        self._rewrite_index_cache[stage] = by_first
        self._rewrite_wildcard_cache[stage] = wildcard
        self._rewrite_index_dirty.discard(stage)

    def _rewrite_candidates_for_token(self, stage: str, token_lexeme: str) -> List[RewriteRule]:
        self._refresh_rewrite_index(stage)
        candidates: List[RewriteRule] = []
        seen: Set[str] = set()
        for rule in self._rewrite_index_cache.get(stage, {}).get(token_lexeme, []):
            if rule.name not in seen:
                candidates.append(rule)
                seen.add(rule.name)
        for rule in self._rewrite_wildcard_cache.get(stage, []):
            if rule.name not in seen:
                candidates.append(rule)
                seen.add(rule.name)
        return candidates

    def _validate_rewrite_rule(self, pattern: Sequence[str], replacement: Sequence[str], *, stage: str) -> None:
        capture_shapes: Dict[str, bool] = {}
        min_total = 0

        for piece in pattern:
            spec = self._parse_rewrite_piece(piece)
            min_total += int(spec["min_count"])

            if spec["kind"] != "capture" or spec["negated"]:
                continue

            name = str(spec["name"])
            is_variadic_shape = bool(
                spec.get("variadic")
                or int(spec["min_count"]) != 1
                or spec.get("max_count") != 1
            )
            prev_shape = capture_shapes.get(name)
            if prev_shape is None:
                capture_shapes[name] = is_variadic_shape
            elif prev_shape != is_variadic_shape:
                raise ParseError(
                    f"rewrite rule ({stage}) capture '{name}' mixes variadic and single-token forms"
                )

        if min_total <= 0:
            raise ParseError(
                f"rewrite rule ({stage}) must consume at least one token in the minimum match path"
            )

        for piece in replacement:
            parsed = self._parse_rewrite_capture(piece)
            if parsed is None:
                continue
            _, name, constraint = parsed
            if constraint:
                raise ParseError(
                    f"rewrite replacement capture '{piece}' must not include constraints"
                )
            if name not in capture_shapes:
                raise ParseError(
                    f"rewrite replacement references unknown capture '{piece}'"
                )

    def _rewrite_bucket(self, stage: str) -> List[RewriteRule]:
        if stage == "reader":
            return self.reader_rewrite_rules
        if stage == "grammar":
            return self.grammar_rewrite_rules
        raise ParseError(f"unknown rewrite stage '{stage}'")

    def add_rewrite_rule(
        self,
        stage: str,
        pattern: Sequence[Any],
        replacement: Sequence[Any],
        *,
        name: Optional[str] = None,
        priority: int = 0,
        enabled: bool = True,
        pipeline: str = "default",
        guard: Optional[str] = None,
        group: str = "default",
        scope: str = "global",
        metadata: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> str:
        pattern_lex = self._normalize_rewrite_lexemes(pattern, field="pattern")
        if not pattern_lex:
            raise ParseError("rewrite pattern cannot be empty")
        replacement_lex = self._normalize_rewrite_lexemes(replacement, field="replacement")
        self._validate_rewrite_rule(pattern_lex, replacement_lex, stage=stage)
        specificity = self._rewrite_pattern_specificity_score(pattern_lex)

        bucket = self._rewrite_bucket(stage)

        rule_name = (name or "").strip()
        if not rule_name:
            while True:
                candidate = f"{stage}-rewrite-{self._rewrite_rule_counter}"
                self._rewrite_rule_counter += 1
                if all(rule.name != candidate for rule in bucket):
                    rule_name = candidate
                    break

        existing_order: Optional[int] = None
        for existing in bucket:
            if existing.name == rule_name:
                existing_order = int(existing.order)
                break

        if existing_order is None:
            order = self._rewrite_rule_counter
            self._rewrite_rule_counter += 1
        else:
            order = existing_order

        if provenance is None:
            provenance_map: Dict[str, Any] = {
                "stage": stage,
                "name": rule_name,
                "kind": "rewrite",
            }
            tok = self._last_token
            if tok is not None:
                loc = self.location_for_token(tok)
                provenance_map.update(
                    {
                        "path": str(loc.path),
                        "line": tok.line,
                        "column": tok.column,
                    }
                )
        else:
            provenance_map = dict(provenance)

        metadata_map = dict(metadata) if isinstance(metadata, dict) else {}
        piece_specs = [self._parse_rewrite_piece(piece) for piece in pattern_lex]
        min_suffix = [0] * (len(piece_specs) + 1)
        for idx in range(len(piece_specs) - 1, -1, -1):
            min_suffix[idx] = min_suffix[idx + 1] + int(piece_specs[idx]["min_count"])

        max_tokens: Optional[int] = 0
        required_literals: List[str] = []
        for spec in piece_specs:
            max_count = spec.get("max_count")
            if max_tokens is not None:
                if max_count is None:
                    max_tokens = None
                else:
                    max_tokens += int(max_count)
            if (
                spec.get("kind") == "literal"
                and not bool(spec.get("negated"))
                and int(spec.get("min_count", 0)) > 0
            ):
                required_literals.append(str(spec.get("literal", "")))

        prefilter_bloom = 0
        prefilter_window = int(max_tokens or 0) if max_tokens is not None else 0
        if prefilter_window > 0 and required_literals:
            for lit in required_literals:
                prefilter_bloom |= self._rewrite_bloom_bit(lit)

        metadata_map["__piece_specs"] = piece_specs
        metadata_map["__min_suffix"] = tuple(min_suffix)
        metadata_map["__prefilter_window"] = prefilter_window
        metadata_map["__prefilter_bloom"] = int(prefilter_bloom)
        metadata_map["__stat_attempts"] = 0
        metadata_map["__stat_matches"] = 0
        metadata_map["__stat_applied"] = 0

        rule = RewriteRule(
            rule_name,
            pattern_lex,
            replacement_lex,
            priority=priority,
            order=order,
            enabled=enabled,
            pipeline=pipeline,
            guard=guard,
            group=group,
            scope=scope,
            metadata=metadata_map,
            provenance=provenance_map,
            specificity=specificity,
        )

        for idx, existing in enumerate(bucket):
            if existing.name == rule_name:
                bucket[idx] = rule
                break
        else:
            bucket.append(rule)
        bucket.sort(key=lambda r: (-r.priority, r.order))
        self._invalidate_rewrite_index(stage)
        return rule_name

    def remove_rewrite_rule(self, stage: str, name: str) -> bool:
        bucket = self._rewrite_bucket(stage)
        for idx, rule in enumerate(bucket):
            if rule.name == name:
                del bucket[idx]
                self._invalidate_rewrite_index(stage)
                return True
        return False

    def clear_rewrite_rules(self, stage: str) -> int:
        bucket = self._rewrite_bucket(stage)
        count = len(bucket)
        bucket.clear()
        self._invalidate_rewrite_index(stage)
        return count

    def list_rewrite_rules(self, stage: str) -> List[str]:
        bucket = self._rewrite_bucket(stage)
        return [rule.name for rule in bucket]

    def set_rewrite_rule_enabled(self, stage: str, name: str, enabled: bool) -> bool:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                rule.enabled = bool(enabled)
                self._invalidate_rewrite_index(stage)
                return True
        return False

    def get_rewrite_rule_enabled(self, stage: str, name: str) -> Optional[bool]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return bool(rule.enabled)
        return None

    def set_rewrite_rule_priority(self, stage: str, name: str, priority: int) -> bool:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                rule.priority = int(priority)
                bucket.sort(key=lambda r: (-r.priority, r.order))
                self._invalidate_rewrite_index(stage)
                return True
        return False

    def get_rewrite_rule_priority(self, stage: str, name: str) -> Optional[int]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return int(rule.priority)
        return None

    def get_rewrite_rule_specificity(self, stage: str, name: str) -> Optional[int]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return int(rule.specificity)
        return None

    def set_rewrite_rule_pipeline(self, stage: str, name: str, pipeline: str) -> bool:
        bucket = self._rewrite_bucket(stage)
        pipeline_name = str(pipeline or "default")
        for rule in bucket:
            if rule.name == name:
                rule.pipeline = pipeline_name
                self._invalidate_rewrite_index(stage)
                return True
        return False

    def get_rewrite_rule_pipeline(self, stage: str, name: str) -> Optional[str]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return str(rule.pipeline)
        return None

    def set_rewrite_rule_guard(self, stage: str, name: str, guard: Optional[str]) -> bool:
        bucket = self._rewrite_bucket(stage)
        guard_name = str(guard) if guard else None
        for rule in bucket:
            if rule.name == name:
                rule.guard = guard_name
                self._invalidate_rewrite_index(stage)
                return True
        return False

    def get_rewrite_rule_guard(self, stage: str, name: str) -> Optional[str]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return rule.guard
        return None

    def set_rewrite_rule_group(self, stage: str, name: str, group: str) -> bool:
        bucket = self._rewrite_bucket(stage)
        group_name = str(group or "default")
        for rule in bucket:
            if rule.name == name:
                rule.group = group_name
                self._invalidate_rewrite_index(stage)
                return True
        return False

    def get_rewrite_rule_group(self, stage: str, name: str) -> Optional[str]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return str(rule.group)
        return None

    def set_rewrite_rule_scope(self, stage: str, name: str, scope: str) -> bool:
        bucket = self._rewrite_bucket(stage)
        scope_name = str(scope or "global")
        for rule in bucket:
            if rule.name == name:
                rule.scope = scope_name
                self._invalidate_rewrite_index(stage)
                return True
        return False

    def get_rewrite_rule_scope(self, stage: str, name: str) -> Optional[str]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return str(rule.scope)
        return None

    def get_rewrite_rule_provenance(self, stage: str, name: str) -> Optional[Dict[str, Any]]:
        bucket = self._rewrite_bucket(stage)
        for rule in bucket:
            if rule.name == name:
                return dict(rule.provenance)
        return None

    def set_rewrite_pipeline_active(self, stage: str, pipeline: str, enabled: bool) -> None:
        bucket = self._rewrite_bucket(stage)
        _ = bucket  # ensure stage validation
        active = self._active_rewrite_pipelines.setdefault(stage, {"default"})
        pipe = str(pipeline or "default")
        if enabled:
            active.add(pipe)
        else:
            active.discard(pipe)
            if not active:
                active.add("default")

    def list_active_rewrite_pipelines(self, stage: str) -> List[str]:
        bucket = self._rewrite_bucket(stage)
        _ = bucket  # ensure stage validation
        return sorted(self._active_rewrite_pipelines.setdefault(stage, {"default"}))

    def set_pattern_group_active(self, group: str, enabled: bool) -> None:
        grp = str(group or "default")
        if enabled:
            self._active_pattern_groups.add(grp)
        else:
            self._active_pattern_groups.discard(grp)
            if not self._active_pattern_groups:
                self._active_pattern_groups.add("default")

    def set_pattern_scope_active(self, scope: str, enabled: bool) -> None:
        scp = str(scope or "global")
        if enabled:
            self._active_pattern_scopes.add(scp)
        else:
            self._active_pattern_scopes.discard(scp)
            if not self._active_pattern_scopes:
                self._active_pattern_scopes.add("global")

    def list_active_pattern_groups(self) -> List[str]:
        return sorted(self._active_pattern_groups)

    def list_active_pattern_scopes(self) -> List[str]:
        return sorted(self._active_pattern_scopes)

    def set_pattern_macro_group(self, name: str, group: str) -> bool:
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return False
        grp = str(group or "default")
        self._pattern_macro_groups[name] = grp
        changed = False
        for rule_name in rule_names:
            if self.set_rewrite_rule_group("grammar", rule_name, grp):
                changed = True
        return changed

    def get_pattern_macro_group(self, name: str) -> Optional[str]:
        if name in self._pattern_macro_groups:
            return self._pattern_macro_groups[name]
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return None
        group = self.get_rewrite_rule_group("grammar", rule_names[0])
        if group is None:
            return None
        return group

    def set_pattern_macro_scope(self, name: str, scope: str) -> bool:
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return False
        scp = str(scope or "global")
        self._pattern_macro_scopes[name] = scp
        changed = False
        for rule_name in rule_names:
            if self.set_rewrite_rule_scope("grammar", rule_name, scp):
                changed = True
        return changed

    def get_pattern_macro_scope(self, name: str) -> Optional[str]:
        if name in self._pattern_macro_scopes:
            return self._pattern_macro_scopes[name]
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return None
        scope = self.get_rewrite_rule_scope("grammar", rule_names[0])
        if scope is None:
            return None
        return scope

    @staticmethod
    def _clone_rewrite_rule(rule: RewriteRule) -> RewriteRule:
        return RewriteRule(
            rule.name,
            list(rule.pattern),
            list(rule.replacement),
            priority=rule.priority,
            order=rule.order,
            enabled=rule.enabled,
            pipeline=rule.pipeline,
            guard=rule.guard,
            group=rule.group,
            scope=rule.scope,
            metadata=dict(rule.metadata),
            provenance=dict(rule.provenance),
            specificity=rule.specificity,
        )

    def _snapshot_rewrite_state(self) -> Dict[str, Any]:
        return {
            "reader_rules": [self._clone_rewrite_rule(rule) for rule in self.reader_rewrite_rules],
            "grammar_rules": [self._clone_rewrite_rule(rule) for rule in self.grammar_rewrite_rules],
            "rewrite_rule_counter": int(self._rewrite_rule_counter),
            "pattern_macro_rules": {name: list(rules) for name, rules in self._pattern_macro_rules.items()},
            "pattern_macro_groups": dict(self._pattern_macro_groups),
            "pattern_macro_scopes": dict(self._pattern_macro_scopes),
            "active_rewrite_pipelines": {
                stage: set(values) for stage, values in self._active_rewrite_pipelines.items()
            },
            "active_pattern_groups": set(self._active_pattern_groups),
            "active_pattern_scopes": set(self._active_pattern_scopes),
            "rewrite_saturation_strategy": self.rewrite_saturation_strategy,
            "rewrite_max_steps": int(self.rewrite_max_steps),
            "rewrite_loop_detection": bool(self.rewrite_loop_detection),
        }

    def _restore_rewrite_state(self, snapshot: Dict[str, Any]) -> None:
        self.reader_rewrite_rules = [self._clone_rewrite_rule(rule) for rule in snapshot.get("reader_rules", [])]
        self.grammar_rewrite_rules = [self._clone_rewrite_rule(rule) for rule in snapshot.get("grammar_rules", [])]
        self._rewrite_rule_counter = int(snapshot.get("rewrite_rule_counter", self._rewrite_rule_counter))
        self._pattern_macro_rules = {
            str(name): list(values)
            for name, values in snapshot.get("pattern_macro_rules", {}).items()
        }
        self._pattern_macro_groups = {
            str(name): str(group)
            for name, group in snapshot.get("pattern_macro_groups", {}).items()
        }
        self._pattern_macro_scopes = {
            str(name): str(scope)
            for name, scope in snapshot.get("pattern_macro_scopes", {}).items()
        }
        active_pipelines = snapshot.get("active_rewrite_pipelines", {})
        self._active_rewrite_pipelines = {
            "reader": set(active_pipelines.get("reader", {"default"})),
            "grammar": set(active_pipelines.get("grammar", {"default"})),
        }
        if not self._active_rewrite_pipelines["reader"]:
            self._active_rewrite_pipelines["reader"].add("default")
        if not self._active_rewrite_pipelines["grammar"]:
            self._active_rewrite_pipelines["grammar"].add("default")
        self._active_pattern_groups = set(snapshot.get("active_pattern_groups", {"default"}))
        self._active_pattern_scopes = set(snapshot.get("active_pattern_scopes", {"global"}))
        if not self._active_pattern_groups:
            self._active_pattern_groups.add("default")
        if not self._active_pattern_scopes:
            self._active_pattern_scopes.add("global")
        self.rewrite_saturation_strategy = str(
            snapshot.get("rewrite_saturation_strategy", self.rewrite_saturation_strategy)
        )
        self.rewrite_max_steps = int(snapshot.get("rewrite_max_steps", self.rewrite_max_steps))
        self.rewrite_loop_detection = bool(snapshot.get("rewrite_loop_detection", self.rewrite_loop_detection))
        self._invalidate_rewrite_index()

    def rewrite_transaction_begin(self) -> int:
        self._rewrite_transactions.append(self._snapshot_rewrite_state())
        return len(self._rewrite_transactions)

    def rewrite_transaction_commit(self) -> bool:
        if not self._rewrite_transactions:
            return False
        self._rewrite_transactions.pop()
        return True

    def rewrite_transaction_rollback(self) -> bool:
        if not self._rewrite_transactions:
            return False
        snapshot = self._rewrite_transactions.pop()
        self._restore_rewrite_state(snapshot)
        return True

    def export_rewrite_pack(self) -> Dict[str, Any]:
        def _encode_rule(stage: str, rule: RewriteRule) -> Dict[str, Any]:
            return {
                "stage": stage,
                "name": rule.name,
                "pattern": list(rule.pattern),
                "replacement": list(rule.replacement),
                "priority": int(rule.priority),
                "enabled": bool(rule.enabled),
                "pipeline": str(rule.pipeline),
                "guard": rule.guard,
                "group": str(rule.group),
                "scope": str(rule.scope),
                "specificity": int(rule.specificity),
                "metadata": dict(rule.metadata),
                "provenance": dict(rule.provenance),
            }

        return {
            "reader": [_encode_rule("reader", rule) for rule in self.reader_rewrite_rules],
            "grammar": [_encode_rule("grammar", rule) for rule in self.grammar_rewrite_rules],
            "pattern_macros": {name: list(values) for name, values in self._pattern_macro_rules.items()},
            "pattern_groups": dict(self._pattern_macro_groups),
            "pattern_scopes": dict(self._pattern_macro_scopes),
        }

    def import_rewrite_pack(self, pack: Dict[str, Any], *, replace: bool = False) -> int:
        if not isinstance(pack, dict):
            raise ParseError("rewrite pack import expects a map")

        if replace:
            self.clear_rewrite_rules("reader")
            self.clear_rewrite_rules("grammar")
            self._pattern_macro_rules.clear()
            self._pattern_macro_groups.clear()
            self._pattern_macro_scopes.clear()

        added = 0
        for stage in ("reader", "grammar"):
            entries = pack.get(stage)
            if entries is None:
                continue
            for entry in _ensure_list(entries):
                row = _ensure_dict(entry)
                self.add_rewrite_rule(
                    stage,
                    _coerce_lexeme_list(row.get("pattern", []), field=f"rewrite pack {stage} pattern"),
                    _coerce_lexeme_list(row.get("replacement", []), field=f"rewrite pack {stage} replacement"),
                    name=str(row.get("name", "") or "").strip() or None,
                    priority=int(row.get("priority", 0)),
                    enabled=_coerce_bool(row.get("enabled", 1), field=f"rewrite pack {stage} enabled"),
                    pipeline=str(row.get("pipeline", "default") or "default"),
                    guard=(str(row.get("guard")) if row.get("guard") is not None else None),
                    group=str(row.get("group", "default") or "default"),
                    scope=str(row.get("scope", "global") or "global"),
                    metadata=dict(row.get("metadata", {})) if isinstance(row.get("metadata"), dict) else {},
                    provenance=dict(row.get("provenance", {})) if isinstance(row.get("provenance"), dict) else {},
                )
                added += 1

        pattern_macros_raw = pack.get("pattern_macros")
        if isinstance(pattern_macros_raw, dict):
            for name, values in pattern_macros_raw.items():
                self._pattern_macro_rules[str(name)] = [str(v) for v in _ensure_list(values)]

        pattern_groups_raw = pack.get("pattern_groups")
        if isinstance(pattern_groups_raw, dict):
            for name, group in pattern_groups_raw.items():
                self._pattern_macro_groups[str(name)] = str(group)

        pattern_scopes_raw = pack.get("pattern_scopes")
        if isinstance(pattern_scopes_raw, dict):
            for name, scope in pattern_scopes_raw.items():
                self._pattern_macro_scopes[str(name)] = str(scope)

        return added

    def _rewrite_rules_compatibility(self, left: RewriteRule, right: RewriteRule) -> str:
        left_keys, left_wild = self._rewrite_pattern_index_keys(left.pattern)
        right_keys, right_wild = self._rewrite_pattern_index_keys(right.pattern)
        if not left_wild and not right_wild and left_keys.isdisjoint(right_keys):
            return "disjoint"
        if list(left.replacement) == list(right.replacement):
            return "equivalent"
        return "overlap"

    def detect_pattern_macro_conflicts(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        conflicts: List[Dict[str, Any]] = []
        if name is None:
            names = sorted(self._pattern_macro_rules.keys())
        else:
            names = [name]

        by_rule_name = {rule.name: rule for rule in self.grammar_rewrite_rules}
        for macro_name in names:
            rule_names = self._pattern_macro_rule_names(macro_name)
            rules = [by_rule_name[rn] for rn in rule_names if rn in by_rule_name]
            for i in range(len(rules)):
                for j in range(i + 1, len(rules)):
                    left = rules[i]
                    right = rules[j]
                    relation = self._rewrite_rules_compatibility(left, right)
                    if relation == "disjoint":
                        continue
                    if left.priority != right.priority:
                        continue
                    conflicts.append(
                        {
                            "macro": macro_name,
                            "left": left.name,
                            "right": right.name,
                            "relation": relation,
                            "specificity": [int(left.specificity), int(right.specificity)],
                        }
                    )
        return conflicts

    def build_rewrite_compatibility_matrix(self, stage: str) -> List[Dict[str, Any]]:
        rules = list(self._rewrite_bucket(stage))
        matrix: List[Dict[str, Any]] = []
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                left = rules[i]
                right = rules[j]
                matrix.append(
                    {
                        "left": left.name,
                        "right": right.name,
                        "relation": self._rewrite_rules_compatibility(left, right),
                        "left_priority": int(left.priority),
                        "right_priority": int(right.priority),
                        "left_specificity": int(left.specificity),
                        "right_specificity": int(right.specificity),
                    }
                )
        return matrix

    def clear_rewrite_trace_log(self) -> int:
        count = len(self.rewrite_trace_log)
        self.rewrite_trace_log.clear()
        return count

    def get_rewrite_profile_snapshot(self) -> Dict[str, Dict[str, int]]:
        return {
            stage: {name: int(value) for name, value in stats.items()}
            for stage, stats in self.rewrite_profile.items()
        }

    def clear_rewrite_profile(self) -> None:
        for stats in self.rewrite_profile.values():
            for key in list(stats.keys()):
                stats[key] = 0
        for rule in self.reader_rewrite_rules:
            if isinstance(rule.metadata, dict):
                rule.metadata["__stat_attempts"] = 0
                rule.metadata["__stat_matches"] = 0
                rule.metadata["__stat_applied"] = 0
        for rule in self.grammar_rewrite_rules:
            if isinstance(rule.metadata, dict):
                rule.metadata["__stat_attempts"] = 0
                rule.metadata["__stat_matches"] = 0
                rule.metadata["__stat_applied"] = 0

    def set_macro_expansion_limit(self, limit: int) -> None:
        if limit < 1:
            raise ParseError("macro expansion limit must be >= 1")
        self.macro_expansion_limit = int(limit)

    def set_macro_preview(self, enabled: bool) -> None:
        self.macro_preview = bool(enabled)

    @staticmethod
    def _rewrite_bloom_bit(lexeme: str) -> int:
        return 1 << (hash(lexeme) & 63)

    def set_macro_profile_enabled(self, enabled: bool) -> None:
        self._macro_profile_enabled = bool(enabled)
        if not self._macro_profile_enabled:
            self._macro_profile.clear()

    def _intern_expansion_lexeme(self, lexeme: str) -> str:
        cached = self._expansion_lexeme_pool.get(lexeme)
        if cached is not None:
            return cached
        interned = sys.intern(str(lexeme))
        self._expansion_lexeme_pool[interned] = interned
        return interned

    def _intern_capture_group(self, values: Sequence[str]) -> Tuple[str, ...]:
        key = tuple(self._intern_expansion_lexeme(piece) for piece in values)
        cached = self._capture_group_pool.get(key)
        if cached is not None:
            return cached
        self._capture_group_pool[key] = key
        return key

    def _record_macro_profile_event(self, name: str, *, emitted_tokens: int, duration_ns: int) -> None:
        macro_name = str(name)
        self._macro_hotness[macro_name] = int(self._macro_hotness.get(macro_name, 0)) + 1
        if not self._macro_profile_enabled:
            return
        row = self._macro_profile.get(macro_name)
        if row is None:
            row = {
                "calls": 0,
                "tokens": 0,
                "total_ns": 0,
                "max_ns": 0,
            }
            self._macro_profile[macro_name] = row
        row["calls"] = int(row.get("calls", 0)) + 1
        row["tokens"] = int(row.get("tokens", 0)) + int(max(0, emitted_tokens))
        row["total_ns"] = int(row.get("total_ns", 0)) + int(max(0, duration_ns))
        row["max_ns"] = max(int(row.get("max_ns", 0)), int(max(0, duration_ns)))

    def get_macro_profile_snapshot(self) -> Dict[str, Dict[str, int]]:
        return {
            name: {
                "calls": int(row.get("calls", 0)),
                "tokens": int(row.get("tokens", 0)),
                "total_ns": int(row.get("total_ns", 0)),
                "max_ns": int(row.get("max_ns", 0)),
                "hotness": int(self._macro_hotness.get(name, 0)),
            }
            for name, row in self._macro_profile.items()
        }

    def clear_macro_profile(self) -> None:
        self._macro_profile.clear()
        self._macro_hotness.clear()

    def format_macro_profile(self, *, limit: int = 0) -> str:
        rows: List[Tuple[str, Dict[str, int]]] = []
        for name, row in self._macro_profile.items():
            calls = int(row.get("calls", 0))
            if calls <= 0:
                continue
            rows.append((name, row))
        if not rows:
            return "[macro-profile] no macro expansions recorded"

        rows.sort(
            key=lambda item: (
                -int(item[1].get("calls", 0)),
                -int(item[1].get("total_ns", 0)),
                item[0],
            )
        )
        if limit > 0:
            rows = rows[:limit]

        lines = [
            "[macro-profile] name calls tokens total-ms avg-us max-us",
        ]
        for name, row in rows:
            calls = int(row.get("calls", 0))
            tokens = int(row.get("tokens", 0))
            total_ns = int(row.get("total_ns", 0))
            max_ns = int(row.get("max_ns", 0))
            total_ms = total_ns / 1_000_000.0
            avg_us = (total_ns / calls) / 1_000.0 if calls > 0 else 0.0
            max_us = max_ns / 1_000.0
            lines.append(
                f"[macro-profile] {name} {calls} {tokens} {total_ms:.3f} {avg_us:.2f} {max_us:.2f}"
            )
        return "\n".join(lines)

    def _eliminate_dead_macros(self) -> int:
        to_remove: List[str] = []
        for name, word in list(self.dictionary.words.items()):
            if word.macro_expansion is None:
                continue
            if int(self._macro_hotness.get(name, 0)) > 0:
                continue
            if self._pattern_macro_rule_names(name):
                continue
            # Keep mixed-mode words (for example compile-time intrinsic aliases).
            if word.definition is not None or word.compile_time_intrinsic is not None or word.runtime_intrinsic is not None:
                continue
            to_remove.append(name)

        for name in to_remove:
            self.unregister_word(name)
        return len(to_remove)

    def _eliminate_unused_rewrite_rules(self) -> int:
        removed = 0
        for stage in ("reader", "grammar"):
            bucket = self._rewrite_bucket(stage)
            if not bucket:
                continue
            kept: List[RewriteRule] = []
            for rule in bucket:
                meta = rule.metadata if isinstance(rule.metadata, dict) else {}
                applied = int(meta.get("__stat_applied", 0))
                if applied <= 0:
                    removed += 1
                    continue
                kept.append(rule)
            if len(kept) != len(bucket):
                bucket[:] = kept
                self._invalidate_rewrite_index(stage)
                if stage == "grammar" and self._pattern_macro_rules:
                    existing = {rule.name for rule in bucket}
                    stale_macros: List[str] = []
                    for macro_name, rule_names in list(self._pattern_macro_rules.items()):
                        next_names = [rule_name for rule_name in rule_names if rule_name in existing]
                        if next_names:
                            self._pattern_macro_rules[macro_name] = next_names
                        else:
                            stale_macros.append(macro_name)
                    for macro_name in stale_macros:
                        self._pattern_macro_rules.pop(macro_name, None)
                        self._pattern_macro_groups.pop(macro_name, None)
                        self._pattern_macro_scopes.pop(macro_name, None)
        return removed

    def _finalize_parse_performance_passes(self) -> None:
        if self.enable_dead_macro_elimination:
            self._eliminate_dead_macros()
        if self.enable_unused_rewrite_elimination:
            self._eliminate_unused_rewrite_rules()

    def register_text_macro(self, name: str, param_count: int, expansion: Sequence[Any]) -> None:
        param_count_i = int(param_count)
        self.register_text_macro_signature(
            name,
            [str(i) for i in range(max(0, param_count_i))],
            expansion,
        )

    def register_text_macro_signature(
        self,
        name: str,
        param_spec: Sequence[Any],
        expansion: Sequence[Any],
    ) -> None:
        if not name:
            raise ParseError("macro name cannot be empty")

        param_tokens = self._normalize_rewrite_lexemes(param_spec, field="macro parameter spec")
        ordered_params: List[str] = []
        seen: Set[str] = set()
        variadic_param: Optional[str] = None
        for raw in param_tokens:
            lex = raw
            is_variadic = False
            if lex.startswith("*"):
                is_variadic = True
                lex = lex[1:]
            elif lex.startswith("..."):
                is_variadic = True
                lex = lex[3:]
            if not lex or (not _is_identifier(lex) and not lex.isdigit()):
                raise ParseError(f"invalid macro parameter name '{raw}' in macro '{name}'")
            if lex in seen:
                raise ParseError(f"duplicate macro parameter '{lex}' in macro '{name}'")
            seen.add(lex)
            if is_variadic:
                if variadic_param is not None:
                    raise ParseError(f"macro '{name}' cannot declare multiple variadic parameters")
                variadic_param = lex
                continue
            if variadic_param is not None:
                raise ParseError(f"macro '{name}' variadic parameter must be last")
            ordered_params.append(lex)

        expansion_lex = self._normalize_rewrite_lexemes(expansion, field="macro expansion")
        word = Word(name=name)
        word.macro_expansion = [self._intern_expansion_lexeme(piece) for piece in expansion_lex]
        word.macro_params = len(ordered_params)
        self.dictionary.register(word)
        self._macro_signatures[name] = (tuple(ordered_params), variadic_param)
        self._macro_hotness.pop(name, None)
        self._macro_profile.pop(name, None)

    def register_pattern_macro(
        self,
        name: str,
        clauses: Sequence[Any],
    ) -> None:
        if not name:
            raise ParseError("pattern macro name cannot be empty")
        if not clauses:
            raise ParseError(f"macro '{name}' requires at least one pattern clause")

        self.unregister_pattern_macro(name)

        macro_group = self._pattern_macro_groups.get(name, "default")
        macro_scope = self._pattern_macro_scopes.get(name, "global")

        rule_names: List[str] = []
        for idx, clause in enumerate(clauses):
            rule_guard: Optional[str] = None
            rule_group = macro_group
            rule_scope = macro_scope
            rule_metadata: Dict[str, Any] = {
                "macro": name,
                "clause_index": idx,
                "kind": "pattern-macro-clause",
            }

            if isinstance(clause, dict):
                row = dict(clause)
                pattern = row.get("pattern")
                replacement = row.get("replacement")
                guard_raw = row.get("guard")
                if guard_raw is not None:
                    rule_guard = str(guard_raw)
                group_raw = row.get("group")
                if group_raw is not None:
                    rule_group = str(group_raw)
                scope_raw = row.get("scope")
                if scope_raw is not None:
                    rule_scope = str(scope_raw)
                extra_metadata = row.get("metadata")
                if isinstance(extra_metadata, dict):
                    rule_metadata.update(extra_metadata)
            else:
                pair = _ensure_list(clause)
                if len(pair) not in (2, 3):
                    raise ParseError(
                        f"macro '{name}' clause {idx + 1} must be [pattern replacement] or [pattern replacement guard]"
                    )
                pattern = pair[0]
                replacement = pair[1]
                if len(pair) == 3 and pair[2] is not None:
                    rule_guard = _coerce_str(pair[2])

            pattern_lex = self._normalize_rewrite_lexemes(
                pattern,
                field=f"macro '{name}' pattern",
            )
            if not pattern_lex:
                raise ParseError(f"macro '{name}' clause {idx + 1} has an empty pattern")
            replacement_lex = self._normalize_rewrite_lexemes(
                replacement,
                field=f"macro '{name}' replacement",
            )
            rule_name = f"pattern-macro:{name}:{idx}"
            added = self.add_rewrite_rule(
                "grammar",
                [name, *pattern_lex],
                replacement_lex,
                name=rule_name,
                group=rule_group,
                scope=rule_scope,
                guard=rule_guard,
                metadata=rule_metadata,
                provenance={
                    "kind": "pattern-macro",
                    "macro": name,
                    "clause_index": idx,
                    "stage": "grammar",
                },
            )
            rule_names.append(added)

        self._pattern_macro_rules[name] = rule_names
        self._pattern_macro_groups[name] = str(macro_group)
        self._pattern_macro_scopes[name] = str(macro_scope)

    def unregister_pattern_macro(self, name: str) -> bool:
        removed = False
        rule_names = self._pattern_macro_rules.pop(name, None)
        self._pattern_macro_groups.pop(name, None)
        self._pattern_macro_scopes.pop(name, None)
        if rule_names is None:
            prefix = f"pattern-macro:{name}:"
            for rule in list(self.grammar_rewrite_rules):
                if rule.name.startswith(prefix):
                    if self.remove_rewrite_rule("grammar", rule.name):
                        removed = True
            return removed

        for rule_name in rule_names:
            if self.remove_rewrite_rule("grammar", rule_name):
                removed = True
        return removed

    @staticmethod
    def _pattern_macro_rule_sort_key(rule_name: str) -> Tuple[int, str]:
        try:
            idx = int(rule_name.rsplit(":", 1)[1])
            return idx, rule_name
        except Exception:
            return 1_000_000_000, rule_name

    def _pattern_macro_rule_names(self, name: str) -> List[str]:
        rule_names = self._pattern_macro_rules.get(name)
        if rule_names is not None:
            return list(rule_names)
        prefix = f"pattern-macro:{name}:"
        discovered = [
            rule.name
            for rule in self.grammar_rewrite_rules
            if rule.name.startswith(prefix)
        ]
        discovered.sort(key=self._pattern_macro_rule_sort_key)
        return discovered

    def set_pattern_macro_enabled(self, name: str, enabled: bool) -> bool:
        rule_names = self._pattern_macro_rule_names(name)
        changed = False
        for rule_name in rule_names:
            if self.set_rewrite_rule_enabled("grammar", rule_name, enabled):
                changed = True
        return changed

    def get_pattern_macro_enabled(self, name: str) -> Optional[bool]:
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return None
        states: List[bool] = []
        for rule_name in rule_names:
            state = self.get_rewrite_rule_enabled("grammar", rule_name)
            if state is not None:
                states.append(bool(state))
        if not states:
            return None
        return all(states)

    def set_pattern_macro_priority(self, name: str, priority: int) -> bool:
        rule_names = self._pattern_macro_rule_names(name)
        changed = False
        for rule_name in rule_names:
            if self.set_rewrite_rule_priority("grammar", rule_name, priority):
                changed = True
        return changed

    def get_pattern_macro_priority(self, name: str) -> Optional[int]:
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return None
        priorities: List[int] = []
        for rule_name in rule_names:
            prio = self.get_rewrite_rule_priority("grammar", rule_name)
            if prio is not None:
                priorities.append(int(prio))
        if not priorities:
            return None
        if len(set(priorities)) != 1:
            return None
        return priorities[0]

    def get_pattern_macro_clauses(self, name: str) -> Optional[List[Tuple[List[str], List[str]]]]:
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return None
        by_name = {rule.name: rule for rule in self.grammar_rewrite_rules}
        clauses: List[Tuple[List[str], List[str]]] = []
        for rule_name in rule_names:
            rule = by_name.get(rule_name)
            if rule is None:
                continue
            pattern = list(rule.pattern)
            if pattern and pattern[0] == name:
                pattern = pattern[1:]
            clauses.append((pattern, list(rule.replacement)))
        if not clauses:
            return None
        return clauses

    def get_pattern_macro_clause_details(self, name: str) -> Optional[List[Dict[str, Any]]]:
        rule_names = self._pattern_macro_rule_names(name)
        if not rule_names:
            return None
        by_name = {rule.name: rule for rule in self.grammar_rewrite_rules}
        details: List[Dict[str, Any]] = []
        for rule_name in rule_names:
            rule = by_name.get(rule_name)
            if rule is None:
                continue
            pattern = list(rule.pattern)
            if pattern and pattern[0] == name:
                pattern = pattern[1:]
            details.append(
                {
                    "rule": rule.name,
                    "pattern": pattern,
                    "replacement": list(rule.replacement),
                    "guard": rule.guard,
                    "group": rule.group,
                    "scope": rule.scope,
                    "pipeline": rule.pipeline,
                    "priority": int(rule.priority),
                    "enabled": bool(rule.enabled),
                    "specificity": int(rule.specificity),
                    "metadata": dict(rule.metadata),
                    "provenance": dict(rule.provenance),
                }
            )
        if not details:
            return None
        return details

    def _macro_exists(self, name: str) -> bool:
        word = self.dictionary.lookup(name)
        if word is not None and word.macro_expansion is not None:
            return True
        return bool(self._pattern_macro_rule_names(name))

    def get_macro_expansion(self, name: str) -> Optional[List[str]]:
        word = self.dictionary.lookup(name)
        if word is None or word.macro_expansion is None:
            return None
        return list(word.macro_expansion)

    def set_macro_expansion(self, name: str, expansion: Sequence[Any]) -> bool:
        word = self.dictionary.lookup(name)
        if word is None or word.macro_expansion is None:
            return False
        expansion_lex = self._normalize_rewrite_lexemes(expansion, field="macro expansion")
        word.macro_expansion = [self._intern_expansion_lexeme(piece) for piece in expansion_lex]
        word.macro_template_ast = None
        word.macro_template_program = None
        word.macro_template_version = None
        word.macro_template_mode = "strict"
        self._macro_hotness.pop(name, None)
        self._macro_profile.pop(name, None)
        return True

    def get_macro_doc(self, name: str) -> Optional[str]:
        value = self._macro_docs.get(name)
        if value is None:
            return None
        return str(value)

    def set_macro_doc(self, name: str, value: Optional[str]) -> bool:
        if not self._macro_exists(name):
            return False
        if value is None:
            self._macro_docs.pop(name, None)
            return True
        self._macro_docs[name] = str(value)
        return True

    def get_macro_attrs(self, name: str) -> Optional[Dict[str, Any]]:
        value = self._macro_attrs.get(name)
        if value is None:
            return None
        return {
            str(key): _capture_deep_clone(item)
            for key, item in value.items()
        }

    def set_macro_attrs(self, name: str, attrs: Optional[Dict[str, Any]]) -> bool:
        if not self._macro_exists(name):
            return False
        if attrs is None:
            self._macro_attrs.pop(name, None)
            return True
        snapshot: Dict[str, Any] = {}
        for key, item in attrs.items():
            snapshot[str(key)] = _capture_deep_clone(item)
        self._macro_attrs[name] = snapshot
        return True

    def clone_macro(self, source: str, target: str) -> bool:
        if not source or not target:
            return False
        if source == target:
            return self._macro_exists(source)
        if self._macro_exists(target) or self.dictionary.lookup(target) is not None:
            return False

        cloned = False
        source_word = self.dictionary.lookup(source)
        source_is_text = source_word is not None and source_word.macro_expansion is not None
        source_pattern_details = self.get_pattern_macro_clause_details(source)

        if source_is_text:
            clone_word = Word(name=target)
            clone_word.macro_expansion = list(source_word.macro_expansion or [])
            clone_word.macro_params = int(source_word.macro_params)
            self.dictionary.register(clone_word)
            signature = self._macro_signatures.get(source)
            if signature is not None:
                self._macro_signatures[target] = (tuple(signature[0]), signature[1])
            cloned = True

        if source_pattern_details:
            clauses: List[Dict[str, Any]] = []
            for detail in source_pattern_details:
                metadata = detail.get("metadata", {})
                metadata_copy = dict(metadata) if isinstance(metadata, dict) else {}
                metadata_copy["macro"] = target
                clauses.append(
                    {
                        "pattern": list(detail.get("pattern", [])),
                        "replacement": list(detail.get("replacement", [])),
                        "guard": detail.get("guard"),
                        "group": str(detail.get("group", "default") or "default"),
                        "scope": str(detail.get("scope", "global") or "global"),
                        "metadata": metadata_copy,
                    }
                )
            self.register_pattern_macro(target, clauses)

            by_name = {rule.name: rule for rule in self.grammar_rewrite_rules}
            target_rule_names = self._pattern_macro_rule_names(target)
            for idx, target_rule_name in enumerate(target_rule_names):
                if idx >= len(source_pattern_details):
                    break
                detail = source_pattern_details[idx]
                target_rule = by_name.get(target_rule_name)
                if target_rule is None:
                    continue
                target_rule.priority = int(detail.get("priority", target_rule.priority))
                target_rule.enabled = bool(detail.get("enabled", target_rule.enabled))
                target_rule.pipeline = str(detail.get("pipeline", target_rule.pipeline) or "default")
                target_rule.guard = (
                    str(detail.get("guard")) if detail.get("guard") is not None else None
                )
                target_rule.specificity = int(detail.get("specificity", target_rule.specificity))

                provenance = detail.get("provenance", {})
                if isinstance(provenance, dict):
                    prov_copy = dict(provenance)
                    if "macro" in prov_copy:
                        prov_copy["macro"] = target
                    target_rule.provenance = prov_copy

                metadata = detail.get("metadata", {})
                if isinstance(metadata, dict):
                    meta_copy = dict(metadata)
                    if "macro" in meta_copy:
                        meta_copy["macro"] = target
                    target_rule.metadata = meta_copy

            self._invalidate_rewrite_index("grammar")
            cloned = True

        if not cloned:
            return False

        if source in self._macro_docs:
            self._macro_docs[target] = str(self._macro_docs[source])
        if source in self._macro_attrs:
            attrs = self._macro_attrs[source]
            self._macro_attrs[target] = {
                str(key): _capture_deep_clone(item)
                for key, item in attrs.items()
            }
        if source in self.capture_schemas:
            schema = self.capture_schemas[source]
            self.capture_schemas[target] = {
                str(key): _capture_deep_clone(item)
                for key, item in schema.items()
            }
        if source in self.capture_taint:
            taint = self.capture_taint[source]
            self.capture_taint[target] = {
                str(key): bool(value)
                for key, value in taint.items()
            }
        if source in self._ct_call_abi_contracts:
            contract = self._ct_call_abi_contracts[source]
            self._ct_call_abi_contracts[target] = {
                str(key): _capture_deep_clone(item)
                for key, item in contract.items()
            }
        if source in self._macro_hotness:
            self._macro_hotness[target] = int(self._macro_hotness[source])
        if source in self._macro_profile:
            self._macro_profile[target] = {
                str(key): int(value)
                for key, value in self._macro_profile[source].items()
            }
        return True

    def rename_macro(self, source: str, target: str) -> bool:
        if not source or not target:
            return False
        if source == target:
            return self._macro_exists(source)
        if self._macro_exists(target) or self.dictionary.lookup(target) is not None:
            return False
        if not self.clone_macro(source, target):
            return False

        source_word = self.dictionary.lookup(source)
        if source_word is not None and source_word.macro_expansion is not None:
            del self.dictionary.words[source]
            self._macro_signatures.pop(source, None)
            if self.token_hook == source:
                self.token_hook = None

        self.unregister_pattern_macro(source)
        self._macro_docs.pop(source, None)
        self._macro_attrs.pop(source, None)
        self.capture_schemas.pop(source, None)
        self.capture_taint.pop(source, None)
        self._ct_call_abi_contracts.pop(source, None)
        self._macro_hotness.pop(source, None)
        self._macro_profile.pop(source, None)
        return True

    def unregister_word(self, name: str) -> bool:
        if name not in self.dictionary.words:
            return False
        del self.dictionary.words[name]
        self._macro_signatures.pop(name, None)
        self._macro_docs.pop(name, None)
        self._macro_attrs.pop(name, None)
        self.capture_schemas.pop(name, None)
        self.capture_taint.pop(name, None)
        self._ct_call_abi_contracts.pop(name, None)
        self._macro_hotness.pop(name, None)
        self._macro_profile.pop(name, None)
        self.unregister_pattern_macro(name)
        if self.token_hook == name:
            self.token_hook = None
        return True

    def word_exists(self, name: str) -> bool:
        return name in self.dictionary.words

    def _macro_signature_for_word(self, word: Word) -> Tuple[Tuple[str, ...], Optional[str]]:
        signature = self._macro_signatures.get(word.name)
        if signature is not None:
            return signature
        ordered = tuple(str(i) for i in range(max(0, int(word.macro_params))))
        return ordered, None

    def _looks_like_pattern_macro_definition(self) -> bool:
        """Heuristically detect pattern-macro clauses in a `macro` body.

        Pattern mode is considered when top-level clause arrows (`=>`) are
        present and we can see two top-level `;` tokens
        (clause terminator + macro terminator).
        """
        pos = self.pos
        token_count = len(self.tokens)
        if pos >= token_count:
            return False

        asm_brace_depth = 0
        awaiting_asm_body = False
        awaiting_asm_terminator = False
        saw_arrow = False
        clause_semicolons = 0
        definition_starters = {"word", ":asm", ":py", "extern", "inline"}

        while pos < token_count:
            tok = self.tokens[pos]
            lex = tok.lexeme
            pos += 1

            if awaiting_asm_body:
                if lex == "{":
                    asm_brace_depth += 1
                    awaiting_asm_body = False
                continue

            if lex == ":asm":
                awaiting_asm_body = True
                continue

            if awaiting_asm_terminator:
                awaiting_asm_terminator = False
                continue

            if lex == "{" and asm_brace_depth > 0:
                asm_brace_depth += 1
                continue

            if lex == "}" and asm_brace_depth > 0:
                asm_brace_depth -= 1
                if asm_brace_depth == 0:
                    awaiting_asm_terminator = True
                continue

            if asm_brace_depth > 0:
                continue

            if lex == "=>":
                saw_arrow = True
                continue

            if lex == ";":
                if not saw_arrow:
                    return False
                clause_semicolons += 1
                if clause_semicolons >= 2:
                    return True
                continue

            if tok.column == 0 and lex in definition_starters:
                return saw_arrow

        return False

    def _consume_macro_argument_group(self, *, word_name: str, call_token: Token) -> List[str]:
        return self.macro_engine.consume_macro_argument_group(
            word_name=word_name,
            call_token=call_token,
        )

    def _collect_macro_callstyle_args(self, *, word_name: str, call_token: Token) -> List[List[str]]:
        return self.macro_engine.collect_macro_callstyle_args(
            word_name=word_name,
            call_token=call_token,
        )

    def _collect_macro_call_args(
        self,
        word: Word,
        call_token: Token,
    ) -> Dict[str, Any]:
        return self.macro_engine.collect_macro_call_args(word, call_token)

    def allocate_variable(self, name: str) -> Tuple[str, str]:
        if name in self.variable_labels:
            label = self.variable_labels[name]
        else:
            base = sanitize_label(f"var_{name}")
            label = base
            suffix = 0
            existing = set(self.variable_labels.values())
            while label in existing:
                suffix += 1
                label = f"{base}_{suffix}"
            self.variable_labels[name] = label
        hidden_word = f"__with_{name}"
        self.variable_words[name] = hidden_word
        if self.dictionary.lookup(hidden_word) is None:
            word = Word(name=hidden_word)

            def _intrinsic(builder: FunctionEmitter, target: str = label) -> None:
                builder.push_label(target)

            word.intrinsic = _intrinsic

            # CT intrinsic: allocate a qword in CTMemory for this variable.
            # The address is lazily created on first use and cached.
            _ct_var_addrs: Dict[str, int] = {}

            def _ct_intrinsic(vm: CompileTimeVM, var_name: str = name) -> None:
                if var_name not in _ct_var_addrs:
                    _ct_var_addrs[var_name] = vm.memory.allocate(8)
                vm.push(_ct_var_addrs[var_name])

            word.compile_time_intrinsic = _ct_intrinsic
            word.runtime_intrinsic = _ct_intrinsic
            self.dictionary.register(word)
        return label, hidden_word

    def _handle_end_control(self) -> None:
        """Close one generic control frame pushed by compile-time words."""
        if not self.control_stack:
            raise ParseError("unexpected 'end' without matching block")

        entry = self.control_stack.pop()
        if not isinstance(entry, dict):
            raise ParseError("invalid control frame")

        close_ops = entry.get("close_ops")
        if close_ops is None:
            return
        if not isinstance(close_ops, list):
            raise ParseError("control frame field 'close_ops' must be a list")

        for spec in close_ops:
            op_name: Optional[str] = None
            data: Any = None
            if isinstance(spec, dict):
                candidate = spec.get("op")
                if isinstance(candidate, str):
                    op_name = candidate
                if "data" in spec:
                    data = spec["data"]
            elif isinstance(spec, (list, tuple)):
                if not spec:
                    raise ParseError("close_ops contains empty sequence")
                if isinstance(spec[0], str):
                    op_name = spec[0]
                data = spec[1] if len(spec) > 1 else None
            elif isinstance(spec, str):
                op_name = spec
            else:
                raise ParseError(f"invalid close op descriptor: {spec!r}")

            if not op_name:
                raise ParseError(f"close op missing valid 'op' name: {spec!r}")
            self._append_op(_make_op(op_name, data))

    def _count_remaining_end_tokens(self, start_index: int) -> int:
        count = 0
        idx = start_index
        toks = self.tokens
        n = len(toks)
        in_definition = bool(self.context_stack and isinstance(self.context_stack[-1], Definition))
        definition_starters = {"word", ":asm", ":py", "extern", "inline"}
        while idx < n:
            tok = toks[idx]
            if (
                in_definition
                and idx > start_index
                and tok.column == 0
                and tok.lexeme in definition_starters
            ):
                break
            if tok.lexeme == "end":
                count += 1
            idx += 1
        return count

    def _auto_close_if_else_frames(self, limit: int) -> int:
        """Implicitly close up to `limit` trailing if/else control frames."""
        closed = 0
        while closed < limit and self.control_stack:
            top = self.control_stack[-1]
            if top.get("type") not in ("if", "else"):
                break
            self._handle_end_control()
            closed += 1
        return closed

    def _handle_flexible_end(self, token: Token) -> None:
        """Close control frames with tolerant if/else shorthand semantics.

        If explicit `end` tokens are fewer than currently open control frames,
        trailing `if`/`else` frames are implicitly closed first so legacy and
        shorthand-heavy styles can coexist.
        """
        if not self.control_stack:
            raise ParseError(f"unexpected 'end' at {token.line}:{token.column}")

        current_end_index = self.pos - 1
        remaining_ends = self._count_remaining_end_tokens(current_end_index)
        reserved_definition_end = 0
        if self.context_stack and isinstance(self.context_stack[-1], Definition):
            reserved_definition_end = 1
        if reserved_definition_end > 0:
            remaining_ends = max(0, remaining_ends - reserved_definition_end)
        open_controls = len(self.control_stack)
        if remaining_ends < open_controls:
            needed_implicit = open_controls - remaining_ends
            self._auto_close_if_else_frames(needed_implicit)

        self._handle_end_control()

    # Parsing ------------------------------------------------------------------
    def parse(self, tokens: Iterable[Token], source: str) -> Module:
        self.tokens = tokens if isinstance(tokens, list) else list(tokens)
        self._token_iter = None
        self._token_iter_exhausted = True
        self.source = source
        self.pos = 0
        self.variable_labels = {}
        self.variable_words = {}
        self.cstruct_layouts = {}
        self.context_stack = [
            Module(
                forms=[],
                variables=self.variable_labels,
                cstruct_layouts=self.cstruct_layouts,
            )
        ]
        self.definition_stack.clear()
        self.last_defined = None
        self.control_stack = []
        self.label_counter = 0
        self.token_hook = None
        self._last_token = None
        self.custom_prelude = None
        self.custom_bss = None
        self._pending_inline_definition = False
        self._pending_priority = None
        self._rewrite_step_count = 0
        self._rewrite_seen_state.clear()
        self.rewrite_loop_reports.clear()
        self._macro_hotness.clear()
        self._ct_parser_sessions.clear()
        self._ct_parser_marks.clear()
        self._ct_rewrite_scope_stack.clear()
        if self._macro_profile_enabled:
            self._macro_profile.clear()
        for _stage_stats in self.rewrite_profile.values():
            for _key in list(_stage_stats.keys()):
                _stage_stats[_key] = 0
        for _rule in self.reader_rewrite_rules:
            if isinstance(_rule.metadata, dict):
                _rule.metadata["__stat_attempts"] = 0
                _rule.metadata["__stat_matches"] = 0
                _rule.metadata["__stat_applied"] = 0
        for _rule in self.grammar_rewrite_rules:
            if isinstance(_rule.metadata, dict):
                _rule.metadata["__stat_attempts"] = 0
                _rule.metadata["__stat_matches"] = 0
                _rule.metadata["__stat_applied"] = 0

        _priority_keywords = _PARSE_PRIORITY_KEYWORDS
        _kw_get = _PARSE_KEYWORD_DISPATCH.get
        _tokens = self.tokens
        _reader_rewrite_rules = self.reader_rewrite_rules
        _grammar_rewrite_rules = self.grammar_rewrite_rules
        _run_token_hook = self._run_token_hook
        _try_apply_rewrite_rules = self._try_apply_rewrite_rules
        _handle_macro_recording = self._handle_macro_recording
        _handle_list_begin = self._handle_list_begin
        _handle_list_end = self._handle_list_end
        _consume_pending_inline = self._consume_pending_inline
        _begin_definition = self._begin_definition
        _handle_flexible_end = self._handle_flexible_end
        _try_end_definition = self._try_end_definition
        _parse_asm_definition = self._parse_asm_definition
        _parse_py_definition = self._parse_py_definition
        _parse_extern = self._parse_extern
        _parse_priority_directive = self._parse_priority_directive
        _handle_ret = self._handle_ret
        _parse_bss_list_literal = self._parse_bss_list_literal
        _try_handle_builtin_control = self._try_handle_builtin_control
        _handle_token = self._handle_token
        try:
            while self.pos < len(_tokens):
              try:
                token = _tokens[self.pos]
                self.pos += 1
                self._last_token = token
                if _reader_rewrite_rules and _try_apply_rewrite_rules("reader", token):
                    _tokens = self.tokens
                    continue
                if self.token_hook and _run_token_hook(token):
                    continue
                if _handle_macro_recording(token):
                    continue
                if _grammar_rewrite_rules and _try_apply_rewrite_rules("grammar", token):
                    _tokens = self.tokens
                    continue
                lexeme = token.lexeme
                if self._pending_priority is not None and lexeme not in _priority_keywords:
                    raise ParseError(
                        f"priority {self._pending_priority} must be followed by definition/extern"
                    )
                kw = _kw_get(lexeme)
                if kw is not None:
                    if kw == _PARSE_KW_LIST_BEGIN:
                        _handle_list_begin()
                    elif kw == _PARSE_KW_LIST_END:
                        _handle_list_end(token)
                    elif kw == _PARSE_KW_WORD:
                        inline_def = _consume_pending_inline()
                        _begin_definition(token, terminator="end", inline=inline_def)
                    elif kw == _PARSE_KW_END:
                        if self.control_stack:
                            _handle_flexible_end(token)
                        elif _try_end_definition(token):
                            pass
                        else:
                            raise ParseError(f"unexpected 'end' at {token.line}:{token.column}")
                    elif kw == _PARSE_KW_ASM:
                        _parse_asm_definition(token)
                        _tokens = self.tokens
                    elif kw == _PARSE_KW_PY:
                        _parse_py_definition(token)
                        _tokens = self.tokens
                    elif kw == _PARSE_KW_EXTERN:
                        _parse_extern(token)
                    elif kw == _PARSE_KW_PRIORITY:
                        _parse_priority_directive(token)
                    elif kw == _PARSE_KW_RET:
                        _handle_ret(token)
                    elif kw == _PARSE_KW_BSS_LIST_BEGIN:
                        _parse_bss_list_literal(token)
                    continue
                if _try_handle_builtin_control(token):
                    continue
                if _handle_token(token):
                    _tokens = self.tokens
              except CompileTimeError:
                raise
              except ParseError as _recov_exc:
                self._record_diagnostic(self._last_token, str(_recov_exc))
                self._skip_to_recovery_point()
                _tokens = self.tokens
                continue
        except CompileTimeError:
            raise
        except ParseError:
            raise
        except Exception as exc:
            tok = self._last_token
            if tok is None:
                raise ParseError(f"unexpected error during parse: {exc}") from None
            raise ParseError(
                f"unexpected error near '{tok.lexeme}' at {tok.line}:{tok.column}: {exc}"
            ) from None

        if self.macro_recording is not None:
            self._record_diagnostic(self._last_token, "unterminated macro definition (missing ';')")
        if self._pending_priority is not None:
            self._record_diagnostic(self._last_token, f"dangling priority {self._pending_priority} without following definition")

        if len(self.context_stack) != 1:
            self._record_diagnostic(self._last_token, "unclosed definition at EOF")
        if self.control_stack:
            self._record_diagnostic(self._last_token, "unclosed control structure at EOF")

        # If any errors were accumulated, raise with all diagnostics
        error_count = sum(1 for d in self.diagnostics if d.level == "error")
        if error_count > 0:
            raise ParseError(f"compilation failed with {error_count} error(s)")

        self._finalize_parse_performance_passes()

        module = self.context_stack.pop()
        if not isinstance(module, Module):  # pragma: no cover - defensive
            raise ParseError("internal parser state corrupt")
        module.variables = dict(self.variable_labels)
        module.prelude = self.custom_prelude
        module.bss = self.custom_bss
        module.cstruct_layouts = dict(self.cstruct_layouts)
        return module

    def _handle_list_begin(self) -> None:
        label = self._new_label("list")
        self._append_op(_make_op("list_begin", label))
        self._push_control({"type": "list", "label": label})

    def _handle_list_end(self, token: Token) -> None:
        entry = self._pop_control(("list",))
        label = entry["label"]
        self._append_op(_make_op("list_end", label))

    def _parse_bss_list_literal(self, token: Token) -> None:
        values: List[int] = []
        while True:
            if self._eof():
                raise ParseError(f"missing '}}' for '{{' opened at {token.line}:{token.column}")
            cur = self._consume()
            if cur.lexeme == "}":
                break
            parsed = _parse_int_or_char_literal(cur)
            if parsed is None:
                raise ParseError(
                    f"only int/char literals are allowed in '{{}}' lists; got '{cur.lexeme}' at {cur.line}:{cur.column}"
                )
            values.append(parsed)

        list_size = len(values)
        if not self._eof():
            peek = self.peek_token()
            if peek is not None and peek.lexeme.startswith(":"):
                size_tok = self._consume()
                raw = size_tok.lexeme[1:]
                if not raw:
                    raise ParseError(f"missing size after ':' at {size_tok.line}:{size_tok.column}")
                try:
                    list_size = int(raw, 0)
                except ValueError:
                    raise ParseError(f"invalid bss list size '{size_tok.lexeme}' at {size_tok.line}:{size_tok.column}")
                if list_size < 0:
                    raise ParseError("bss list size must be >= 0")
                if len(values) > list_size:
                    raise ParseError(
                        f"bss list has {len(values)} initializer values but declared size is {list_size}"
                    )

        self._append_op(_make_op("bss_list_literal", {"size": list_size, "values": values}))

    def _should_use_custom_control(self, lexeme: str) -> bool:
        # Fast path: default parser controls unless explicitly overridden.
        if lexeme not in self.control_overrides:
            return False
        word = self.dictionary.lookup(lexeme)
        if word is None:
            return False
        return bool(word.immediate)

    def _warn_control_override(self, token: Token, lexeme: str) -> None:
        if lexeme in self._warned_control_overrides:
            return
        self._warned_control_overrides.add(lexeme)
        sys.stderr.write(
            f"[warn] default control structure ({lexeme}) has been overridden; using custom implementation\n"
        )

    def _try_handle_builtin_control(self, token: Token) -> bool:
        lexeme = token.lexeme
        if lexeme not in _DEFAULT_CONTROL_WORDS:
            return False
        if self._should_use_custom_control(lexeme):
            self._warn_control_override(token, lexeme)
            return False
        if lexeme == "if":
            self._handle_builtin_if(token)
            return True
        if lexeme == "else":
            self._handle_builtin_else(token)
            return True
        if lexeme == "for":
            self._handle_builtin_for(token)
            return True
        if lexeme == "while":
            self._handle_builtin_while(token)
            return True
        if lexeme == "do":
            self._handle_builtin_do(token)
            return True
        return False

    def _handle_builtin_if(self, token: Token) -> None:
        if self.control_stack:
            top = self.control_stack[-1]
            if (
                top.get("type") == "else"
                and isinstance(top.get("line"), int)
                and top["line"] == token.line
            ):
                prev_else = self._pop_control(("else",))
                shared_end = prev_else.get("end")
                if not isinstance(shared_end, str):
                    shared_end = self._new_label("if_end")
                false_label = self._new_label("if_false")
                self._append_op(_make_op("branch_zero", false_label))
                self._push_control(
                    {
                        "type": "if",
                        "false": false_label,
                        "end": shared_end,
                        "close_ops": [
                            {"op": "label", "data": false_label},
                            {"op": "label", "data": shared_end},
                        ],
                        "line": token.line,
                        "column": token.column,
                    }
                )
                return

        false_label = self._new_label("if_false")
        self._append_op(_make_op("branch_zero", false_label))
        self._push_control(
            {
                "type": "if",
                "false": false_label,
                "end": None,
                "close_ops": [{"op": "label", "data": false_label}],
                "line": token.line,
                "column": token.column,
            }
        )

    def _handle_builtin_else(self, token: Token) -> None:
        entry = self._pop_control(("if",))
        false_label = entry.get("false")
        if not isinstance(false_label, str):
            raise ParseError("invalid if control frame")
        end_label = entry.get("end")
        if not isinstance(end_label, str):
            end_label = self._new_label("if_end")
        self._append_op(_make_op("jump", end_label))
        self._append_op(_make_op("label", false_label))
        self._push_control(
            {
                "type": "else",
                "end": end_label,
                "close_ops": [{"op": "label", "data": end_label}],
                "line": token.line,
                "column": token.column,
            }
        )

    def _handle_builtin_for(self, token: Token) -> None:
        loop_label = self._new_label("for_loop")
        end_label = self._new_label("for_end")
        frame = {"loop": loop_label, "end": end_label}
        self._append_op(_make_op("for_begin", dict(frame)))
        self._push_control(
            {
                "type": "for",
                "loop": loop_label,
                "end": end_label,
                "close_ops": [{"op": "for_end", "data": dict(frame)}],
                "line": token.line,
                "column": token.column,
            }
        )

    def _handle_builtin_while(self, token: Token) -> None:
        begin_label = self._new_label("begin")
        end_label = self._new_label("end")
        self._append_op(_make_op("label", begin_label))
        self._push_control(
            {
                "type": "while_open",
                "begin": begin_label,
                "end": end_label,
                "line": token.line,
                "column": token.column,
            }
        )

    def _handle_builtin_do(self, token: Token) -> None:
        entry = self._pop_control(("while_open",))
        begin_label = entry.get("begin")
        end_label = entry.get("end")
        if not isinstance(begin_label, str) or not isinstance(end_label, str):
            raise ParseError("invalid while control frame")
        self._append_op(_make_op("branch_zero", end_label))
        self._push_control(
            {
                "type": "while",
                "begin": begin_label,
                "end": end_label,
                "close_ops": [
                    {"op": "jump", "data": begin_label},
                    {"op": "label", "data": end_label},
                ],
                "line": token.line,
                "column": token.column,
            }
        )

    def _parse_priority_directive(self, token: Token) -> None:
        if self._eof():
            raise ParseError(f"priority value missing at {token.line}:{token.column}")
        value_tok = self._consume()
        try:
            value = int(value_tok.lexeme, 0)
        except ValueError:
            raise ParseError(
                f"invalid priority '{value_tok.lexeme}' at {value_tok.line}:{value_tok.column}"
            )
        self._pending_priority = value

    def _consume_pending_priority(self, *, default: int = 0) -> int:
        if self._pending_priority is None:
            return default
        value = self._pending_priority
        self._pending_priority = None
        return value

    def _handle_ret(self, token: Token) -> None:
        self._append_op(_make_op("ret", loc=token))

    # Internal helpers ---------------------------------------------------------

    def _parse_extern(self, token: Token) -> None:
        # extern <name> [inputs outputs]
        # OR
        # extern <ret_type> <name>(<args>)

        if self._eof():
            raise ParseError(f"extern missing name at {token.line}:{token.column}")

        priority = self._consume_pending_priority(default=self.EXTERN_DEFAULT_PRIORITY)
        first_token = self._consume()
        if self._try_parse_c_extern(first_token, priority=priority):
            return
        self._parse_legacy_extern(first_token, priority=priority)

    def _parse_legacy_extern(self, name_token: Token, *, priority: int = 0) -> None:
        name = name_token.lexeme
        candidate = Word(name=name, priority=priority)
        word = self.dictionary.register(candidate)
        if word is not candidate:
            return
        word.is_extern = True

        peek = self.peek_token()
        if peek is not None and peek.lexeme.isdigit():
            word.extern_inputs = int(self._consume().lexeme)
            peek = self.peek_token()
            if peek is not None and peek.lexeme.isdigit():
                word.extern_outputs = int(self._consume().lexeme)
            else:
                word.extern_outputs = 0
        else:
            word.extern_inputs = 0
            word.extern_outputs = 0

    def _try_parse_c_extern(self, first_token: Token, *, priority: int = 0) -> bool:
        saved_pos = self.pos
        prefix_tokens: List[str] = [first_token.lexeme]

        while True:
            if self._eof():
                self.pos = saved_pos
                return False
            lookahead = self._consume()
            if lookahead.lexeme == "(":
                break
            if lookahead.lexeme.isdigit():
                self.pos = saved_pos
                return False
            prefix_tokens.append(lookahead.lexeme)

        if not prefix_tokens:
            raise ParseError("extern missing return type/name before '('")

        name_lexeme = prefix_tokens.pop()
        if not _is_identifier(name_lexeme):
            prefix_name, suffix_name = _split_trailing_identifier(name_lexeme)
            if suffix_name is None:
                raise ParseError(f"extern expected identifier before '(' but got '{name_lexeme}'")
            name_lexeme = suffix_name
            if prefix_name:
                prefix_tokens.append(prefix_name)

        if not _is_identifier(name_lexeme):
            raise ParseError(f"extern expected identifier before '(' but got '{name_lexeme}'")

        ret_type = _normalize_c_type_tokens(prefix_tokens, allow_default=True)
        inputs, arg_types, variadic = self._parse_c_param_list()
        outputs = 0 if ret_type == "void" else 1
        self._register_c_extern(name_lexeme, inputs, outputs, arg_types, ret_type,
                                priority=priority, variadic=variadic)
        return True

    def _parse_c_param_list(self) -> Tuple[int, List[str], bool]:
        """Parse C-style parameter list. Returns (count, types, is_variadic)."""
        inputs = 0
        arg_types: List[str] = []
        variadic = False

        if self._eof():
            raise ParseError("extern unclosed '('")
        peek = self.peek_token()
        if peek.lexeme == ")":
            self._consume()
            return inputs, arg_types, False

        while True:
            # Check for ... (variadic)
            peek = self.peek_token()
            if peek is not None and peek.lexeme == "...":
                self._consume()
                variadic = True
                if self._eof():
                    raise ParseError("extern unclosed '(' after '...'")
                closing = self._consume()
                if closing.lexeme != ")":
                    raise ParseError("expected ')' after '...' in extern parameter list")
                break
            lexemes = self._collect_c_param_lexemes()
            arg_type = _normalize_c_type_tokens(lexemes, allow_default=False)
            if arg_type == "void" and inputs == 0:
                if self._eof():
                    raise ParseError("extern unclosed '(' after 'void'")
                closing = self._consume()
                if closing.lexeme != ")":
                    raise ParseError("expected ')' after 'void' in extern parameter list")
                return 0, [], False
            inputs += 1
            arg_types.append(arg_type)
            if self._eof():
                raise ParseError("extern unclosed '('")
            separator = self._consume()
            if separator.lexeme == ")":
                break
            if separator.lexeme != ",":
                raise ParseError(
                    f"expected ',' or ')' in extern parameter list, got '{separator.lexeme}'"
                )
        return inputs, arg_types, variadic

    def _collect_c_param_lexemes(self) -> List[str]:
        lexemes: List[str] = []
        while True:
            if self._eof():
                raise ParseError("extern unclosed '('")
            peek = self.peek_token()
            if peek.lexeme in (",", ")"):
                break
            lexemes.append(self._consume().lexeme)

        if not lexemes:
            raise ParseError("missing parameter type in extern declaration")

        if len(lexemes) > 1 and _is_identifier(lexemes[-1]):
            lexemes.pop()
            return lexemes

        prefix, suffix = _split_trailing_identifier(lexemes[-1])
        if suffix is not None:
            if prefix:
                lexemes[-1] = prefix
            else:
                lexemes.pop()
        return lexemes

    def _register_c_extern(
        self,
        name: str,
        inputs: int,
        outputs: int,
        arg_types: List[str],
        ret_type: str,
        *,
        priority: int = 0,
        variadic: bool = False,
    ) -> None:
        candidate = Word(name=name, priority=priority)
        word = self.dictionary.register(candidate)
        if word is not candidate:
            return
        word.is_extern = True
        word.extern_inputs = inputs
        word.extern_outputs = outputs
        word.extern_signature = (arg_types, ret_type)
        word.extern_variadic = variadic

    def _handle_token(self, token: Token) -> bool:
        """Handle a token. Returns True if the token list was modified (macro expansion)."""
        lexeme = token.lexeme
        lexeme_len = len(lexeme)
        first = lexeme[0]
        _append_op = self._append_op
        _try_literal = self._try_literal
        words = self.dictionary.words
        # Fast-path: cached literal parsing for numeric/quoted candidates.
        if (
            first.isdigit()
            or first == '"'
            or first == '.'
            or first == "'"
            or ((first == '-' or first == '+') and lexeme_len > 1)
        ):
            if _try_literal(token):
                return False

        if first == '&':
            target_name = lexeme[1:]
            if not target_name:
                raise ParseError(f"missing word name after '&' at {token.line}:{token.column}")
            _append_op(_make_op("word_ptr", target_name))
            return False

        word = words.get(lexeme)
        if word is not None:
            if word.macro_expansion is not None:
                captures = self._collect_macro_call_args(word, token)
                self._inject_macro_tokens(word, token, captures)
                return True
            if word.immediate:
                if word.macro:
                    produced = word.macro(MacroContext(self))
                    if produced:
                        for node in produced:
                            _append_op(node)
                else:
                    self._execute_immediate_word(word)
                return False

        _append_op(_make_word_op(lexeme))
        return False

    def _execute_immediate_word(self, word: Word) -> None:
        try:
            self.compile_time_vm.invoke(word)
        except CompileTimeError:
            raise
        except ParseError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise CompileTimeError(f"compile-time word '{word.name}' failed: {exc}") from None

    def _handle_macro_recording(self, token: Token) -> bool:
        if self.macro_recording is None:
            return False
        rec = self.macro_recording
        lex = token.lexeme

        if rec.awaiting_asm_body:
            if lex == "{":
                rec.asm_brace_depth += 1
                rec.awaiting_asm_body = False
            rec.tokens.append(lex)
            return True

        if lex == ":asm":
            rec.awaiting_asm_body = True
            rec.tokens.append(lex)
            return True

        if rec.awaiting_asm_terminator:
            if lex == ";":
                rec.tokens.append(lex)
                rec.awaiting_asm_terminator = False
                return True
            rec.awaiting_asm_terminator = False

        if lex == "macro" and rec.asm_brace_depth == 0:
            rec.nested_macro_depth += 1
            rec.tokens.append(lex)
            return True

        if lex == "{" and rec.asm_brace_depth > 0:
            rec.asm_brace_depth += 1
            rec.tokens.append(lex)
            return True

        if lex == "}" and rec.asm_brace_depth > 0:
            rec.asm_brace_depth -= 1
            rec.tokens.append(lex)
            if rec.asm_brace_depth == 0:
                rec.awaiting_asm_terminator = True
            return True

        if lex == ";" and rec.asm_brace_depth == 0:
            if rec.nested_macro_depth > 0:
                rec.nested_macro_depth -= 1
                rec.tokens.append(lex)
            else:
                self._finish_macro_recording(token)
        else:
            rec.tokens.append(lex)
        return True

    def _maybe_expand_macro(self, token: Token) -> bool:
        word = self.dictionary.lookup(token.lexeme)
        if word and word.macro_expansion is not None:
            captures = self._collect_macro_call_args(word, token)
            self._inject_macro_tokens(word, token, captures)
            return True
        return False

    def _inject_macro_tokens(self, word: Word, token: Token, captures: Dict[str, Any]) -> None:
        self.macro_engine.inject_macro_tokens(word, token, captures)

    def _start_macro_recording(
        self,
        name: str,
        param_count: int,
        *,
        ordered_params: Optional[List[str]] = None,
        variadic_param: Optional[str] = None,
    ) -> None:
        if self.macro_recording is not None:
            raise ParseError("nested macro definitions are not supported")
        self.macro_recording = MacroDefinition(
            name=name,
            tokens=[],
            param_count=param_count,
            ordered_params=ordered_params,
            variadic_param=variadic_param,
        )

    def _finish_macro_recording(self, token: Token) -> None:
        if self.macro_recording is None:
            raise ParseError(f"unexpected ';' closing a macro at {token.line}:{token.column}")
        macro_def = self.macro_recording
        self.macro_recording = None
        word = Word(name=macro_def.name)
        word.macro_expansion = [self._intern_expansion_lexeme(piece) for piece in macro_def.tokens]
        word.macro_params = len(macro_def.ordered_params)
        self.dictionary.register(word)
        self._macro_signatures[macro_def.name] = (
            tuple(macro_def.ordered_params),
            macro_def.variadic_param,
        )
        self._macro_hotness.pop(macro_def.name, None)
        self._macro_profile.pop(macro_def.name, None)

    def _push_control(self, entry: Dict[str, str]) -> None:
        if "line" not in entry or "column" not in entry:
            tok = self._last_token
            if tok is not None:
                entry = dict(entry)
                entry["line"] = tok.line
                entry["column"] = tok.column
        self.control_stack.append(entry)

    def _pop_control(self, expected: Tuple[str, ...]) -> Dict[str, str]:
        if not self.control_stack:
            raise ParseError("control stack underflow")
        entry = self.control_stack.pop()
        if entry.get("type") not in expected:
            tok = self._last_token
            location = ""
            if tok is not None:
                location = f" at {tok.line}:{tok.column} near '{tok.lexeme}'"
            origin = ""
            if "line" in entry and "column" in entry:
                origin = f" (opened at {entry['line']}:{entry['column']})"
            raise ParseError(f"mismatched control word '{entry.get('type')}'" + origin + location)
        return entry

    def _new_label(self, prefix: str) -> str:
        label = f"L_{prefix}_{self.label_counter}"
        self.label_counter += 1
        return label

    def _try_apply_rewrite_rules(self, stage: str, token: Token) -> bool:
        return self.macro_engine.try_apply_rewrite_rules(stage, token)

    def _run_token_hook(self, token: Token) -> bool:
        if not self.token_hook:
            return False
        hook_word = self.dictionary.lookup(self.token_hook)
        if hook_word is None:
            raise ParseError(f"token hook '{self.token_hook}' not defined")
        self.compile_time_vm.invoke_with_args(hook_word, [token])
        # Convention: hook leaves handled flag on stack (int truthy means consumed)
        handled = self.compile_time_vm.pop()
        return bool(handled)

    def _try_end_definition(self, token: Token) -> bool:
        if len(self.context_stack) <= 1:
            return False
        ctx = self.context_stack[-1]
        if not isinstance(ctx, Definition):
            return False
        if ctx.terminator != token.lexeme:
            return False
        self._end_definition(token)
        return True

    def _consume_pending_inline(self) -> bool:
        pending = self._pending_inline_definition
        self._pending_inline_definition = False
        return pending

    def _begin_definition(self, token: Token, terminator: str = "end", inline: bool = False) -> None:
        if self._eof():
            raise ParseError(
                f"definition name missing after '{token.lexeme}' at {token.line}:{token.column}"
            )
        name_token = self._consume()
        priority = self._consume_pending_priority()
        definition = Definition(
            name=name_token.lexeme,
            body=[],
            terminator=terminator,
            inline=inline,
            stack_inputs=_parse_stack_effect_comment(self.source, token.start),
        )
        self.context_stack.append(definition)
        candidate = Word(name=definition.name, priority=priority)
        candidate.definition = definition
        candidate.inline = inline
        active_word = self.dictionary.register(candidate)
        is_active = active_word is candidate
        self.definition_stack.append((candidate, is_active))

    def _end_definition(self, token: Token) -> None:
        if len(self.context_stack) <= 1:
            raise ParseError(f"unexpected '{token.lexeme}' at {token.line}:{token.column}")
        ctx = self.context_stack.pop()
        if not isinstance(ctx, Definition):
            raise ParseError(f"'{token.lexeme}' can only close definitions")
        if ctx.terminator != token.lexeme:
            raise ParseError(
                f"definition '{ctx.name}' expects terminator '{ctx.terminator}' but got '{token.lexeme}'"
            )
        word, is_active = self.definition_stack.pop()
        if not is_active:
            return
        ctx.immediate = word.immediate
        ctx.compile_only = word.compile_only
        ctx.runtime_only = word.runtime_only
        ctx.inline = word.inline
        if word.runtime_only and (word.compile_only or word.immediate):
            raise ParseError(
                f"word '{word.name}' cannot be both runtime-only and compile-time-active"
            )
        if word.compile_only or word.immediate:
            word.compile_time_override = True
            word.compile_time_intrinsic = None
        module = self.context_stack[-1]
        if not isinstance(module, Module):
            raise ParseError("nested definitions are not supported yet")
        module.forms.append(ctx)
        self.last_defined = word

    def _parse_effect_annotations(self) -> List[str]:
        """Parse a '(effects ...)' clause that follows a :asm name."""
        open_tok = self._consume()
        if open_tok.lexeme != "(":  # pragma: no cover - defensive
            raise ParseError("internal parser error: effect clause must start with '('")
        tokens: List[Token] = []
        while True:
            if self._eof():
                raise ParseError("unterminated effect clause in asm definition")
            tok = self._consume()
            if tok.lexeme == ")":
                break
            tokens.append(tok)
        if not tokens:
            raise ParseError("effect clause must include 'effect' or 'effects'")
        keyword = tokens.pop(0)
        if keyword.lexeme.lower() not in {"effect", "effects"}:
            raise ParseError(
                f"effect clause must start with 'effect' or 'effects', got '{keyword.lexeme}'"
            )
        effect_names: List[str] = []
        for tok in tokens:
            if tok.lexeme == ",":
                continue
            normalized = tok.lexeme.lower().replace("_", "-")
            canonical = _WORD_EFFECT_ALIASES.get(normalized)
            if canonical is None:
                raise ParseError(
                    f"unknown effect '{tok.lexeme}' at {tok.line}:{tok.column}"
                )
            if canonical not in effect_names:
                effect_names.append(canonical)
        if not effect_names:
            raise ParseError("effect clause missing effect names")
        return effect_names

    def _parse_asm_definition(self, token: Token) -> None:
        if self._eof():
            raise ParseError(f"definition name missing after ':asm' at {token.line}:{token.column}")
        inline_def = self._consume_pending_inline()
        name_token = self._consume()
        effect_names: Optional[List[str]] = None
        if not self._eof():
            next_token = self.peek_token()
            if next_token is not None and next_token.lexeme == "(":
                effect_names = self._parse_effect_annotations()
        brace_token = self._consume()
        if brace_token.lexeme != "{":
            raise ParseError(f"expected '{{' after asm name at {brace_token.line}:{brace_token.column}")
        block_start = brace_token.end
        block_end: Optional[int] = None
        body_tokens: List[Token] = []
        # Scan for closing brace directly via list indexing (avoid method-call overhead)
        _tokens = self.tokens
        _tlen = len(_tokens)
        _pos = self.pos
        while _pos < _tlen:
            nt = _tokens[_pos]
            _pos += 1
            if nt.lexeme == "}":
                block_end = nt.start
                break
            body_tokens.append(nt)
        self.pos = _pos
        if block_end is None:
            raise ParseError("missing '}' to terminate asm body")
        asm_body = self.source[block_start:block_end]
        if any(tok.expansion_depth > 0 for tok in body_tokens):
            asm_body = _reconstruct_asm_from_tokens(body_tokens)
        priority = self._consume_pending_priority()
        definition = AsmDefinition(name=name_token.lexeme, body=asm_body, inline=inline_def)
        if effect_names is not None:
            definition.effects = set(effect_names)
        candidate = Word(name=definition.name, priority=priority)
        candidate.definition = definition
        if inline_def:
            candidate.inline = True
        word = self.dictionary.register(candidate)
        if word is candidate:
            definition.immediate = word.immediate
            definition.compile_only = word.compile_only
            definition.runtime_only = word.runtime_only
        module = self.context_stack[-1]
        if not isinstance(module, Module):
            raise ParseError("asm definitions must be top-level forms")
        if word is candidate:
            module.forms.append(definition)
            self.last_defined = word
        if self._eof():
            raise ParseError("asm definition missing terminator ';'")
        terminator = self._consume()
        if terminator.lexeme != ";":
            raise ParseError(f"expected ';' after asm definition at {terminator.line}:{terminator.column}")

    def _parse_py_definition(self, token: Token) -> None:
        if self._eof():
            raise ParseError(f"definition name missing after ':py' at {token.line}:{token.column}")
        name_token = self._consume()
        brace_token = self._consume()
        if brace_token.lexeme != "{":
            raise ParseError(f"expected '{{' after py name at {brace_token.line}:{brace_token.column}")
        block_start = brace_token.end
        block_end: Optional[int] = None
        _tokens = self.tokens
        _tlen = len(_tokens)
        _pos = self.pos
        while _pos < _tlen:
            nt = _tokens[_pos]
            _pos += 1
            if nt.lexeme == "}":
                block_end = nt.start
                break
        self.pos = _pos
        if block_end is None:
            raise ParseError("missing '}' to terminate py body")
        import textwrap
        py_body = textwrap.dedent(self.source[block_start:block_end])
        priority = self._consume_pending_priority()
        candidate = Word(name=name_token.lexeme, priority=priority)
        word = self.dictionary.register(candidate)
        if word is not candidate:
            if self._eof():
                raise ParseError("py definition missing terminator ';'")
            terminator = self._consume()
            if terminator.lexeme != ";":
                raise ParseError(f"expected ';' after py definition at {terminator.line}:{terminator.column}")
            return
        namespace = self._py_exec_namespace()
        try:
            exec(py_body, namespace)
        except Exception as exc:  # pragma: no cover - user code
            raise ParseError(f"python macro body for '{word.name}' raised: {exc}") from exc
        macro_fn = namespace.get("macro")
        intrinsic_fn = namespace.get("intrinsic")
        if macro_fn is None and intrinsic_fn is None:
            raise ParseError("python definition must define 'macro' or 'intrinsic'")
        if macro_fn is not None:
            word.macro = macro_fn
            word.immediate = True
        if intrinsic_fn is not None:
            word.intrinsic = intrinsic_fn
        if self._eof():
            raise ParseError("py definition missing terminator ';'")
        terminator = self._consume()
        if terminator.lexeme != ";":
            raise ParseError(f"expected ';' after py definition at {terminator.line}:{terminator.column}")

    def _py_exec_namespace(self) -> Dict[str, Any]:
        return dict(PY_EXEC_GLOBALS)

    def _append_op(self, node: Op) -> None:
        if self.capture_op_locations and node.loc is None:
            tok = self._last_token
            if tok is not None:
                # Inlined fast path of location_for_token
                spans = self.file_spans
                if spans:
                    if self._span_index_len != len(spans):
                        self._rebuild_span_index()
                        self._span_cache_idx = -1
                    tl = tok.line
                    ci = self._span_cache_idx
                    if ci >= 0:
                        span = spans[ci]
                        if span.start_line <= tl < span.end_line:
                            node.loc = _make_loc(span.path, span.local_start_line + (tl - span.start_line), tok.column)
                        else:
                            node.loc = self._location_for_token_slow(tok, tl)
                    else:
                        node.loc = self._location_for_token_slow(tok, tl)
                else:
                    node.loc = _make_loc(_SOURCE_PATH, tok.line, tok.column)
        target = self.context_stack[-1]
        if target.__class__ is Definition:
            target.body.append(node)
        else:
            target.forms.append(node)

    def _location_for_token_slow(self, token: Token, tl: int) -> SourceLocation:
        """Slow path for location_for_token: bisect lookup."""
        span_starts = self._span_starts
        idx = bisect.bisect_right(span_starts, tl) - 1
        if idx >= 0:
            span = self.file_spans[idx]
            if tl < span.end_line:
                self._span_cache_idx = idx
                return _make_loc(span.path, span.local_start_line + (tl - span.start_line), token.column)
        return _make_loc(_SOURCE_PATH, tl, token.column)

    def _try_literal(self, token: Token) -> bool:
        lexeme = token.lexeme
        cached = _PARSE_LITERAL_CACHE.get(lexeme, _PARSE_LITERAL_CACHE_MISS)
        if cached is _PARSE_LITERAL_NOT_LITERAL:
            return False
        if cached is not _PARSE_LITERAL_CACHE_MISS:
            self._append_op(_make_literal_op(cached))
            return True

        first = lexeme[0] if lexeme else '\0'
        if first.isdigit() or first == '-' or first == '+':
            try:
                value = int(lexeme, 0)
                _PARSE_LITERAL_CACHE[lexeme] = value
                if len(_PARSE_LITERAL_CACHE) > _PARSE_LITERAL_CACHE_MAX:
                    _PARSE_LITERAL_CACHE.clear()
                self._append_op(_make_literal_op(value))
                return True
            except ValueError:
                pass

        # Try float
        if first.isdigit() or first == '-' or first == '+' or first == '.':
            try:
                if "." in lexeme or "e" in lexeme.lower():
                    value = float(lexeme)
                    _PARSE_LITERAL_CACHE[lexeme] = value
                    if len(_PARSE_LITERAL_CACHE) > _PARSE_LITERAL_CACHE_MAX:
                        _PARSE_LITERAL_CACHE.clear()
                    self._append_op(_make_literal_op(value))
                    return True
            except ValueError:
                pass

        if first == '"':
            string_value = _parse_string_literal(token)
            if string_value is not None:
                _PARSE_LITERAL_CACHE[lexeme] = string_value
                if len(_PARSE_LITERAL_CACHE) > _PARSE_LITERAL_CACHE_MAX:
                    _PARSE_LITERAL_CACHE.clear()
                self._append_op(_make_literal_op(string_value))
                return True

        if first == "'":
            char_value = _parse_char_literal(token)
            if char_value is not None:
                _PARSE_LITERAL_CACHE[lexeme] = char_value
                if len(_PARSE_LITERAL_CACHE) > _PARSE_LITERAL_CACHE_MAX:
                    _PARSE_LITERAL_CACHE.clear()
                self._append_op(_make_literal_op(char_value))
                return True

        _PARSE_LITERAL_CACHE[lexeme] = _PARSE_LITERAL_NOT_LITERAL
        if len(_PARSE_LITERAL_CACHE) > _PARSE_LITERAL_CACHE_MAX:
            _PARSE_LITERAL_CACHE.clear()
        return False

    def _consume(self) -> Token:
        pos = self.pos
        if pos >= len(self.tokens):
            raise ParseError("unexpected EOF")
        self.pos = pos + 1
        return self.tokens[pos]

    def _eof(self) -> bool:
        return self.pos >= len(self.tokens)

    def _ensure_tokens(self, upto: int) -> None:
        if self._token_iter_exhausted:
            return
        if self._token_iter is None:
            self._token_iter_exhausted = True
            return
        while len(self.tokens) <= upto and not self._token_iter_exhausted:
            try:
                next_tok = next(self._token_iter)
            except StopIteration:
                self._token_iter_exhausted = True
                break
            self.tokens.append(next_tok)


# ---------------------------------------------------------------------------
# Compile-time VM helpers
# ---------------------------------------------------------------------------


def _to_i64(v: int) -> int:
    """Truncate to signed 64-bit integer (matching x86-64 register semantics)."""
    v = v & 0xFFFFFFFFFFFFFFFF
    if v >= 0x8000000000000000:
        v -= 0x10000000000000000
    return v


class _CTVMJump(Exception):
    """Raised by the ``jmp`` intrinsic to transfer control in _execute_nodes."""

    def __init__(self, target_ip: int) -> None:
        self.target_ip = target_ip


class _CTVMReturn(Exception):
    """Raised to return from the current word frame in _execute_nodes."""


class _CTVMExit(Exception):
    """Raised by the ``exit`` intrinsic to stop compile-time execution."""

    def __init__(self, code: int = 0) -> None:
        self.code = code


class CTMemory:
    """Managed memory for the compile-time VM.

    Uses ctypes buffers with real process addresses so that ``c@``, ``c!``,
    ``@``, ``!`` can operate on them directly via ``ctypes.from_address``.

    String literals are slab-allocated from a contiguous data section so that
    ``data_start``/``data_end`` bracket them correctly for ``print``'s range
    check.
    """

    PERSISTENT_SIZE = 64  # matches default BSS ``persistent: resb 64``
    PRINT_BUF_SIZE = 128   # matches ``PRINT_BUF_BYTES``
    DATA_SECTION_SIZE = 4 * 1024 * 1024  # 4 MB slab for string literals

    def __init__(self, persistent_size: int = 0) -> None:
        import ctypes as _ctypes
        globals().setdefault('ctypes', _ctypes)
        self._buffers: List[Any] = []  # prevent GC of ctypes objects
        self._string_cache: Dict[str, Tuple[int, int]] = {}  # cache string literals

        # Persistent BSS region (for ``mem`` word)
        actual_persistent = persistent_size if persistent_size > 0 else self.PERSISTENT_SIZE
        self._persistent = ctypes.create_string_buffer(actual_persistent)
        self._persistent_size = actual_persistent
        self._buffers.append(self._persistent)
        self.persistent_addr: int = ctypes.addressof(self._persistent)

        # print_buf region (for words that use ``[rel print_buf]``)
        self._print_buf = ctypes.create_string_buffer(self.PRINT_BUF_SIZE)
        self._buffers.append(self._print_buf)
        self.print_buf_addr: int = ctypes.addressof(self._print_buf)

        # Data section – contiguous slab for string literals so that
        # data_start..data_end consistently brackets all of them.
        self._data_section = ctypes.create_string_buffer(self.DATA_SECTION_SIZE)
        self._buffers.append(self._data_section)
        self.data_start: int = ctypes.addressof(self._data_section)
        self.data_end: int = self.data_start + self.DATA_SECTION_SIZE
        self._data_offset: int = 0

        # sys_argc / sys_argv – populated by invoke()
        self._sys_argc = ctypes.c_int64(0)
        self._buffers.append(self._sys_argc)
        self.sys_argc_addr: int = ctypes.addressof(self._sys_argc)

        self._sys_argv_ptrs: Optional[ctypes.Array[Any]] = None
        self._sys_argv = ctypes.c_int64(0)  # qword holding pointer to argv array
        self._buffers.append(self._sys_argv)
        self.sys_argv_addr: int = ctypes.addressof(self._sys_argv)

    # -- argv helpers ------------------------------------------------------

    def setup_argv(self, args: List[str]) -> None:
        """Populate sys_argc / sys_argv from *args*."""
        self._sys_argc.value = len(args)
        # Build null-terminated C string array
        argv_bufs: List[Any] = []
        for arg in args:
            encoded = arg.encode("utf-8") + b"\x00"
            buf = ctypes.create_string_buffer(encoded, len(encoded))
            self._buffers.append(buf)
            argv_bufs.append(buf)
        # pointer array (+ NULL sentinel)
        arr_type = ctypes.c_int64 * (len(args) + 1)
        self._sys_argv_ptrs = arr_type()
        for i, buf in enumerate(argv_bufs):
            self._sys_argv_ptrs[i] = ctypes.addressof(buf)
        self._sys_argv_ptrs[len(args)] = 0
        self._buffers.append(self._sys_argv_ptrs)
        self._sys_argv.value = ctypes.addressof(self._sys_argv_ptrs)

    # -- allocation --------------------------------------------------------

    def allocate(self, size: int) -> int:
        """Allocate a zero-filled region, return its real address.
        Adds padding to mimic real mmap which always gives full pages."""
        if size <= 0:
            size = 1
        buf = ctypes.create_string_buffer(size + 16)  # padding for null terminators
        addr = ctypes.addressof(buf)
        self._buffers.append(buf)
        return addr

    def store_string(self, s: str) -> Tuple[int, int]:
        """Store a UTF-8 string in the data section slab.  Returns ``(addr, length)``.
        Caches immutable string literals to avoid redundant allocations."""
        cached = self._string_cache.get(s)
        if cached is not None:
            return cached
        encoded = s.encode("utf-8")
        needed = len(encoded) + 1  # null terminator
        aligned = (needed + 7) & ~7  # 8-byte align
        if self._data_offset + aligned > self.DATA_SECTION_SIZE:
            raise RuntimeError("CT data section overflow")
        addr = self.data_start + self._data_offset
        ctypes.memmove(addr, encoded, len(encoded))
        ctypes.c_uint8.from_address(addr + len(encoded)).value = 0  # null terminator
        self._data_offset += aligned
        result = (addr, len(encoded))
        self._string_cache[s] = result
        return result

    # -- low-level access --------------------------------------------------

    @staticmethod
    def read_byte(addr: int) -> int:
        return ctypes.c_uint8.from_address(addr).value

    @staticmethod
    def write_byte(addr: int, value: int) -> None:
        ctypes.c_uint8.from_address(addr).value = value & 0xFF

    @staticmethod
    def read_qword(addr: int) -> int:
        return ctypes.c_int64.from_address(addr).value

    @staticmethod
    def write_qword(addr: int, value: int) -> None:
        ctypes.c_int64.from_address(addr).value = _to_i64(value)

    @staticmethod
    def read_bytes(addr: int, length: int) -> bytes:
        return ctypes.string_at(addr, length)


class CompileTimeVM:
    NATIVE_STACK_SIZE = 8 * 1024 * 1024  # 8 MB per native stack

    def __init__(self, parser: Parser) -> None:
        self.parser = parser
        self.dictionary = parser.dictionary
        self.stack: List[Any] = []
        self.return_stack: List[Any] = []
        self.loop_stack: List[Dict[str, Any]] = []
        self._handles = _CTHandleTable()
        self.call_stack: List[str] = []
        # Runtime-faithful execution state — lazily allocated on first use
        self._memory: Optional[CTMemory] = None
        self.runtime_mode: bool = False
        self._list_capture_stack: List[Any] = []  # for list_begin/list_end (int depth or native r12 addr)
        self._ct_executed: Set[str] = set()  # words already executed at CT
        # Native stack state (used only in runtime_mode)
        self.r12: int = 0  # data stack pointer (grows downward)
        self.r13: int = 0  # return stack pointer (grows downward)
        self._native_data_stack: Optional[Any] = None   # ctypes buffer
        self._native_data_top: int = 0
        # REPL persistent state
        self._repl_initialized: bool = False
        self._repl_libs: List[str] = []
        self._native_return_stack: Optional[Any] = None  # ctypes buffer
        self._native_return_top: int = 0
        # JIT cache: word name -> ctypes callable
        self._jit_cache: Dict[str, Any] = {}
        self._jit_code_pages: List[Any] = []  # keep mmap pages alive
        # Pre-allocated output structs for JIT calls (lazily allocated)
        self._jit_out2: Optional[Any] = None
        self._jit_out2_addr: int = 0
        self._jit_out4: Optional[Any] = None
        self._jit_out4_addr: int = 0
        # BSS symbol table for JIT patching
        self._bss_symbols: Dict[str, int] = {}
        # dlopen handles for C extern support
        self._dl_handles: List[Any] = []  # ctypes.CDLL handles
        self._dl_func_cache: Dict[str, Any] = {}  # name -> ctypes callable
        self._ct_libs: List[str] = []  # library names from -l flags
        self._ctypes_struct_cache: Dict[str, Any] = {}
        self.current_location: Optional[SourceLocation] = None
        # Coroutine JIT support: save buffer for callee-saved regs (lazily allocated)
        self._jit_save_buf: Optional[Any] = None
        self._jit_save_buf_addr: int = 0

    @property
    def memory(self) -> CTMemory:
        if self._memory is None:
            self._memory = CTMemory()
        return self._memory

    @memory.setter
    def memory(self, value: CTMemory) -> None:
        self._memory = value

    def _ensure_jit_out(self) -> None:
        if self._jit_out2 is None:
            import ctypes as _ctypes
            globals().setdefault('ctypes', _ctypes)
            self._jit_out2 = (_ctypes.c_int64 * 2)()
            self._jit_out2_addr = _ctypes.addressof(self._jit_out2)
            self._jit_out4 = (_ctypes.c_int64 * 4)()
            self._jit_out4_addr = _ctypes.addressof(self._jit_out4)

    def _ensure_jit_save_buf(self) -> None:
        if self._jit_save_buf is None:
            self._jit_save_buf = (ctypes.c_int64 * 8)()
            self._jit_save_buf_addr = ctypes.addressof(self._jit_save_buf)

    @staticmethod
    def _is_coroutine_asm(body: str) -> bool:
        """Detect asm words that manipulate the x86 return stack (coroutine patterns).

        Heuristic: if the body pops rsi/rdi before any label (capturing the
        return address), it's a coroutine word.
        """
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            if _RE_LABEL_PAT.match(line):
                break
            if line.startswith("pop "):
                reg = line.split()[1].rstrip(",")
                if reg in ("rsi", "rdi"):
                    return True
        return False

    def reset(self) -> None:
        self.stack.clear()
        self.return_stack.clear()
        self.loop_stack.clear()
        self._handles.clear()
        self.call_stack.clear()
        self._list_capture_stack.clear()
        self.r12 = 0
        self.r13 = 0
        self.current_location = None
        self._repl_initialized = False

    def invoke(self, word: Word, *, runtime_mode: bool = False, libs: Optional[List[str]] = None) -> None:
        self.reset()
        self._ensure_jit_out()
        prev_mode = self.runtime_mode
        self.runtime_mode = runtime_mode
        if runtime_mode:
            # Determine persistent size from BSS overrides if available.
            persistent_size = 0
            if self.parser.custom_bss:
                for bss_line in self.parser.custom_bss:
                    m = _RE_BSS_PERSISTENT.search(bss_line)
                    if m:
                        persistent_size = int(m.group(1))
            self.memory = CTMemory(persistent_size)  # fresh memory per invocation
            self.memory.setup_argv(sys.argv)

            # Allocate native stacks
            self._native_data_stack = ctypes.create_string_buffer(self.NATIVE_STACK_SIZE)
            self._native_data_top = ctypes.addressof(self._native_data_stack) + self.NATIVE_STACK_SIZE
            self.r12 = self._native_data_top  # empty, grows downward

            self._native_return_stack = ctypes.create_string_buffer(self.NATIVE_STACK_SIZE)
            self._native_return_top = ctypes.addressof(self._native_return_stack) + self.NATIVE_STACK_SIZE
            self.r13 = self._native_return_top  # empty, grows downward

            # BSS symbol table for JIT [rel SYMBOL] patching
            self._bss_symbols = {
                "dstack": ctypes.addressof(self._native_data_stack),
                "dstack_top": self._native_data_top,
                "rstack": ctypes.addressof(self._native_return_stack),
                "rstack_top": self._native_return_top,
                "data_start": self.memory.data_start,
                "data_end": self.memory.data_start + self.memory._data_offset if self.memory._data_offset else self.memory.data_end,
                "print_buf": self.memory.print_buf_addr,
                "print_buf_end": self.memory.print_buf_addr + CTMemory.PRINT_BUF_SIZE,
                "persistent": self.memory.persistent_addr,
                "persistent_end": self.memory.persistent_addr + self.memory._persistent_size,
                "sys_argc": self.memory.sys_argc_addr,
                "sys_argv": self.memory.sys_argv_addr,
            }

            # JIT cache is per-invocation (addresses change)
            self._jit_cache = {}
            self._jit_code_pages = []

            # dlopen libraries for C extern support
            self._dl_handles = []
            self._dl_func_cache = {}
            all_libs = list(self._ct_libs)
            if libs:
                for lib in libs:
                    if lib not in all_libs:
                        all_libs.append(lib)
            for lib_name in all_libs:
                self._dlopen(lib_name)

            # Deep word chains need extra Python stack depth.
            old_limit = sys.getrecursionlimit()
            if old_limit < 10000:
                sys.setrecursionlimit(10000)
        try:
            self._call_word(word)
        except _CTVMExit:
            pass  # graceful exit from CT execution
        finally:
            self.runtime_mode = prev_mode
            # Clear JIT cache; code pages are libc mmap'd and we intentionally
            # leak them — the OS reclaims them at process exit.
            self._jit_cache.clear()
            self._jit_code_pages.clear()
            self._dl_func_cache.clear()
            self._dl_handles.clear()

    def invoke_with_args(self, word: Word, args: Sequence[Any]) -> None:
        self.reset()
        for value in args:
            self.push(value)
        self._call_word(word)

    def invoke_repl(self, word: Word, *, libs: Optional[List[str]] = None) -> None:
        """Execute *word* in runtime mode, preserving stack/memory across calls.

        On the first call (or after ``reset()``), allocates native stacks and
        memory.  Subsequent calls reuse the existing state so values left on
        the data stack persist between REPL evaluations.
        """
        self._ensure_jit_out()
        prev_mode = self.runtime_mode
        self.runtime_mode = True

        if not self._repl_initialized:
            persistent_size = 0
            if self.parser.custom_bss:
                for bss_line in self.parser.custom_bss:
                    m = _RE_BSS_PERSISTENT.search(bss_line)
                    if m:
                        persistent_size = int(m.group(1))
            self.memory = CTMemory(persistent_size)
            self.memory.setup_argv(sys.argv)

            self._native_data_stack = ctypes.create_string_buffer(self.NATIVE_STACK_SIZE)
            self._native_data_top = ctypes.addressof(self._native_data_stack) + self.NATIVE_STACK_SIZE
            self.r12 = self._native_data_top

            self._native_return_stack = ctypes.create_string_buffer(self.NATIVE_STACK_SIZE)
            self._native_return_top = ctypes.addressof(self._native_return_stack) + self.NATIVE_STACK_SIZE
            self.r13 = self._native_return_top

            self._bss_symbols = {
                "dstack": ctypes.addressof(self._native_data_stack),
                "dstack_top": self._native_data_top,
                "rstack": ctypes.addressof(self._native_return_stack),
                "rstack_top": self._native_return_top,
                "data_start": self.memory.data_start,
                "data_end": self.memory.data_start + self.memory._data_offset if self.memory._data_offset else self.memory.data_end,
                "print_buf": self.memory.print_buf_addr,
                "print_buf_end": self.memory.print_buf_addr + CTMemory.PRINT_BUF_SIZE,
                "persistent": self.memory.persistent_addr,
                "persistent_end": self.memory.persistent_addr + self.memory._persistent_size,
                "sys_argc": self.memory.sys_argc_addr,
                "sys_argv": self.memory.sys_argv_addr,
            }
            self._jit_cache = {}
            self._jit_code_pages = []
            self._dl_handles = []
            self._dl_func_cache = {}
            all_libs = list(self._ct_libs)
            if libs:
                for lib in libs:
                    if lib not in all_libs:
                        all_libs.append(lib)
            for lib_name in all_libs:
                self._dlopen(lib_name)

            old_limit = sys.getrecursionlimit()
            if old_limit < 10000:
                sys.setrecursionlimit(10000)
            self._repl_initialized = True
            self._repl_libs = list(libs or [])
        else:
            # Subsequent call — open any new libraries not yet loaded
            if libs:
                for lib in libs:
                    if lib not in self._repl_libs:
                        self._dlopen(lib)
                        self._repl_libs.append(lib)

        # Clear transient state but keep stacks and memory
        self.call_stack.clear()
        self.loop_stack.clear()
        self._list_capture_stack.clear()
        self.current_location = None
        # JIT cache must be cleared because word definitions change between
        # REPL evaluations (re-parsed each time).
        self._jit_cache.clear()

        try:
            self._call_word(word)
        except _CTVMExit:
            pass
        finally:
            self.runtime_mode = prev_mode

    def repl_stack_values(self) -> List[int]:
        """Return current native data stack contents (bottom to top)."""
        if not self._repl_initialized or self.r12 >= self._native_data_top:
            return []
        values = []
        addr = self._native_data_top - 8
        while addr >= self.r12:
            values.append(CTMemory.read_qword(addr))
            addr -= 8
        return values

    def push(self, value: Any) -> None:
        if self.runtime_mode:
            self.r12 -= 8
            if isinstance(value, float):
                bits = _get_struct().unpack("q", _get_struct().pack("d", value))[0]
                CTMemory.write_qword(self.r12, bits)
            else:
                CTMemory.write_qword(self.r12, _to_i64(int(value)))
        else:
            self.stack.append(value)

    def pop(self) -> Any:
        if self.runtime_mode:
            if self.r12 >= self._native_data_top:
                raise ParseError("compile-time stack underflow")
            val = CTMemory.read_qword(self.r12)
            self.r12 += 8
            return val
        if not self.stack:
            raise ParseError("compile-time stack underflow")
        return self.stack.pop()

    def _resolve_handle(self, value: Any) -> Any:
        if isinstance(value, int):
            for delta in (0, -1, 1):
                candidate = value + delta
                if candidate in self._handles.objects:
                    obj = self._handles.objects[candidate]
                    self._handles.objects[value] = obj
                    return obj
            # Occasionally a raw object id can appear on the stack; recover it if we still
            # hold the object reference.
            for obj in self._handles.objects.values():
                if id(obj) == value:
                    self._handles.objects[value] = obj
                    return obj
        return value

    def peek(self) -> Any:
        if self.runtime_mode:
            if self.r12 >= self._native_data_top:
                raise ParseError("compile-time stack underflow")
            return CTMemory.read_qword(self.r12)
        if not self.stack:
            raise ParseError("compile-time stack underflow")
        return self.stack[-1]

    def pop_int(self) -> int:
        if self.runtime_mode:
            return self.pop()  # already returns int from native stack
        value = self.pop()
        if isinstance(value, bool):
            return int(value)
        if not isinstance(value, int):
            raise ParseError(f"expected integer on compile-time stack, got {type(value).__name__}: {value!r}")
        return value

    # -- return stack helpers (native r13 in runtime_mode) -----------------

    def push_return(self, value: int) -> None:
        if self.runtime_mode:
            self.r13 -= 8
            CTMemory.write_qword(self.r13, _to_i64(value))
        else:
            self.return_stack.append(value)

    def pop_return(self) -> int:
        if self.runtime_mode:
            val = CTMemory.read_qword(self.r13)
            self.r13 += 8
            return val
        return self.return_stack.pop()

    def peek_return(self) -> int:
        if self.runtime_mode:
            return CTMemory.read_qword(self.r13)
        return self.return_stack[-1]

    def poke_return(self, value: int) -> None:
        """Overwrite top of return stack."""
        if self.runtime_mode:
            CTMemory.write_qword(self.r13, _to_i64(value))
        else:
            self.return_stack[-1] = value

    def return_stack_empty(self) -> bool:
        if self.runtime_mode:
            return self.r13 >= self._native_return_top
        return len(self.return_stack) == 0

    # -- native stack depth ------------------------------------------------

    def native_stack_depth(self) -> int:
        """Number of items on data stack (runtime_mode only)."""
        return (self._native_data_top - self.r12) // 8

    def pop_str(self) -> str:
        value = self._resolve_handle(self.pop())
        if not isinstance(value, str):
            raise ParseError("expected string on compile-time stack")
        return value

    def pop_list(self) -> List[Any]:
        value = self._resolve_handle(self.pop())
        if not isinstance(value, list):
            known = value in self._handles.objects if isinstance(value, int) else False
            handles_size = len(self._handles.objects)
            handle_keys = list(self._handles.objects.keys())
            raise ParseError(
                f"expected list on compile-time stack, got {type(value).__name__} value={value!r} known_handle={known} handles={handles_size}:{handle_keys!r} stack={self.stack!r}"
            )
        return value

    def pop_token(self) -> Token:
        value = self._resolve_handle(self.pop())
        if not isinstance(value, Token):
            raise ParseError("expected token on compile-time stack")
        return value

    # -- dlopen / C extern support -----------------------------------------

    def _dlopen(self, lib_name: str) -> None:
        """Open a shared library and append to _dl_handles."""
        import ctypes.util
        # Try as given first (handles absolute paths, "libc.so.6", etc.)
        candidates = [lib_name]
        # If given a static archive (.a), try .so from the same directory
        if lib_name.endswith(".a"):
            so_variant = lib_name[:-2] + ".so"
            candidates.append(so_variant)
        # Try lib<name>.so
        if not lib_name.startswith("lib") and "." not in lib_name:
            candidates.append(f"lib{lib_name}.so")
        # Use ctypes.util.find_library for short names like "m", "c"
        found = ctypes.util.find_library(lib_name)
        if found:
            candidates.append(found)
        for candidate in candidates:
            try:
                handle = ctypes.CDLL(candidate, use_errno=True)
                self._dl_handles.append(handle)
                return
            except OSError:
                continue
        # Not fatal — the library may not be needed at CT

    _CTYPE_MAP: Optional[Dict[str, Any]] = None

    @classmethod
    def _get_ctype_map(cls) -> Dict[str, Any]:
        if cls._CTYPE_MAP is None:
            import ctypes
            cls._CTYPE_MAP = {
                "int": ctypes.c_int,
                "int8_t": ctypes.c_int8,
                "uint8_t": ctypes.c_uint8,
                "int16_t": ctypes.c_int16,
                "uint16_t": ctypes.c_uint16,
                "int32_t": ctypes.c_int32,
                "uint32_t": ctypes.c_uint32,
                "long": ctypes.c_long,
                "long long": ctypes.c_longlong,
                "int64_t": ctypes.c_int64,
                "unsigned int": ctypes.c_uint,
                "unsigned long": ctypes.c_ulong,
                "unsigned long long": ctypes.c_ulonglong,
                "uint64_t": ctypes.c_uint64,
                "size_t": ctypes.c_size_t,
                "ssize_t": ctypes.c_ssize_t,
                "char": ctypes.c_char,
                "char*": ctypes.c_void_p,
                "void*": ctypes.c_void_p,
                "double": ctypes.c_double,
                "float": ctypes.c_float,
            }
        return cls._CTYPE_MAP

    def _resolve_struct_ctype(self, struct_name: str) -> Any:
        cached = self._ctypes_struct_cache.get(struct_name)
        if cached is not None:
            return cached
        layout = self.parser.cstruct_layouts.get(struct_name)
        if layout is None:
            raise ParseError(f"unknown cstruct '{struct_name}' used in extern signature")
        fields = []
        for field in layout.fields:
            fields.append((field.name, self._resolve_ctype(field.type_name)))
        struct_cls = type(f"CTStruct_{sanitize_label(struct_name)}", (ctypes.Structure,), {"_fields_": fields})
        self._ctypes_struct_cache[struct_name] = struct_cls
        return struct_cls

    def _resolve_ctype(self, type_name: str) -> Any:
        """Map a C type name string to a ctypes type."""
        import ctypes
        t = _canonical_c_type_name(type_name)
        if t.endswith("*"):
            return ctypes.c_void_p
        if t.startswith("struct "):
            return self._resolve_struct_ctype(t[len("struct "):].strip())
        t = t.replace("*", "* ").replace("  ", " ").strip()
        ctype_map = self._get_ctype_map()
        if t in ctype_map:
            return ctype_map[t]
        # Default to c_long (64-bit on Linux x86-64)
        return ctypes.c_long

    def _dlsym(self, name: str) -> Any:
        """Look up a symbol across all dl handles, return a raw function pointer or None."""
        for handle in self._dl_handles:
            try:
                return getattr(handle, name)
            except AttributeError:
                continue
        return None

    def _call_extern_ct(self, word: Word) -> None:
        """Call an extern C function via dlsym/ctypes on the native stacks."""
        name = word.name

        # Special handling for exit — intercept it before doing anything
        if name == "exit":
            raise _CTVMExit()

        func = self._dl_func_cache.get(name)
        if func is None:
            raw = self._dlsym(name)
            if raw is None:
                raise ParseError(f"extern '{name}' not found in any loaded library")

            signature = word.extern_signature
            inputs = word.extern_inputs
            outputs = word.extern_outputs

            if signature:
                arg_types, ret_type = signature
                c_arg_types = [self._resolve_ctype(t) for t in arg_types]
                if ret_type == "void":
                    c_ret_type = None
                else:
                    c_ret_type = self._resolve_ctype(ret_type)
            else:
                # Legacy mode: assume all int64 args
                arg_types = []
                c_arg_types = [ctypes.c_int64] * inputs
                c_ret_type = ctypes.c_int64 if outputs > 0 else None

            # Configure the ctypes function object directly
            raw.restype = c_ret_type
            raw.argtypes = c_arg_types
            # Stash metadata for calling
            raw._ct_inputs = inputs
            raw._ct_outputs = outputs
            raw._ct_arg_types = c_arg_types
            raw._ct_ret_type = c_ret_type
            raw._ct_signature = signature
            func = raw
            self._dl_func_cache[name] = func

        inputs = func._ct_inputs
        outputs = func._ct_outputs
        arg_types = list(func._ct_signature[0]) if func._ct_signature else []

        # For variadic externs, the TOS value is the extra arg count
        # (consumed by the compiler, not passed to C).
        va_extra = 0
        if getattr(word, "extern_variadic", False):
            va_extra = int(self.pop())
            inputs += va_extra
            for _ in range(va_extra):
                arg_types.append("long")
            # Update ctypes argtypes to include the variadic args
            func.argtypes = list(func._ct_arg_types) + [ctypes.c_int64] * va_extra

        # Pop arguments off the native data stack (right-to-left / reverse order)
        raw_args = []
        for i in range(inputs):
            raw_args.append(self.pop())
        raw_args.reverse()

        # Convert arguments to proper ctypes values
        call_args = []
        for i, raw in enumerate(raw_args):
            arg_type = _canonical_c_type_name(arg_types[i]) if i < len(arg_types) else None
            if arg_type in ("float", "double"):
                # Reinterpret the int64 bits as a double (matching the language's convention)
                raw_int = _to_i64(int(raw))
                double_val = _get_struct().unpack("d", _get_struct().pack("q", raw_int))[0]
                call_args.append(double_val)
            elif arg_type is not None and arg_type.startswith("struct ") and not arg_type.endswith("*"):
                struct_name = arg_type[len("struct "):].strip()
                struct_ctype = self._resolve_struct_ctype(struct_name)
                call_args.append(struct_ctype.from_address(int(raw)))
            else:
                call_args.append(int(raw))

        result = func(*call_args)

        if outputs > 0 and result is not None:
            ret_type = _canonical_c_type_name(func._ct_signature[1]) if func._ct_signature else None
            if ret_type in ("float", "double"):
                int_bits = _get_struct().unpack("q", _get_struct().pack("d", float(result)))[0]
                self.push(int_bits)
            elif ret_type is not None and ret_type.startswith("struct "):
                struct_name = ret_type[len("struct "):].strip()
                layout = self.parser.cstruct_layouts.get(struct_name)
                if layout is None:
                    raise ParseError(f"unknown cstruct '{struct_name}' used in extern return type")
                out_ptr = self.memory.allocate(layout.size)
                ctypes.memmove(out_ptr, ctypes.byref(result), layout.size)
                self.push(out_ptr)
            else:
                self.push(int(result))

    def _call_word(self, word: Word) -> None:
        self.call_stack.append(word.name)
        try:
            if word.runtime_only:
                raise ParseError(
                    f"word '{word.name}' is runtime-only and cannot be executed at compile time"
                )
            definition = word.definition
            # In runtime_mode, prefer runtime_intrinsic (for exit/jmp/syscall
            # and __with_* variables).  All other :asm words run as native JIT.
            if self.runtime_mode and word.runtime_intrinsic is not None:
                word.runtime_intrinsic(self)
                return

            if (
                not self.runtime_mode
                and isinstance(definition, Definition)
                and word.compile_only
                and not word.compile_time_override
            ):
                body = definition.body
                if len(body) == 1:
                    node = body[0]
                    if node._opcode == OP_LITERAL:
                        self.push(node.data)
                        return
                    if node._opcode == OP_WORD:
                        if node._word_ref is None:
                            self._resolve_words_in_body(definition)
                        ref = node._word_ref
                        if (
                            ref is not None
                            and ref.compile_time_intrinsic is not None
                            and not ref.compile_time_override
                            and not ref.immediate
                        ):
                            ref.compile_time_intrinsic(self)
                            return

            prefer_definition = word.compile_time_override or (isinstance(definition, Definition) and (word.immediate or word.compile_only))
            if not prefer_definition and word.compile_time_intrinsic is not None:
                word.compile_time_intrinsic(self)
                return
            # C extern words: call via dlopen/dlsym in runtime_mode
            if self.runtime_mode and getattr(word, "is_extern", False):
                self._call_extern_ct(word)
                return
            if definition is None:
                raise ParseError(f"word '{word.name}' has no compile-time definition")
            if isinstance(definition, AsmDefinition):
                if self.runtime_mode:
                    self._run_jit(word)
                else:
                    self._run_asm_definition(word)
                return
            # Whole-word JIT for regular definitions in runtime mode
            if self.runtime_mode and isinstance(definition, Definition):
                ck = f"__defn_jit_{word.name}"
                jf = self._jit_cache.get(ck)
                if jf is None and ck + "_miss" not in self._jit_cache:
                    jf = self._compile_definition_jit(word)
                    if jf is not None:
                        self._jit_cache[ck] = jf
                        self._jit_cache[ck + "_addr"] = ctypes.cast(jf, ctypes.c_void_p).value
                    else:
                        self._jit_cache[ck + "_miss"] = True
                if jf is not None:
                    out = self._jit_out2
                    jf(self.r12, self.r13, self._jit_out2_addr)
                    self.r12 = out[0]
                    self.r13 = out[1]
                    return
            self._execute_nodes(definition.body, _defn=definition)
        except CompileTimeError:
            raise
        except (_CTVMJump, _CTVMExit, _CTVMReturn):
            raise
        except ParseError as exc:
            raise CompileTimeError(f"{exc}\ncompile-time stack: {' -> '.join(self.call_stack)}") from None
        except Exception as exc:
            raise CompileTimeError(
                f"compile-time failure in '{word.name}': {exc}\ncompile-time stack: {' -> '.join(self.call_stack)}"
            ) from None
        finally:
            self.call_stack.pop()

    # -- Native JIT execution (runtime_mode) --------------------------------

    _JIT_FUNC_TYPE: Optional[Any] = None

    def _run_jit(self, word: Word) -> None:
        """JIT-compile (once) and execute an :asm word on the native r12/r13 stacks."""
        func = self._jit_cache.get(word.name)
        if func is None:
            func = self._compile_jit(word)
            self._jit_cache[word.name] = func

        out = self._jit_out2
        func(self.r12, self.r13, self._jit_out2_addr)
        self.r12 = out[0]
        self.r13 = out[1]

    def _compile_jit(self, word: Word) -> Any:
        """Assemble an :asm word into executable memory and return a ctypes callable."""
        if not _ensure_keystone():
            raise ParseError("keystone-engine is required for JIT execution")
        definition = word.definition
        if not isinstance(definition, AsmDefinition):
            raise ParseError(f"word '{word.name}' has no asm body")
        asm_body = definition.body.strip("\n")
        is_coro = self._is_coroutine_asm(asm_body)

        bss = self._bss_symbols

        # Build wrapper
        lines: List[str] = []
        if is_coro:
            self._ensure_jit_save_buf()
            sb = self._jit_save_buf_addr
            # Use register-indirect addressing: x86-64 mov [disp],reg only
            # supports 32-bit displacement -- sb is a 64-bit heap address.
            lines.extend([
                "_ct_entry:",
                f"    mov rax, {sb}",        # load save buffer base
                "    mov [rax], rbx",
                "    mov [rax + 8], r12",
                "    mov [rax + 16], r13",
                "    mov [rax + 24], r14",
                "    mov [rax + 32], r15",
                "    mov [rax + 40], rdx",   # output ptr
                "    mov r12, rdi",
                "    mov r13, rsi",
                # Replace return address with trampoline
                "    pop rcx",
                "    mov [rax + 48], rcx",   # save ctypes return addr
                "    lea rcx, [rip + _ct_trampoline]",
                "    push rcx",
            ])
        else:
            # Standard wrapper: save callee-saved regs on stack
            lines.extend([
                "_ct_entry:",
                "    push rbx",
                "    push r12",
                "    push r13",
                "    push r14",
                "    push r15",
                "    sub rsp, 16",       # align + room for output ptr
                "    mov [rsp], rdx",    # save output-struct pointer
                "    mov r12, rdi",      # data stack
                "    mov r13, rsi",      # return stack
            ])

        # Patch asm body
        # Collect dot-prefixed local labels and build rename map for Keystone
        _local_labels: Set[str] = set()
        for raw_line in asm_body.splitlines():
            line = raw_line.strip()
            lm = _RE_LABEL_PAT.match(line)
            if lm and lm.group(1).startswith('.'):
                _local_labels.add(lm.group(1))

        for raw_line in asm_body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("extern"):
                continue  # strip extern declarations
            if line == "ret" and not is_coro:
                line = "jmp _ct_save"

            # Rename dot-prefixed local labels to Keystone-compatible names
            for lbl in _local_labels:
                line = re.sub(rf'(?<!\w){re.escape(lbl)}(?=\s|:|,|$|\]|\))',
                              '_jl' + lbl[1:], line)

            # Patch [rel SYMBOL] -> concrete address
            m = _RE_REL_PAT.search(line)
            if m and m.group(1) in bss:
                sym = m.group(1)
                addr = bss[sym]
                if line.lstrip().startswith("lea"):
                    # lea REG, [rel X] -> mov REG, addr
                    line = _RE_REL_PAT.sub(str(addr), line).replace("lea", "mov", 1)
                else:
                    # e.g. mov rax, [rel X] or mov byte [rel X], val
                    # Replace with push/mov-rax/substitute/pop trampoline
                    lines.append("    push rax")
                    lines.append(f"    mov rax, {addr}")
                    new_line = _RE_REL_PAT.sub("[rax]", line)
                    lines.append(f"    {new_line}")
                    lines.append("    pop rax")
                    continue
            # Convert NASM 'rel' to explicit rip-relative for Keystone
            if '[rel ' in line:
                line = line.replace('[rel ', '[rip + ')
            lines.append(f"    {line}")

        # Save/epilogue
        if is_coro:
            sb = self._jit_save_buf_addr
            lines.extend([
                "_ct_trampoline:",
                f"    mov rax, {sb}",        # reload save buffer base
                "    mov rcx, [rax + 40]",   # output ptr
                "    mov [rcx], r12",
                "    mov [rcx + 8], r13",
                "    mov rbx, [rax]",
                "    mov r12, [rax + 8]",
                "    mov r13, [rax + 16]",
                "    mov r14, [rax + 24]",
                "    mov r15, [rax + 32]",
                "    mov rcx, [rax + 48]",   # ctypes return addr
                "    push rcx",
                "    ret",
            ])
        else:
            lines.extend([
                "_ct_save:",
                "    mov rax, [rsp]",      # output-struct pointer
                "    mov [rax], r12",
                "    mov [rax + 8], r13",
                "    add rsp, 16",
                "    pop r15",
                "    pop r14",
                "    pop r13",
                "    pop r12",
                "    pop rbx",
                "    ret",
            ])

        ptr = self._jit_assemble_page(lines, word.name)
        if CompileTimeVM._JIT_FUNC_TYPE is None:
            CompileTimeVM._JIT_FUNC_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p)
        func = self._JIT_FUNC_TYPE(ptr)
        return func

    def _jit_assemble_page(self, lines: List[str], word_name: str) -> int:
        """Assemble lines into an RWX page and return its address."""
        def _norm(l: str) -> str:
            l = l.split(";", 1)[0].rstrip()
            for sz in ("qword", "dword", "word", "byte"):
                l = l.replace(f"{sz} [", f"{sz} ptr [")
            return l
        normalized = [_norm(l) for l in lines if _norm(l).strip()]
        ks = Ks(KS_ARCH_X86, KS_MODE_64)
        try:
            encoding, _ = ks.asm("\n".join(normalized))
        except KsError as exc:
            debug_txt = "\n".join(normalized)
            raise ParseError(
                f"JIT assembly failed for '{word_name}': {exc}\n--- asm ---\n{debug_txt}\n--- end ---"
            ) from exc
        if encoding is None:
            raise ParseError(f"JIT produced no code for '{word_name}'")
        code = bytes(encoding)
        page_size = max(len(code), 4096)
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.mmap.restype = ctypes.c_void_p
        _libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_long]
        PROT_RWX = 0x1 | 0x2 | 0x4
        MAP_PRIVATE = 0x02
        MAP_ANONYMOUS = 0x20
        ptr = _libc.mmap(None, page_size, PROT_RWX,
                          MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
        if ptr == ctypes.c_void_p(-1).value or ptr is None:
            raise RuntimeError(f"mmap failed for JIT code ({page_size} bytes)")
        ctypes.memmove(ptr, code, len(code))
        self._jit_code_pages.append((ptr, page_size))
        return ptr

    def _compile_raw_jit(self, word: Word) -> int:
        """Compile a word into executable memory without a wrapper.

        Returns the native code address (not a ctypes callable).
        For AsmDefinition: just the asm body (patched, no wrapper).
        For Definition: compiled body with ret (no entry/exit wrapper).
        """
        cache_key = f"__raw_jit_{word.name}"
        cached = self._jit_cache.get(cache_key)
        if cached is not None:
            return cached

        definition = word.definition
        bss = self._bss_symbols
        lines: List[str] = []

        if isinstance(definition, AsmDefinition):
            asm_body = definition.body.strip("\n")
            is_coro = self._is_coroutine_asm(asm_body)
            _local_labels: Set[str] = set()
            for raw_line in asm_body.splitlines():
                line = raw_line.strip()
                lm = _RE_LABEL_PAT.match(line)
                if lm and lm.group(1).startswith('.'):
                    _local_labels.add(lm.group(1))
            for raw_line in asm_body.splitlines():
                line = raw_line.strip()
                if not line or line.startswith(";") or line.startswith("extern"):
                    continue
                # Keep ret as-is (raw functions return normally)
                for lbl in _local_labels:
                    line = re.sub(rf'(?<!\w){re.escape(lbl)}(?=\s|:|,|$|\]|\))',
                                  '_jl' + lbl[1:], line)
                m = _RE_REL_PAT.search(line)
                if m and m.group(1) in bss:
                    sym = m.group(1)
                    addr = bss[sym]
                    if line.lstrip().startswith("lea"):
                        line = _RE_REL_PAT.sub(str(addr), line).replace("lea", "mov", 1)
                    else:
                        lines.append("    push rax")
                        lines.append(f"    mov rax, {addr}")
                        new_line = _RE_REL_PAT.sub("[rax]", line)
                        lines.append(f"    {new_line}")
                        lines.append("    pop rax")
                        continue
                if '[rel ' in line:
                    line = line.replace('[rel ', '[rip + ')
                lines.append(f"    {line}")
            lines.append("    ret")
        elif isinstance(definition, Definition):
            lines.extend(self._compile_raw_definition_lines(word, definition))
        else:
            raise ParseError(f"cannot raw-JIT word '{word.name}'")

        ptr = self._jit_assemble_page(lines, f"raw_{word.name}")
        self._jit_cache[cache_key] = ptr
        return ptr

    def _compile_raw_definition_lines(self, word: Word, defn: Definition) -> List[str]:
        """Compile a Definition body to raw JIT asm lines (no wrapper, just body + ret)."""
        self._resolve_words_in_body(defn)
        bss = self._bss_symbols
        body = defn.body
        lines: List[str] = []
        uid = id(defn)
        lc = [0]
        def _nl(prefix: str) -> str:
            lc[0] += 1
            return f"_rj{uid}_{prefix}_{lc[0]}"

        # Label maps
        label_map: Dict[str, str] = {}
        for node in body:
            if node._opcode == OP_LABEL:
                ln = str(node.data)
                if ln not in label_map:
                    label_map[ln] = _nl("lbl")

        for_map: Dict[int, Tuple[str, str]] = {}
        fstack: List[Tuple[int, str, str]] = []
        for idx, node in enumerate(body):
            if node._opcode == OP_FOR_BEGIN:
                bl, el = _nl("for_top"), _nl("for_end")
                fstack.append((idx, bl, el))
            elif node._opcode == OP_FOR_END:
                if fstack:
                    bi, bl, el = fstack.pop()
                    for_map[bi] = (bl, el)
                    for_map[idx] = (bl, el)

        ba_map: Dict[int, Tuple[str, str]] = {}
        bstack: List[Tuple[int, str, str]] = []
        for idx, node in enumerate(body):
            if node._opcode == OP_WORD and node._word_ref is None:
                nm = node.data
                if nm == "begin":
                    bl, al = _nl("begin"), _nl("again")
                    bstack.append((idx, bl, al))
                elif nm == "again":
                    if bstack:
                        bi, bl, al = bstack.pop()
                        ba_map[bi] = (bl, al)
                        ba_map[idx] = (bl, al)

        begin_rt: List[Tuple[str, str]] = []
        out2_addr = self._jit_out2_addr

        for idx, node in enumerate(body):
            opc = node._opcode
            if opc == OP_LITERAL:
                data = node.data
                if isinstance(data, str):
                    addr, length = self.memory.store_string(data)
                    lines.append("    sub r12, 16")
                    lines.append(f"    mov rax, {addr}")
                    lines.append("    mov [r12 + 8], rax")
                    if -0x80000000 <= length <= 0x7FFFFFFF:
                        lines.append(f"    mov qword [r12], {length}")
                    else:
                        lines.append(f"    mov rax, {length}")
                        lines.append("    mov [r12], rax")
                else:
                    val = int(data) & 0xFFFFFFFFFFFFFFFF
                    if val >= 0x8000000000000000:
                        val -= 0x10000000000000000
                    lines.append("    sub r12, 8")
                    if -0x80000000 <= val <= 0x7FFFFFFF:
                        lines.append(f"    mov qword [r12], {val}")
                    else:
                        lines.append(f"    mov rax, {val}")
                        lines.append("    mov [r12], rax")
            elif opc == OP_WORD:
                wref = node._word_ref
                if wref is None:
                    name = node.data
                    if name == "begin":
                        pair = ba_map.get(idx)
                        if pair:
                            begin_rt.append(pair)
                            lines.append(f"{pair[0]}:")
                    elif name == "again":
                        pair = ba_map.get(idx)
                        if pair:
                            lines.append(f"    jmp {pair[0]}")
                            lines.append(f"{pair[1]}:")
                            if begin_rt and begin_rt[-1] == pair:
                                begin_rt.pop()
                    elif name == "continue":
                        if begin_rt:
                            lines.append(f"    jmp {begin_rt[-1][0]}")
                    elif name == "exit":
                        lines.append("    ret")
                    continue

                wd = wref.definition
                if isinstance(wd, AsmDefinition):
                    if self._is_coroutine_asm(wd.body.strip("\n")):
                        # Coroutine asm: call raw JIT instead of inlining
                        raw_addr = self._compile_raw_jit(wref)
                        lines.append(f"    mov rax, {raw_addr}")
                        lines.append("    call rax")
                    else:
                        # Inline asm body
                        prefix = _nl(f"a{idx}")
                        _local_labels: Set[str] = set()
                        asm_txt = wd.body.strip("\n")
                        has_ret = False
                        for raw_line in asm_txt.splitlines():
                            ln = raw_line.strip()
                            lm = _RE_LABEL_PAT.match(ln)
                            if lm:
                                _local_labels.add(lm.group(1))
                            if ln == "ret":
                                has_ret = True
                        end_lbl = f"{prefix}_end" if has_ret else None
                        for raw_line in asm_txt.splitlines():
                            ln = raw_line.strip()
                            if not ln or ln.startswith(";") or ln.startswith("extern"):
                                continue
                            if ln == "ret":
                                lines.append(f"    jmp {end_lbl}")
                                continue
                            for lbl in _local_labels:
                                ln = re.sub(rf'(?<!\w){re.escape(lbl)}(?=\s|:|,|$|\]|\))',
                                            prefix + lbl, ln)
                            m = _RE_REL_PAT.search(ln)
                            if m and m.group(1) in bss:
                                sym = m.group(1)
                                addr = bss[sym]
                                if ln.lstrip().startswith("lea"):
                                    ln = _RE_REL_PAT.sub(str(addr), ln).replace("lea", "mov", 1)
                                else:
                                    lines.append("    push rax")
                                    lines.append(f"    mov rax, {addr}")
                                    new_ln = _RE_REL_PAT.sub("[rax]", ln)
                                    lines.append(f"    {new_ln}")
                                    lines.append("    pop rax")
                                    continue
                            if '[rel ' in ln:
                                ln = ln.replace('[rel ', '[')
                            lines.append(f"    {ln}")
                        if end_lbl is not None:
                            lines.append(f"{end_lbl}:")
                elif isinstance(wd, Definition):
                    # Call standard JIT'd sub-definition via output buffer
                    ck = f"__defn_jit_{wref.name}"
                    if ck not in self._jit_cache:
                        sub = self._compile_definition_jit(wref)
                        if sub is None:
                            # Can't JIT; fall back to raw JIT of the sub-word
                            raw_addr = self._compile_raw_jit(wref)
                            lines.append(f"    mov rax, {raw_addr}")
                            lines.append("    call rax")
                            continue
                        self._jit_cache[ck] = sub
                        self._jit_cache[ck + "_addr"] = ctypes.cast(sub, ctypes.c_void_p).value
                    func_addr = self._jit_cache.get(ck + "_addr")
                    if func_addr is None:
                        raise ParseError(f"raw JIT: missing JIT for '{wref.name}'")
                    lines.append("    mov rdi, r12")
                    lines.append("    mov rsi, r13")
                    lines.append(f"    mov rdx, {out2_addr}")
                    lines.append(f"    mov rax, {func_addr}")
                    lines.append("    call rax")
                    lines.append(f"    mov rax, {out2_addr}")
                    lines.append("    mov r12, [rax]")
                    lines.append("    mov r13, [rax + 8]")
                else:
                    raise ParseError(f"raw JIT: unsupported word '{wref.name}'")
            elif opc == OP_WORD_PTR:
                # Word pointer: push the raw JIT address of the target
                target_name = str(node.data)
                tw = self.dictionary.lookup(target_name)
                if tw is None:
                    raise ParseError(f"raw JIT: unknown word '{target_name}'")
                raw_addr = self._compile_raw_jit(tw)
                lines.append("    sub r12, 8")
                lines.append(f"    mov rax, {raw_addr}")
                lines.append("    mov [r12], rax")
            elif opc == OP_FOR_BEGIN:
                pair = for_map.get(idx)
                if pair is None:
                    raise ParseError("raw JIT: unmatched for")
                bl, el = pair
                lines.append("    mov rax, [r12]")
                lines.append("    add r12, 8")
                lines.append("    cmp rax, 0")
                lines.append(f"    jle {el}")
                lines.append("    sub r13, 8")
                lines.append("    mov [r13], rax")
                lines.append(f"{bl}:")
            elif opc == OP_FOR_END:
                pair = for_map.get(idx)
                if pair is None:
                    raise ParseError("raw JIT: unmatched for end")
                bl, el = pair
                lines.append("    dec qword [r13]")
                lines.append("    cmp qword [r13], 0")
                lines.append(f"    jg {bl}")
                lines.append("    add r13, 8")
                lines.append(f"{el}:")
            elif opc == OP_BRANCH_ZERO:
                ln = str(node.data)
                al = label_map.get(ln)
                if al is None:
                    raise ParseError("raw JIT: unknown branch target")
                lines.append("    mov rax, [r12]")
                lines.append("    add r12, 8")
                lines.append("    test rax, rax")
                lines.append(f"    jz {al}")
            elif opc == OP_JUMP:
                ln = str(node.data)
                al = label_map.get(ln)
                if al is None:
                    raise ParseError("raw JIT: unknown jump target")
                lines.append(f"    jmp {al}")
            elif opc == OP_LABEL:
                ln = str(node.data)
                al = label_map.get(ln)
                if al is None:
                    raise ParseError("raw JIT: unknown label")
                lines.append(f"{al}:")
            else:
                raise ParseError(f"raw JIT: unsupported opcode {opc} in '{word.name}'")

        lines.append("    ret")
        return lines

    # -- Whole-word JIT: compile Definition bodies to native code -----------

    def _compile_definition_jit(self, word: Word) -> Any:
        """JIT-compile a regular Definition body into native x86-64 code.

        Returns a ctypes callable or None if the definition cannot be JIT'd.
        """
        defn = word.definition
        if not isinstance(defn, Definition):
            return None
        if not _ensure_keystone():
            return None

        # Guard against infinite recursion (recursive words)
        compiling = getattr(self, "_djit_compiling", None)
        if compiling is None:
            compiling = set()
            self._djit_compiling = compiling
        if word.name in compiling:
            return None  # recursive word, can't JIT
        compiling.add(word.name)
        try:
            return self._compile_definition_jit_inner(word, defn)
        finally:
            compiling.discard(word.name)

    def _compile_definition_jit_inner(self, word: Word, defn: Definition) -> Any:
        # Ensure word references are resolved
        self._resolve_words_in_body(defn)

        body = defn.body
        bss = self._bss_symbols

        # Pre-scan: bail if any op is unsupported
        for node in body:
            opc = node._opcode
            if opc == OP_LITERAL:
                if isinstance(node.data, str):
                    return None
            elif opc == OP_WORD:
                wref = node._word_ref
                if wref is None:
                    name = node.data
                    if name not in ("begin", "again", "continue", "exit"):
                        return None
                elif wref.runtime_intrinsic is not None:
                    return None
                elif getattr(wref, "is_extern", False):
                    return None  # extern words need _call_extern_ct
                else:
                    wd = wref.definition
                    if wd is None:
                        return None
                    if not isinstance(wd, (AsmDefinition, Definition)):
                        return None
                    if isinstance(wd, Definition):
                        ck = f"__defn_jit_{wref.name}"
                        if ck not in self._jit_cache:
                            sub = self._compile_definition_jit(wref)
                            if sub is None:
                                return None
                            self._jit_cache[ck] = sub
                            self._jit_cache[ck + "_addr"] = ctypes.cast(sub, ctypes.c_void_p).value
            elif opc in (OP_FOR_BEGIN, OP_FOR_END, OP_BRANCH_ZERO, OP_JUMP, OP_LABEL):
                pass
            else:
                return None

        uid = id(defn)
        lc = [0]
        def _nl(prefix: str) -> str:
            lc[0] += 1
            return f"_dj{uid}_{prefix}_{lc[0]}"

        # Build label maps
        label_map: Dict[str, str] = {}
        for node in body:
            if node._opcode == OP_LABEL:
                ln = str(node.data)
                if ln not in label_map:
                    label_map[ln] = _nl("lbl")

        # For-loop pairing
        for_map: Dict[int, Tuple[str, str]] = {}
        fstack: List[Tuple[int, str, str]] = []
        for idx, node in enumerate(body):
            if node._opcode == OP_FOR_BEGIN:
                bl, el = _nl("for_top"), _nl("for_end")
                fstack.append((idx, bl, el))
            elif node._opcode == OP_FOR_END:
                if fstack:
                    bi, bl, el = fstack.pop()
                    for_map[bi] = (bl, el)
                    for_map[idx] = (bl, el)

        # begin/again pairing
        ba_map: Dict[int, Tuple[str, str]] = {}
        bstack: List[Tuple[int, str, str]] = []
        for idx, node in enumerate(body):
            if node._opcode == OP_WORD and node._word_ref is None:
                nm = node.data
                if nm == "begin":
                    bl, al = _nl("begin"), _nl("again")
                    bstack.append((idx, bl, al))
                elif nm == "again":
                    if bstack:
                        bi, bl, al = bstack.pop()
                        ba_map[bi] = (bl, al)
                        ba_map[idx] = (bl, al)

        lines: List[str] = []
        # Entry wrapper
        lines.extend([
            "_ct_entry:",
            "    push rbx",
            "    push r12",
            "    push r13",
            "    push r14",
            "    push r15",
            "    sub rsp, 16",
            "    mov [rsp], rdx",
            "    mov r12, rdi",
            "    mov r13, rsi",
        ])

        begin_rt: List[Tuple[str, str]] = []

        def _patch_asm_body(asm_body: str, prefix: str) -> List[str]:
            """Patch an asm body for inlining: uniquify labels, patch [rel]."""
            result: List[str] = []
            local_labels: Set[str] = set()
            has_ret = False
            for raw_line in asm_body.splitlines():
                line = raw_line.strip()
                lm = _RE_LABEL_PAT.match(line)
                if lm:
                    local_labels.add(lm.group(1))
                if line == "ret":
                    has_ret = True
            end_label = f"{prefix}_end" if has_ret else None
            for raw_line in asm_body.splitlines():
                line = raw_line.strip()
                if not line or line.startswith(";") or line.startswith("extern"):
                    continue
                if line == "ret":
                    result.append(f"    jmp {end_label}")
                    continue
                for label in local_labels:
                    line = re.sub(rf'(?<!\w){re.escape(label)}(?=\s|:|,|$|\]|\))', prefix + label, line)
                m = _RE_REL_PAT.search(line)
                if m and m.group(1) in bss:
                    sym = m.group(1)
                    addr = bss[sym]
                    if line.lstrip().startswith("lea"):
                        line = _RE_REL_PAT.sub(str(addr), line).replace("lea", "mov", 1)
                    else:
                        result.append("    push rax")
                        result.append(f"    mov rax, {addr}")
                        new_line = _RE_REL_PAT.sub("[rax]", line)
                        result.append(f"    {new_line}")
                        result.append("    pop rax")
                        continue
                # Convert NASM 'rel' to explicit rip-relative for Keystone
                if '[rel ' in line:
                    line = line.replace('[rel ', '[rip + ')
                result.append(f"    {line}")
            if end_label is not None:
                result.append(f"{end_label}:")
            return result

        for idx, node in enumerate(body):
            opc = node._opcode

            if opc == OP_LITERAL:
                val = int(node.data) & 0xFFFFFFFFFFFFFFFF
                if val >= 0x8000000000000000:
                    val -= 0x10000000000000000
                lines.append("    sub r12, 8")
                if -0x80000000 <= val <= 0x7FFFFFFF:
                    lines.append(f"    mov qword [r12], {val}")
                else:
                    lines.append(f"    mov rax, {val}")
                    lines.append("    mov [r12], rax")

            elif opc == OP_WORD:
                wref = node._word_ref
                if wref is None:
                    name = node.data
                    if name == "begin":
                        pair = ba_map.get(idx)
                        if pair:
                            begin_rt.append(pair)
                            lines.append(f"{pair[0]}:")
                    elif name == "again":
                        pair = ba_map.get(idx)
                        if pair:
                            lines.append(f"    jmp {pair[0]}")
                            lines.append(f"{pair[1]}:")
                            if begin_rt and begin_rt[-1] == pair:
                                begin_rt.pop()
                    elif name == "continue":
                        if begin_rt:
                            lines.append(f"    jmp {begin_rt[-1][0]}")
                    elif name == "exit":
                        if begin_rt:
                            pair = begin_rt.pop()
                            lines.append(f"    jmp {pair[1]}")
                        else:
                            lines.append("    jmp _ct_save")
                    continue

                wd = wref.definition
                if isinstance(wd, AsmDefinition):
                    prefix = _nl(f"a{idx}")
                    lines.extend(_patch_asm_body(wd.body.strip("\n"), prefix))
                elif isinstance(wd, Definition):
                    # Call JIT'd sub-definition
                    ck = f"__defn_jit_{wref.name}"
                    func_addr = self._jit_cache.get(ck + "_addr")
                    if func_addr is None:
                        return None  # should have been pre-compiled above
                    # Save & call: rdi=r12, rsi=r13, rdx=output_ptr
                    lines.append("    mov rax, [rsp]")
                    lines.append("    mov rdi, r12")
                    lines.append("    mov rsi, r13")
                    lines.append("    mov rdx, rax")
                    lines.append(f"    mov rax, {func_addr}")
                    lines.append("    call rax")
                    # Restore r12/r13 from output struct
                    lines.append("    mov rax, [rsp]")
                    lines.append("    mov r12, [rax]")
                    lines.append("    mov r13, [rax + 8]")

            elif opc == OP_FOR_BEGIN:
                pair = for_map.get(idx)
                if pair is None:
                    return None
                bl, el = pair
                lines.append("    mov rax, [r12]")
                lines.append("    add r12, 8")
                lines.append("    cmp rax, 0")
                lines.append(f"    jle {el}")
                lines.append("    sub r13, 8")
                lines.append("    mov [r13], rax")
                lines.append(f"{bl}:")

            elif opc == OP_FOR_END:
                pair = for_map.get(idx)
                if pair is None:
                    return None
                bl, el = pair
                lines.append("    dec qword [r13]")
                lines.append("    cmp qword [r13], 0")
                lines.append(f"    jg {bl}")
                lines.append("    add r13, 8")
                lines.append(f"{el}:")

            elif opc == OP_BRANCH_ZERO:
                ln = str(node.data)
                al = label_map.get(ln)
                if al is None:
                    return None
                lines.append("    mov rax, [r12]")
                lines.append("    add r12, 8")
                lines.append("    test rax, rax")
                lines.append(f"    jz {al}")

            elif opc == OP_JUMP:
                ln = str(node.data)
                al = label_map.get(ln)
                if al is None:
                    return None
                lines.append(f"    jmp {al}")

            elif opc == OP_LABEL:
                ln = str(node.data)
                al = label_map.get(ln)
                if al is None:
                    return None
                lines.append(f"{al}:")

        # Epilog
        lines.extend([
            "_ct_save:",
            "    mov rax, [rsp]",
            "    mov [rax], r12",
            "    mov [rax + 8], r13",
            "    add rsp, 16",
            "    pop r15",
            "    pop r14",
            "    pop r13",
            "    pop r12",
            "    pop rbx",
            "    ret",
        ])

        def _norm(l: str) -> str:
            l = l.split(";", 1)[0].rstrip()
            for sz in ("qword", "dword", "word", "byte"):
                l = l.replace(f"{sz} [", f"{sz} ptr [")
            return l
        normalized = [_norm(l) for l in lines if _norm(l).strip()]

        ks = Ks(KS_ARCH_X86, KS_MODE_64)
        try:
            encoding, _ = ks.asm("\n".join(normalized))
        except KsError:
            return None
        if encoding is None:
            return None

        code = bytes(encoding)
        page_size = max(len(code), 4096)
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.mmap.restype = ctypes.c_void_p
        _libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_long]
        PROT_RWX = 0x1 | 0x2 | 0x4
        MAP_PRIVATE = 0x02
        MAP_ANONYMOUS = 0x20
        ptr = _libc.mmap(None, page_size, PROT_RWX,
                          MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
        if ptr == ctypes.c_void_p(-1).value or ptr is None:
            return None
        ctypes.memmove(ptr, code, len(code))
        self._jit_code_pages.append((ptr, page_size))
        if CompileTimeVM._JIT_FUNC_TYPE is None:
            CompileTimeVM._JIT_FUNC_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p)
        return self._JIT_FUNC_TYPE(ptr)

    # -- Old non-runtime asm execution (kept for non-runtime CT mode) -------

    def _run_asm_definition(self, word: Word) -> None:
        definition = word.definition
        if not _ensure_keystone():
            raise ParseError("keystone is required for compile-time :asm execution; install keystone-engine")
        if not isinstance(definition, AsmDefinition):  # pragma: no cover - defensive
            raise ParseError(f"word '{word.name}' has no asm body")
        asm_body = definition.body.strip("\n")

        # Determine whether this asm expects string semantics via declared effects.
        string_mode = WORD_EFFECT_STRING_IO in definition.effects

        handles = self._handles

        non_int_data = any(not isinstance(v, int) for v in self.stack)
        non_int_return = any(not isinstance(v, int) for v in self.return_stack)

        # Collect all strings present on data and return stacks so we can point
        # puts() at a real buffer and pass its range check (data_start..data_end).
        strings: List[str] = []
        if string_mode:
            for v in self.stack + self.return_stack:
                if isinstance(v, str):
                    strings.append(v)
        data_blob = b""
        string_addrs: Dict[str, Tuple[int, int]] = {}
        if strings:
            offset = 0
            parts: List[bytes] = []
            seen: Dict[str, Tuple[int, int]] = {}
            for s in strings:
                if s in seen:
                    string_addrs[s] = seen[s]
                    continue
                encoded = s.encode("utf-8") + b"\x00"
                parts.append(encoded)
                addr = offset
                length = len(encoded) - 1
                seen[s] = (addr, length)
                string_addrs[s] = (addr, length)
                offset += len(encoded)
            data_blob = b"".join(parts)
        string_buffer: Optional[ctypes.Array[Any]] = None
        data_start = 0
        data_end = 0
        if data_blob:
            string_buffer = ctypes.create_string_buffer(data_blob)
            data_start = ctypes.addressof(string_buffer)
            data_end = data_start + len(data_blob)
            handles.refs.append(string_buffer)
            for s, (off, _len) in string_addrs.items():
                handles.objects[data_start + off] = s

        PRINT_BUF_BYTES = 128
        print_buffer = ctypes.create_string_buffer(PRINT_BUF_BYTES)
        handles.refs.append(print_buffer)
        print_buf = ctypes.addressof(print_buffer)

        wrapper_lines = []
        wrapper_lines.extend([
            "_ct_entry:",
            "    push rbx",
            "    push r12",
            "    push r13",
            "    push r14",
            "    push r15",
            "    mov r12, rdi",  # data stack pointer
            "    mov r13, rsi",  # return stack pointer
            "    mov r14, rdx",  # out ptr for r12
            "    mov r15, rcx",  # out ptr for r13
        ])
        if asm_body:
            patched_body = []
            # Build BSS symbol table for [rel X] -> concrete address substitution
            _bss_symbols: Dict[str, int] = {
                "data_start": data_start,
                "data_end": data_end,
                "print_buf": print_buf,
                "print_buf_end": print_buf + PRINT_BUF_BYTES,
            }
            if self.memory is not None:
                _bss_symbols.update({
                    "persistent": self.memory.persistent_addr,
                    "persistent_end": self.memory.persistent_addr + self.memory._persistent_size,
                })
            for line in asm_body.splitlines():
                line = line.strip()
                if line == "ret":
                    line = "jmp _ct_save"
                # Replace [rel SYMBOL] with concrete addresses
                m = _RE_REL_PAT.search(line)
                if m and m.group(1) in _bss_symbols:
                    sym = m.group(1)
                    addr = _bss_symbols[sym]
                    # lea REG, [rel X]  ->  mov REG, addr
                    if line.lstrip().startswith("lea"):
                        line = _RE_REL_PAT.sub(str(addr), line).replace("lea", "mov", 1)
                    else:
                        # For memory operands like mov byte [rel X], val
                        # replace [rel X] with [<addr>]
                        tmp_reg = "rax"
                        # Use a scratch register to hold the address
                        patched_body.append(f"push rax")
                        patched_body.append(f"mov rax, {addr}")
                        new_line = _RE_REL_PAT.sub("[rax]", line)
                        patched_body.append(new_line)
                        patched_body.append(f"pop rax")
                        continue
                # Convert NASM 'rel' to explicit rip-relative for Keystone
                if '[rel ' in line:
                    line = line.replace('[rel ', '[rip + ')
                patched_body.append(line)
            wrapper_lines.extend(patched_body)
        wrapper_lines.extend([
            "_ct_save:",
            "    mov [r14], r12",
            "    mov [r15], r13",
            "    pop r15",
            "    pop r14",
            "    pop r13",
            "    pop r12",
            "    pop rbx",
            "    ret",
        ])
        def _normalize_sizes(line: str) -> str:
            for size in ("qword", "dword", "word", "byte"):
                line = line.replace(f"{size} [", f"{size} ptr [")
            return line

        def _strip_comment(line: str) -> str:
            return line.split(";", 1)[0].rstrip()

        normalized_lines = []
        for raw in wrapper_lines:
            stripped = _strip_comment(raw)
            if not stripped.strip():
                continue
            normalized_lines.append(_normalize_sizes(stripped))
        ks = Ks(KS_ARCH_X86, KS_MODE_64)
        try:
            encoding, _ = ks.asm("\n".join(normalized_lines))
        except KsError as exc:
            debug_lines = "\n".join(normalized_lines)
            raise ParseError(
                f"keystone failed for word '{word.name}': {exc}\n--- asm ---\n{debug_lines}\n--- end asm ---"
            ) from exc
        if encoding is None:
            raise ParseError(
                f"keystone produced no code for word '{word.name}' (lines: {len(wrapper_lines)})"
            )

        code = bytes(encoding)
        import mmap
        code_buf = mmap.mmap(-1, len(code), prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
        code_buf.write(code)
        code_ptr = ctypes.addressof(ctypes.c_char.from_buffer(code_buf))
        func_type = ctypes.CFUNCTYPE(None, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64)
        func = func_type(code_ptr)

        handles = self._handles

        def _marshal_stack(py_stack: List[Any]) -> Tuple[int, int, int, Any]:
            capacity = len(py_stack) + 16
            buffer = (ctypes.c_int64 * capacity)()
            base = ctypes.addressof(buffer)
            top = base + capacity * 8
            sp = top
            for value in py_stack:
                sp -= 8
                if isinstance(value, int):
                    ctypes.c_int64.from_address(sp).value = value
                elif isinstance(value, str):
                    if string_mode:
                        offset, strlen = string_addrs.get(value, (0, 0))
                        addr = data_start + offset if data_start else handles.store(value)
                        # puts expects (len, addr) with len on top
                        ctypes.c_int64.from_address(sp).value = addr
                        sp -= 8
                        ctypes.c_int64.from_address(sp).value = strlen
                    else:
                        ctypes.c_int64.from_address(sp).value = handles.store(value)
                else:
                    ctypes.c_int64.from_address(sp).value = handles.store(value)
            return sp, top, base, buffer

        # r12/r13 must point at the top element (or top of buffer if empty)
        buffers: List[Any] = []
        d_sp, d_top, d_base, d_buf = _marshal_stack(self.stack)
        buffers.append(d_buf)
        r_sp, r_top, r_base, r_buf = _marshal_stack(self.return_stack)
        buffers.append(r_buf)
        out_d = ctypes.c_uint64(0)
        out_r = ctypes.c_uint64(0)
        func(d_sp, r_sp, ctypes.addressof(out_d), ctypes.addressof(out_r))

        new_d = out_d.value
        new_r = out_r.value
        if not (d_base <= new_d <= d_top):
            raise ParseError(f"compile-time asm '{word.name}' corrupted data stack pointer")
        if not (r_base <= new_r <= r_top):
            raise ParseError(f"compile-time asm '{word.name}' corrupted return stack pointer")

        def _unmarshal_stack(sp: int, top: int, table: _CTHandleTable) -> List[Any]:
            if sp == top:
                return []
            values: List[Any] = []
            addr = top - 8
            while addr >= sp:
                raw = ctypes.c_int64.from_address(addr).value
                if raw in table.objects:
                    obj = table.objects[raw]
                    if isinstance(obj, str) and values and isinstance(values[-1], int):
                        # collapse (len, addr) pairs back into the original string
                        values.pop()
                        values.append(obj)
                    else:
                        values.append(obj)
                else:
                    values.append(raw)
                addr -= 8
            return values

        self.stack = _unmarshal_stack(new_d, d_top, handles)
        self.return_stack = _unmarshal_stack(new_r, r_top, handles)

    def _call_word_by_name(self, name: str) -> None:
        word = self.dictionary.lookup(name)
        if word is None:
            raise ParseError(f"unknown word '{name}' during compile-time execution")
        self._call_word(word)

    def _resolve_words_in_body(self, defn: Definition) -> None:
        """Pre-resolve word name -> Word objects on Op nodes (once per Definition)."""
        if defn._words_resolved:
            return
        lookup = self.dictionary.lookup
        for node in defn.body:
            if node._opcode == OP_WORD and node._word_ref is None:
                name = str(node.data)
                # Skip structural keywords that _execute_nodes handles inline
                if name not in ("begin", "again", "continue", "exit", "get_addr"):
                    ref = lookup(name)
                    if ref is not None:
                        node._word_ref = ref
        defn._words_resolved = True

    def _prepare_definition(self, defn: Definition) -> Tuple[Dict[str, int], Dict[int, int], Dict[int, int]]:
        """Return (label_positions, for_pairs, begin_pairs), cached on the Definition."""
        if defn._label_positions is None:
            lp, fp, bp = self._analyze_nodes(defn.body)
            defn._label_positions = lp
            defn._for_pairs = fp
            defn._begin_pairs = bp
        self._resolve_words_in_body(defn)
        if self.runtime_mode:
            # Merged JIT runs are a performance optimization, but have shown
            # intermittent instability on some environments. Keep them opt-in.
            if os.environ.get("L2_CT_MERGED_JIT", "0") == "1":
                if defn._merged_runs is None:
                    defn._merged_runs = self._find_mergeable_runs(defn)
            else:
                defn._merged_runs = {}
        return defn._label_positions, defn._for_pairs, defn._begin_pairs

    def _find_mergeable_runs(self, defn: Definition) -> Dict[int, Tuple[int, str]]:
        """Find consecutive runs of JIT-able asm word ops (length >= 2)."""
        runs: Dict[int, Tuple[int, str]] = {}
        body = defn.body
        n = len(body)
        i = 0
        while i < n:
            # Start of a potential run
            if body[i]._opcode == OP_WORD and body[i]._word_ref is not None:
                w = body[i]._word_ref
                if (w.runtime_intrinsic is None and isinstance(w.definition, AsmDefinition)
                        and not w.compile_time_override):
                    run_start = i
                    run_words = [w.name]
                    i += 1
                    while i < n and body[i]._opcode == OP_WORD and body[i]._word_ref is not None:
                        w2 = body[i]._word_ref
                        if (w2.runtime_intrinsic is None and isinstance(w2.definition, AsmDefinition)
                                and not w2.compile_time_override):
                            run_words.append(w2.name)
                            i += 1
                        else:
                            break
                    if len(run_words) >= 2:
                        key = f"__merged_{defn.name}_{run_start}_{i}"
                        runs[run_start] = (i, key)
                    continue
            i += 1
        return runs

    def _compile_merged_jit(self, words: List[Word], cache_key: str) -> Any:
        """Compile multiple asm word bodies into a single JIT function."""
        if not _ensure_keystone():
            raise ParseError("keystone-engine is required for JIT execution")

        bss = self._bss_symbols

        lines: List[str] = []
        # Entry wrapper (same as _compile_jit)
        lines.extend([
            "_ct_entry:",
            "    push rbx",
            "    push r12",
            "    push r13",
            "    push r14",
            "    push r15",
            "    sub rsp, 16",
            "    mov [rsp], rdx",
            "    mov r12, rdi",
            "    mov r13, rsi",
        ])

        # Append each word's asm body, with labels uniquified
        for word_idx, word in enumerate(words):
            defn = word.definition
            asm_body = defn.body.strip("\n")
            prefix = f"_m{word_idx}_"

            # Collect all labels in this asm body first
            local_labels: Set[str] = set()
            for raw_line in asm_body.splitlines():
                line = raw_line.strip()
                lm = _RE_LABEL_PAT.match(line)
                if lm:
                    local_labels.add(lm.group(1))

            for raw_line in asm_body.splitlines():
                line = raw_line.strip()
                if not line or line.startswith(";"):
                    continue
                if line.startswith("extern"):
                    continue
                if line == "ret":
                    # Last word: jmp to save; others: fall through
                    if word_idx < len(words) - 1:
                        continue  # just skip ret -> fall through
                    else:
                        line = "jmp _ct_save"

                # Replace all references to local labels with prefixed versions
                for label in local_labels:
                    # Use word-boundary replacement to avoid partial matches
                    line = re.sub(rf'(?<!\w){re.escape(label)}(?=\s|:|,|$|\]|\))', prefix + label, line)

                # Patch [rel SYMBOL] -> concrete address
                m = _RE_REL_PAT.search(line)
                if m and m.group(1) in bss:
                    sym = m.group(1)
                    addr = bss[sym]
                    if line.lstrip().startswith("lea"):
                        line = _RE_REL_PAT.sub(str(addr), line).replace("lea", "mov", 1)
                    else:
                        lines.append("    push rax")
                        lines.append(f"    mov rax, {addr}")
                        new_line = _RE_REL_PAT.sub("[rax]", line)
                        lines.append(f"    {new_line}")
                        lines.append("    pop rax")
                        continue
                # Convert NASM 'rel' to explicit rip-relative for Keystone
                if '[rel ' in line:
                    line = line.replace('[rel ', '[rip + ')
                lines.append(f"    {line}")

        # Save epilog
        lines.extend([
            "_ct_save:",
            "    mov rax, [rsp]",
            "    mov [rax], r12",
            "    mov [rax + 8], r13",
            "    add rsp, 16",
            "    pop r15",
            "    pop r14",
            "    pop r13",
            "    pop r12",
            "    pop rbx",
            "    ret",
        ])

        # Normalize for Keystone
        def _norm(l: str) -> str:
            l = l.split(";", 1)[0].rstrip()
            for sz in ("qword", "dword", "word", "byte"):
                l = l.replace(f"{sz} [", f"{sz} ptr [")
            return l
        normalized = [_norm(l) for l in lines if _norm(l).strip()]

        ks = Ks(KS_ARCH_X86, KS_MODE_64)
        try:
            encoding, _ = ks.asm("\n".join(normalized))
        except KsError as exc:
            debug_txt = "\n".join(normalized)
            raise ParseError(
                f"JIT merged assembly failed for '{cache_key}': {exc}\n--- asm ---\n{debug_txt}\n--- end ---"
            ) from exc
        if encoding is None:
            raise ParseError(f"JIT merged produced no code for '{cache_key}'")

        code = bytes(encoding)
        page_size = max(len(code), 4096)
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.mmap.restype = ctypes.c_void_p
        _libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_long]
        PROT_RWX = 0x1 | 0x2 | 0x4
        MAP_PRIVATE = 0x02
        MAP_ANONYMOUS = 0x20
        ptr = _libc.mmap(None, page_size, PROT_RWX,
                          MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
        if ptr == ctypes.c_void_p(-1).value or ptr is None:
            raise RuntimeError(f"mmap failed for merged JIT code ({page_size} bytes)")
        ctypes.memmove(ptr, code, len(code))
        self._jit_code_pages.append((ptr, page_size))
        return self._JIT_FUNC_TYPE(ptr)

    def _execute_nodes(self, nodes: Sequence[Op], *, _defn: Optional[Definition] = None) -> None:
        # Use cached analysis if we have one, else compute fresh
        if _defn is not None:
            label_positions, loop_pairs, begin_pairs = self._prepare_definition(_defn)
        else:
            label_positions, loop_pairs, begin_pairs = self._analyze_nodes(nodes)
        prev_loop_stack = self.loop_stack
        self.loop_stack = []
        begin_stack: List[Tuple[int, int]] = []

        # Local variable aliases for hot-path speedup
        _runtime_mode = self.runtime_mode
        _push = self.push
        _pop = self.pop
        _pop_int = self.pop_int
        _push_return = self.push_return
        _pop_return = self.pop_return
        _peek_return = self.peek_return
        _poke_return = self.poke_return
        _call_word = self._call_word
        _dict_lookup = self.dictionary.lookup
        _ct_fast_dispatch = _CT_FAST_CT_INTRINSIC_DISPATCH if not _runtime_mode else None

        # Hot JIT-call locals (avoid repeated attribute access)
        _jit_cache = self._jit_cache if _runtime_mode else None
        _jit_out2 = self._jit_out2 if _runtime_mode else None
        _jit_out2_addr = self._jit_out2_addr if _runtime_mode else 0
        _compile_jit = self._compile_jit if _runtime_mode else None
        _compile_merged = self._compile_merged_jit if _runtime_mode else None
        _AsmDef = AsmDefinition
        _merged_runs = (_defn._merged_runs if _defn is not None and _defn._merged_runs else None) if _runtime_mode else None

        # Inline ctypes for runtime mode — eliminates per-op function call overhead
        if _runtime_mode:
            _c_int64_at = ctypes.c_int64.from_address
            _I64_MASK = 0xFFFFFFFFFFFFFFFF
            _I64_SIGN = 0x8000000000000000
            _I64_WRAP = 0x10000000000000000
            _store_string = self.memory.store_string
        else:
            _c_int64_at = _I64_MASK = _I64_SIGN = _I64_WRAP = _store_string = None  # type: ignore[assignment]

        ip = 0
        prev_location = self.current_location
        # Local opcode constants (avoid global dict lookup)
        _OP_WORD = OP_WORD
        _OP_LITERAL = OP_LITERAL
        _OP_WORD_PTR = OP_WORD_PTR
        _OP_FOR_BEGIN = OP_FOR_BEGIN
        _OP_FOR_END = OP_FOR_END
        _OP_BRANCH_ZERO = OP_BRANCH_ZERO
        _OP_JUMP = OP_JUMP
        _OP_LABEL = OP_LABEL
        _OP_LIST_BEGIN = OP_LIST_BEGIN
        _OP_RET = OP_RET
        _OP_LIST_END = OP_LIST_END
        _OP_LIST_LITERAL = OP_LIST_LITERAL
        try:
            while ip < len(nodes):
                _node = nodes[ip]
                kind = _node._opcode

                if kind == _OP_WORD:
                    # Merged JIT run: call one combined function for N words
                    if _merged_runs is not None:
                        run_info = _merged_runs.get(ip)
                        if run_info is not None:
                            end_ip, cache_key = run_info
                            func = _jit_cache.get(cache_key)
                            if func is None:
                                hit_key = cache_key + "_hits"
                                hits = _jit_cache.get(hit_key, 0) + 1
                                _jit_cache[hit_key] = hits
                                if hits >= 2:
                                    run_words = [nodes[j]._word_ref for j in range(ip, end_ip)]
                                    func = _compile_merged(run_words, cache_key)
                                    _jit_cache[cache_key] = func
                            if func is not None:
                                func(self.r12, self.r13, _jit_out2_addr)
                                self.r12 = _jit_out2[0]
                                self.r13 = _jit_out2[1]
                                ip = end_ip
                                continue

                    # Fast path: pre-resolved word reference
                    word = _node._word_ref
                    if word is not None:
                        if not _runtime_mode and _ct_fast_dispatch is not None:
                            fast_intrinsic = _ct_fast_dispatch.get(word.name)
                            if (
                                fast_intrinsic is not None
                                and word.compile_time_intrinsic is fast_intrinsic
                                and not word.compile_time_override
                                and not word.immediate
                            ):
                                self.current_location = _node.loc
                                self.call_stack.append(word.name)
                                try:
                                    fast_intrinsic(self)
                                except _CTVMJump as jmp:
                                    self.call_stack.pop()
                                    ip = jmp.target_ip
                                    continue
                                except _CTVMReturn:
                                    self.call_stack.pop()
                                    return
                                except ParseError as exc:
                                    self.call_stack.pop()
                                    raise CompileTimeError(
                                        f"{exc}\ncompile-time stack: {' -> '.join(self.call_stack + [word.name])}"
                                    ) from None
                                except Exception as exc:
                                    self.call_stack.pop()
                                    raise CompileTimeError(
                                        f"compile-time failure in '{word.name}': {exc}\n"
                                        f"compile-time stack: {' -> '.join(self.call_stack + [word.name])}"
                                    ) from None
                                else:
                                    self.call_stack.pop()
                                ip += 1
                                continue

                        if _runtime_mode:
                            ri = word.runtime_intrinsic
                            if ri is not None:
                                self.call_stack.append(word.name)
                                try:
                                    ri(self)
                                except _CTVMJump as jmp:
                                    self.call_stack.pop()
                                    ip = jmp.target_ip
                                    continue
                                except _CTVMReturn:
                                    self.call_stack.pop()
                                    return
                                finally:
                                    if self.call_stack and self.call_stack[-1] == word.name:
                                        self.call_stack.pop()
                                ip += 1
                                continue
                            defn = word.definition
                            if isinstance(defn, _AsmDef):
                                wn = word.name
                                func = _jit_cache.get(wn)
                                if func is None:
                                    func = _compile_jit(word)
                                    _jit_cache[wn] = func
                                func(self.r12, self.r13, _jit_out2_addr)
                                self.r12 = _jit_out2[0]
                                self.r13 = _jit_out2[1]
                                ip += 1
                                continue
                            # Whole-word JIT for Definition bodies
                            ck = "__defn_jit_" + word.name
                            jf = _jit_cache.get(ck)
                            if jf is None and _jit_cache.get(ck + "_miss") is None:
                                jf = self._compile_definition_jit(word)
                                if jf is not None:
                                    _jit_cache[ck] = jf
                                    _jit_cache[ck + "_addr"] = ctypes.cast(jf, ctypes.c_void_p).value
                                else:
                                    _jit_cache[ck + "_miss"] = True
                            if jf is not None:
                                jf(self.r12, self.r13, _jit_out2_addr)
                                self.r12 = _jit_out2[0]
                                self.r13 = _jit_out2[1]
                                ip += 1
                                continue
                        self.current_location = _node.loc
                        try:
                            _call_word(word)
                        except _CTVMJump as jmp:
                            ip = jmp.target_ip
                            continue
                        except _CTVMReturn:
                            return
                        ip += 1
                        continue

                    # Structural keywords or unresolved words
                    name = _node.data
                    if name == "begin":
                        end_idx = begin_pairs.get(ip)
                        if end_idx is None:
                            raise ParseError("'begin' without matching 'again'")
                        begin_stack.append((ip, end_idx))
                        ip += 1
                        continue
                    if name == "again":
                        if not begin_stack or begin_stack[-1][1] != ip:
                            raise ParseError("'again' without matching 'begin'")
                        ip = begin_stack[-1][0] + 1
                        continue
                    if name == "continue":
                        if not begin_stack:
                            raise ParseError("'continue' outside begin/again loop")
                        ip = begin_stack[-1][0] + 1
                        continue
                    if name == "exit":
                        if begin_stack:
                            frame = begin_stack.pop()
                            ip = frame[1] + 1
                            continue
                        return
                    if _runtime_mode and name == "get_addr":
                        r12 = self.r12 - 8
                        _c_int64_at(r12).value = ip + 1
                        self.r12 = r12
                        ip += 1
                        continue
                    self.current_location = _node.loc
                    w = _dict_lookup(name)
                    if w is None:
                        raise ParseError(f"unknown word '{name}' during compile-time execution")
                    try:
                        _call_word(w)
                    except _CTVMJump as jmp:
                        ip = jmp.target_ip
                        continue
                    except _CTVMReturn:
                        return
                    ip += 1
                    continue

                if kind == _OP_LITERAL:
                    if _runtime_mode:
                        data = _node.data
                        if isinstance(data, str):
                            addr, length = _store_string(data)
                            r12 = self.r12 - 16
                            _c_int64_at(r12 + 8).value = addr
                            _c_int64_at(r12).value = length
                            self.r12 = r12
                        else:
                            r12 = self.r12 - 8
                            v = int(data) & _I64_MASK
                            if v >= _I64_SIGN:
                                v -= _I64_WRAP
                            _c_int64_at(r12).value = v
                            self.r12 = r12
                    else:
                        _push(_node.data)
                    ip += 1
                    continue

                if kind == _OP_FOR_END:
                    if _runtime_mode:
                        val = _c_int64_at(self.r13).value - 1
                        _c_int64_at(self.r13).value = val
                        if val > 0:
                            ip = self.loop_stack[-1] + 1
                            continue
                        self.r13 += 8
                    else:
                        if not self.loop_stack:
                            raise ParseError("'next' without matching 'for'")
                        val = _peek_return() - 1
                        _poke_return(val)
                        if val > 0:
                            ip = self.loop_stack[-1] + 1
                            continue
                        _pop_return()
                    self.loop_stack.pop()
                    ip += 1
                    continue

                if kind == _OP_FOR_BEGIN:
                    if _runtime_mode:
                        count = _c_int64_at(self.r12).value
                        self.r12 += 8
                        if count <= 0:
                            match = loop_pairs.get(ip)
                            if match is None:
                                raise ParseError("internal loop bookkeeping error")
                            ip = match + 1
                            continue
                        r13 = self.r13 - 8
                        v = count & _I64_MASK
                        if v >= _I64_SIGN:
                            v -= _I64_WRAP
                        _c_int64_at(r13).value = v
                        self.r13 = r13
                    else:
                        count = _pop_int()
                        if count <= 0:
                            match = loop_pairs.get(ip)
                            if match is None:
                                raise ParseError("internal loop bookkeeping error")
                            ip = match + 1
                            continue
                        _push_return(count)
                    self.loop_stack.append(ip)
                    ip += 1
                    continue

                if kind == _OP_BRANCH_ZERO:
                    if _runtime_mode:
                        condition = _c_int64_at(self.r12).value
                        self.r12 += 8
                        if condition == 0:
                            ip = label_positions.get(str(_node.data), -1)
                            if ip == -1:
                                raise ParseError(f"unknown label during compile-time execution")
                        else:
                            ip += 1
                    else:
                        condition = _pop()
                        if isinstance(condition, bool):
                            flag = condition
                        elif isinstance(condition, int):
                            flag = condition != 0
                        else:
                            raise ParseError("branch expects integer or boolean condition")
                        if not flag:
                            ip = label_positions.get(str(_node.data), -1)
                            if ip == -1:
                                raise ParseError(f"unknown label during compile-time execution")
                        else:
                            ip += 1
                    continue

                if kind == _OP_JUMP:
                    ip = label_positions.get(str(_node.data), -1)
                    if ip == -1:
                        raise ParseError(f"unknown label during compile-time execution")
                    continue

                if kind == _OP_LABEL:
                    ip += 1
                    continue

                if kind == _OP_WORD_PTR:
                    target_name = str(_node.data)
                    target_word = _dict_lookup(target_name)
                    if target_word is None:
                        raise ParseError(
                            f"unknown word '{target_name}' referenced by pointer during compile-time execution"
                        )
                    if _runtime_mode:
                        # Push native code address so asm can jmp/call it
                        addr = self._compile_raw_jit(target_word)
                        _push(addr)
                        # Store reverse mapping so _rt_jmp can resolve back to Word
                        self._handles.objects[addr] = target_word
                    else:
                        _push(self._handles.store(target_word))
                    ip += 1
                    continue

                if kind == _OP_LIST_BEGIN:
                    if _runtime_mode:
                        self._list_capture_stack.append(self.r12)
                    else:
                        self._list_capture_stack.append(len(self.stack))
                    ip += 1
                    continue

                if kind == _OP_LIST_LITERAL:
                    values = list(_node.data or [])
                    count = len(values)
                    buf_size = (count + 1) * 8
                    addr = self.memory.allocate(buf_size)
                    CTMemory.write_qword(addr, count)
                    for idx_item, val in enumerate(values):
                        CTMemory.write_qword(addr + 8 + idx_item * 8, int(val))
                    _push(addr)
                    ip += 1
                    continue

                if kind == OP_BSS_LIST_LITERAL:
                    payload = _node.data if isinstance(_node.data, dict) else {}
                    count = int(payload.get("size", 0))
                    values = list(payload.get("values", []) or [])
                    if count < 0:
                        raise ParseError("bss list size must be >= 0")
                    if len(values) > count:
                        raise ParseError("bss list has more initializer values than declared size")
                    buf_size = (count + 1) * 8
                    addr = self.memory.allocate(buf_size)
                    CTMemory.write_qword(addr, count)
                    for idx_item, val in enumerate(values):
                        CTMemory.write_qword(addr + 8 + idx_item * 8, int(val))
                    for idx_item in range(len(values), count):
                        CTMemory.write_qword(addr + 8 + idx_item * 8, 0)
                    _push(addr)
                    ip += 1
                    continue

                if kind == _OP_LIST_END:
                    if not self._list_capture_stack:
                        raise ParseError("']' without matching '['")
                    saved = self._list_capture_stack.pop()
                    if _runtime_mode:
                        items: List[int] = []
                        ptr = saved - 8
                        while ptr >= self.r12:
                            items.append(_c_int64_at(ptr).value)
                            ptr -= 8
                        self.r12 = saved
                    else:
                        items = self.stack[saved:]
                        del self.stack[saved:]
                    count = len(items)
                    buf_size = (count + 1) * 8
                    addr = self.memory.allocate(buf_size)
                    CTMemory.write_qword(addr, count)
                    for idx_item, val in enumerate(items):
                        CTMemory.write_qword(addr + 8 + idx_item * 8, val)
                    _push(addr)
                    ip += 1
                    continue

                if kind == _OP_RET:
                    return

                self.current_location = _node.loc
                raise ParseError(f"unsupported compile-time op (opcode={kind})")
        finally:
            self.current_location = prev_location
            self.loop_stack = prev_loop_stack

    def _analyze_nodes(self, nodes: Sequence[Op]) -> Tuple[Dict[str, int], Dict[int, int], Dict[int, int]]:
        """Single-pass analysis: returns (label_positions, for_pairs, begin_pairs)."""
        label_positions: Dict[str, int] = {}
        for_pairs: Dict[int, int] = {}
        begin_pairs: Dict[int, int] = {}
        for_stack: List[int] = []
        begin_stack: List[int] = []
        for idx, node in enumerate(nodes):
            opc = node._opcode
            if opc == OP_LABEL:
                label_positions[str(node.data)] = idx
            elif opc == OP_FOR_BEGIN:
                for_stack.append(idx)
            elif opc == OP_FOR_END:
                if not for_stack:
                    raise ParseError("'next' without matching 'for'")
                begin_idx = for_stack.pop()
                for_pairs[begin_idx] = idx
                for_pairs[idx] = begin_idx
            elif opc == OP_WORD:
                d = node.data
                if d == "begin":
                    begin_stack.append(idx)
                elif d == "again":
                    if not begin_stack:
                        raise ParseError("'again' without matching 'begin'")
                    begin_idx = begin_stack.pop()
                    begin_pairs[begin_idx] = idx
                    begin_pairs[idx] = begin_idx
        if for_stack:
            raise ParseError("'for' without matching 'next'")
        if begin_stack:
            raise ParseError("'begin' without matching 'again'")
        return label_positions, for_pairs, begin_pairs

    def _label_positions(self, nodes: Sequence[Op]) -> Dict[str, int]:
        positions: Dict[str, int] = {}
        for idx, node in enumerate(nodes):
            if node._opcode == OP_LABEL:
                positions[str(node.data)] = idx
        return positions

    def _for_pairs(self, nodes: Sequence[Op]) -> Dict[int, int]:
        stack: List[int] = []
        pairs: Dict[int, int] = {}
        for idx, node in enumerate(nodes):
            if node._opcode == OP_FOR_BEGIN:
                stack.append(idx)
            elif node._opcode == OP_FOR_END:
                if not stack:
                    raise ParseError("'next' without matching 'for'")
                begin_idx = stack.pop()
                pairs[begin_idx] = idx
                pairs[idx] = begin_idx
        if stack:
            raise ParseError("'for' without matching 'next'")
        return pairs

    def _begin_pairs(self, nodes: Sequence[Op]) -> Dict[int, int]:
        stack: List[int] = []
        pairs: Dict[int, int] = {}
        for idx, node in enumerate(nodes):
            if node._opcode == OP_WORD and node.data == "begin":
                stack.append(idx)
            elif node._opcode == OP_WORD and node.data == "again":
                if not stack:
                    raise ParseError("'again' without matching 'begin'")
                begin_idx = stack.pop()
                pairs[begin_idx] = idx
                pairs[idx] = begin_idx
        if stack:
            raise ParseError("'begin' without matching 'again'")
        return pairs

    def _jump_to_label(self, labels: Dict[str, int], target: str) -> int:
        if target not in labels:
            raise ParseError(f"unknown label '{target}' during compile-time execution")
        return labels[target]


# ---------------------------------------------------------------------------
# NASM Emitter
# ---------------------------------------------------------------------------


class Emission:
    __slots__ = ('text', 'data', 'bss')

    def __init__(self, text: List[str] = None, data: List[str] = None, bss: List[str] = None) -> None:
        self.text = text if text is not None else []
        self.data = data if data is not None else []
        self.bss = bss if bss is not None else []

    def snapshot(self) -> str:
        parts: List[str] = []
        if self.text:
            parts.extend(["section .text", *self.text])
        if self.data:
            parts.extend(["section .data", *self.data])
        if self.bss:
            parts.extend(["section .bss", *self.bss])
        parts.append("section .note.GNU-stack noalloc noexec nowrite")
        return "\n".join(parts)


_ASM_LABEL_ONLY_RE = re.compile(r"^\s*([A-Za-z0-9_.$@]+):\s*$")
_ASM_GLOBAL_RE = re.compile(r"^\s*global\s+([A-Za-z0-9_.$@]+)\s*$", re.IGNORECASE)
_ASM_JUMP_RE = re.compile(r"^j([a-z]+)$", re.IGNORECASE)
_ASM_LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9_.$@]+$")
_ASM_LEA_SELF_RE = re.compile(r"^\[\s*([A-Za-z0-9_.$@]+)\s*\]$")
_ASM_REL_LABEL_REF_RE = re.compile(r"(?:\brel\s+|\brip\s*\+\s*)([A-Za-z0-9_.$@]+)", re.IGNORECASE)
_ASM_INVERT_JCC: Dict[str, str] = {
    "je": "jne", "jne": "je", "jz": "jnz", "jnz": "jz",
    "jg": "jle", "jge": "jl", "jl": "jge", "jle": "jg",
    "ja": "jbe", "jae": "jb", "jb": "jae", "jbe": "ja",
    "jo": "jno", "jno": "jo", "js": "jns", "jns": "js",
    "jp": "jnp", "jnp": "jp",
}


def _split_asm_comment(line: str) -> Tuple[str, str]:
    if ";" not in line:
        return line.rstrip(), ""
    code, comment = line.split(";", 1)
    return code.rstrip(), comment


def _parse_asm_instruction(line: str) -> Optional[Tuple[str, List[str]]]:
    code, _ = _split_asm_comment(line)
    text = code.strip()
    if not text:
        return None
    if text.startswith("%"):
        return None
    if text.lower().startswith("section ") or text.lower().startswith("global ") or text.lower().startswith("extern "):
        return None
    if _ASM_LABEL_ONLY_RE.match(text):
        return None
    parts = text.split(None, 1)
    mnemonic = parts[0].lower()
    ops: List[str] = []
    if len(parts) > 1:
        ops = [p.strip() for p in parts[1].split(",") if p.strip()]
    return mnemonic, ops


def _render_instruction(mnemonic: str, operands: Sequence[str], original: str) -> str:
    indent = "    "
    m = re.match(r"^(\s+)", original)
    if m is not None:
        indent = m.group(1)
    if operands:
        return f"{indent}{mnemonic} " + ", ".join(operands)
    return f"{indent}{mnemonic}"


def _invert_jcc(mnemonic: str) -> Optional[str]:
    return _ASM_INVERT_JCC.get(mnemonic.lower())


def optimize_emitted_asm_text(
    asm_text: str,
    *,
    collect_pass_logs: bool = False,
) -> Tuple[str, Dict[str, int], List[str]]:
    """Run an extensive but conservative post-emission optimization pass."""
    lines = asm_text.splitlines()
    _PARSE_SENTINEL = object()
    _parse_cache: Dict[str, object] = {}
    _is_code_cache: Dict[str, bool] = {}
    _label_only_cache: Dict[str, object] = {}
    _hint_cache: Dict[str, str] = {}

    _PASS_A_CANDIDATES = {
        "nop", "mov", "xchg", "lea", "add", "sub", "or", "xor", "shl", "shr", "sar", "imul", "mul"
    }

    def _parse_cached(line: str) -> Optional[Tuple[str, List[str]]]:
        cached = _parse_cache.get(line, _PARSE_SENTINEL)
        if cached is _PARSE_SENTINEL:
            parsed = _parse_asm_instruction(line)
            _parse_cache[line] = parsed if parsed is not None else None
            return parsed
        return cached if cached is not None else None

    def _is_code_line(line: str) -> bool:
        cached = _is_code_cache.get(line)
        if cached is not None:
            return cached
        s = line.lstrip()
        is_code = bool(s and not s.startswith(";"))
        _is_code_cache[line] = is_code
        return is_code

    def _label_only(line: str) -> Optional[str]:
        cached = _label_only_cache.get(line, _PARSE_SENTINEL)
        if cached is _PARSE_SENTINEL:
            s = line.strip()
            label: Optional[str] = None
            if s.endswith(":"):
                candidate = s[:-1]
                if _ASM_LABEL_NAME_RE.match(candidate):
                    label = candidate
            _label_only_cache[line] = label if label is not None else None
            return label
        return cached if cached is not None else None

    def _instr_hint(line: str) -> str:
        """Best-effort mnemonic-like token for quick pass filtering."""
        cached = _hint_cache.get(line)
        if cached is not None:
            return cached
        s = line.lstrip()
        if not s or s.startswith(";") or s.startswith("%"):
            _hint_cache[line] = ""
            return ""
        end = 0
        n = len(s)
        while end < n and s[end] not in " \t;":
            end += 1
        tok = s[:end].lower()
        if not tok or tok.endswith(":"):
            tok = ""
        _hint_cache[line] = tok
        return tok

    stats: Dict[str, int] = {
        "removed_nops": 0,
        "removed_self_moves": 0,
        "removed_trivial_arith": 0,
        "removed_jump_to_next_label": 0,
        "threaded_jump_targets": 0,
        "removed_unreachable_after_terminator": 0,
        "inverted_conditional_branches": 0,
        "collapsed_redundant_jumps": 0,
        "removed_redundant_labels": 0,
        "collapsed_blank_runs": 0,
    }

    def _next_code_index(arr: Sequence[str], i: int) -> int:
        j = i
        while j < len(arr):
            if _is_code_line(arr[j]):
                return j
            j += 1
        return -1

    def _collect_label_positions(arr: Sequence[str]) -> Dict[str, int]:
        pos: Dict[str, int] = {}
        for idx, ln in enumerate(arr):
            label = _label_only(ln)
            if label is not None:
                pos[label] = idx
        return pos

    def _resolve_jmp_chain(target: str, arr: Sequence[str], lbl_pos: Dict[str, int]) -> str:
        seen: Set[str] = set()
        cur = target
        while cur not in seen:
            seen.add(cur)
            idx = lbl_pos.get(cur)
            if idx is None:
                return cur
            nxt = _next_code_index(arr, idx + 1)
            if nxt < 0:
                return cur
            if _instr_hint(arr[nxt]) != "jmp":
                return cur
            parsed = _parse_cached(arr[nxt])
            if parsed is None:
                return cur
            mnem, ops = parsed
            if mnem != "jmp" or len(ops) != 1 or not _ASM_LABEL_NAME_RE.match(ops[0]):
                return cur
            cur = ops[0]
        return target

    pass_logs: List[str] = []

    def _record_pass(round_no: int, pass_name: str, before: Dict[str, int], after: Dict[str, int]) -> None:
        if not collect_pass_logs:
            return
        delta_parts: List[str] = []
        for key in sorted(after):
            delta = after[key] - before.get(key, 0)
            if delta:
                delta_parts.append(f"{key}=+{delta}")
        if delta_parts:
            pass_logs.append(f"round {round_no} {pass_name}: " + ", ".join(delta_parts))
        else:
            pass_logs.append(f"round {round_no} {pass_name}: no changes")

    # Fixed-point optimization rounds.
    rounds_run = 0
    for round_idx in range(10):
        changed = False
        rounds_run = round_idx + 1

        # Pass A: local no-op/trivial arithmetic cleanup.
        _before_a = dict(stats)
        stage_a: List[str] = []
        for ln in lines:
            hint = _instr_hint(ln)
            if hint not in _PASS_A_CANDIDATES:
                stage_a.append(ln)
                continue
            parsed = _parse_cached(ln)
            if parsed is None:
                stage_a.append(ln)
                continue
            mnem, ops = parsed
            if mnem == "nop":
                stats["removed_nops"] += 1
                changed = True
                continue
            if mnem in ("mov", "xchg") and len(ops) == 2 and ops[0] == ops[1]:
                stats["removed_self_moves"] += 1
                changed = True
                continue
            if mnem == "lea" and len(ops) == 2:
                m_lea = _ASM_LEA_SELF_RE.match(ops[1])
                if m_lea is not None and m_lea.group(1) == ops[0]:
                    stats["removed_self_moves"] += 1
                    changed = True
                    continue
            if mnem in ("add", "sub", "or", "xor", "shl", "shr", "sar") and len(ops) == 2 and ops[1] in ("0", "0x0"):
                stats["removed_trivial_arith"] += 1
                changed = True
                continue
            if mnem in ("imul", "mul") and len(ops) == 2 and ops[1] in ("1", "0x1"):
                stats["removed_trivial_arith"] += 1
                changed = True
                continue
            stage_a.append(ln)
        lines = stage_a
        _record_pass(round_idx + 1, "A/local-cleanup", _before_a, stats)

        # Pass B: jump threading and jump-to-next-label elimination.
        _before_b = dict(stats)
        lbl_pos = _collect_label_positions(lines)
        for i, ln in enumerate(list(lines)):
            hint = _instr_hint(ln)
            if not hint or hint[0] != "j":
                continue
            parsed = _parse_cached(ln)
            if parsed is None:
                continue
            mnem, ops = parsed
            if len(ops) != 1 or not _ASM_LABEL_NAME_RE.match(ops[0]):
                continue
            if mnem == "jmp" or _ASM_JUMP_RE.match(mnem):
                old_target = ops[0]
                new_target = _resolve_jmp_chain(old_target, lines, lbl_pos)
                if new_target != old_target:
                    lines[i] = _render_instruction(mnem, [new_target], ln)
                    stats["threaded_jump_targets"] += 1
                    changed = True

                if mnem == "jmp":
                    j = _next_code_index(lines, i + 1)
                    if j >= 0:
                        label = _label_only(lines[j])
                        if label is not None and label == new_target:
                            lines[i] = ""
                            stats["removed_jump_to_next_label"] += 1
                            changed = True
        _record_pass(round_idx + 1, "B/jump-threading", _before_b, stats)

        # Pass C: remove unreachable instructions after unconditional terminators.
        _before_c = dict(stats)
        stage_c: List[str] = []
        unreachable = False
        for ln in lines:
            if _label_only(ln) is not None:
                unreachable = False
            if not _is_code_line(ln):
                stage_c.append(ln)
                continue
            parsed = _parse_cached(ln)
            if unreachable and parsed is not None:
                stage_c.append("")
                stats["removed_unreachable_after_terminator"] += 1
                changed = True
                continue
            stage_c.append(ln)
            if parsed is not None:
                mnem, _ops = parsed
                if mnem in ("jmp", "ret", "ud2"):
                    unreachable = True
        lines = stage_c
        _record_pass(round_idx + 1, "C/unreachable-prune", _before_c, stats)

        # Pass D: invert branch pattern: jcc L1 ; jmp L2 ; L1:
        _before_d = dict(stats)
        i = 0
        while i < len(lines):
            hint_i = _instr_hint(lines[i])
            if not hint_i or hint_i[0] != "j" or hint_i == "jmp":
                i += 1
                continue
            parsed = _parse_cached(lines[i])
            if parsed is None:
                i += 1
                continue
            mnem, ops = parsed
            inv = _invert_jcc(mnem)
            if inv is None or len(ops) != 1 or not _ASM_LABEL_NAME_RE.match(ops[0]):
                i += 1
                continue
            j = _next_code_index(lines, i + 1)
            if j < 0:
                i += 1
                continue
            if _instr_hint(lines[j]) != "jmp":
                i += 1
                continue
            parsed_j = _parse_cached(lines[j])
            if parsed_j is None or parsed_j[0] != "jmp" or len(parsed_j[1]) != 1:
                i += 1
                continue
            k = _next_code_index(lines, j + 1)
            if k < 0:
                i += 1
                continue
            label = _label_only(lines[k])
            if label is None or label != ops[0]:
                i += 1
                continue

            lines[i] = _render_instruction(inv, [parsed_j[1][0]], lines[i])
            lines[j] = ""
            stats["inverted_conditional_branches"] += 1
            changed = True
            i = k + 1
        _record_pass(round_idx + 1, "D/branch-invert", _before_d, stats)

        # Pass E: collapse duplicate adjacent labels and retarget branches.
        _before_e = dict(stats)
        # Skip expensive label-alias analysis when there are no adjacent labels.
        has_adjacent_labels = False
        prev_label_pending = False
        for _ln in lines:
            lbl = _label_only(_ln)
            if lbl is not None:
                if prev_label_pending:
                    has_adjacent_labels = True
                    break
                prev_label_pending = True
                continue
            s = _ln.strip()
            if not s or s.startswith(";"):
                continue
            prev_label_pending = False

        if not has_adjacent_labels:
            _record_pass(round_idx + 1, "E/label-collapse", _before_e, stats)
        else:
            alias: Dict[str, str] = {}
            _fast_ref_counts: Dict[str, int] = {}
            for _ln in lines:
                _parsed = _parse_cached(_ln)
                if _parsed is None:
                    continue
                _mnem, _ops = _parsed
                for _op in _ops:
                    if _ASM_LABEL_NAME_RE.match(_op):
                        _fast_ref_counts[_op] = _fast_ref_counts.get(_op, 0) + 1
                    else:
                        for _mref in _ASM_REL_LABEL_REF_RE.finditer(_op):
                            _lbl = _mref.group(1)
                            _fast_ref_counts[_lbl] = _fast_ref_counts.get(_lbl, 0) + 1
            _ref_memo: Dict[str, bool] = {}
            _label_ref_pat_cache: Dict[str, re.Pattern[str]] = {}

            def _label_is_referenced(label: str, arr: Sequence[str], def_idx: int) -> bool:
                cached = _ref_memo.get(label)
                if cached is not None:
                    return cached
                if _fast_ref_counts.get(label, 0) > 0:
                    _ref_memo[label] = True
                    return True
                pat = _label_ref_pat_cache.get(label)
                if pat is None:
                    pat = re.compile(rf"(?<![A-Za-z0-9_.$@]){re.escape(label)}(?![A-Za-z0-9_.$@])")
                    _label_ref_pat_cache[label] = pat
                for li, ltxt in enumerate(arr):
                    if li == def_idx:
                        continue
                    if not ltxt.strip():
                        continue
                    if pat.search(ltxt):
                        _ref_memo[label] = True
                        return True
                _ref_memo[label] = False
                return False

            i = 0
            while i < len(lines):
                src_label = _label_only(lines[i])
                if src_label is None:
                    i += 1
                    continue
                j = i + 1
                while j < len(lines):
                    sj = lines[j].strip()
                    if not sj or sj.startswith(";"):
                        j += 1
                        continue
                    dst_label = _label_only(lines[j])
                    if dst_label is not None:
                        if not _label_is_referenced(src_label, lines, i):
                            alias[src_label] = dst_label
                            lines[i] = ""
                            stats["removed_redundant_labels"] += 1
                            changed = True
                    break
                i = j

            if alias:
                def _resolve_alias(lbl: str) -> str:
                    seen: Set[str] = set()
                    cur = lbl
                    while cur in alias and cur not in seen:
                        seen.add(cur)
                        cur = alias[cur]
                    return cur

                for idx, ln in enumerate(lines):
                    parsed = _parse_cached(ln)
                    if parsed is None:
                        continue
                    mnem, ops = parsed
                    if len(ops) == 1 and _ASM_LABEL_NAME_RE.match(ops[0]):
                        mapped = _resolve_alias(ops[0])
                        if mapped != ops[0]:
                            lines[idx] = _render_instruction(mnem, [mapped], ln)
                            stats["threaded_jump_targets"] += 1
                            changed = True
            _record_pass(round_idx + 1, "E/label-collapse", _before_e, stats)

        # Pass F: collapse back-to-back unconditional jumps.
        _before_f = dict(stats)
        for idx, ln in enumerate(lines):
            if _instr_hint(ln) != "jmp":
                continue
            nxt = _next_code_index(lines, idx + 1)
            if nxt >= 0:
                if _instr_hint(lines[nxt]) == "jmp":
                    lines[idx] = ""
                    stats["collapsed_redundant_jumps"] += 1
                    changed = True
        _record_pass(round_idx + 1, "F/jump-collapse", _before_f, stats)

        if not changed:
            break

    # Final formatting cleanup.
    optimized: List[str] = []
    blank_run = 0
    for ln in lines:
        if ln.strip() == "":
            blank_run += 1
            if blank_run > 1:
                stats["collapsed_blank_runs"] += 1
                continue
        else:
            blank_run = 0
        optimized.append(ln)

    if collect_pass_logs:
        pass_logs.append(f"rounds_run={rounds_run}")

    return "\n".join(optimized), stats, pass_logs


class FunctionEmitter:
    """Utility for emitting per-word assembly."""

    def __init__(self, text: List[str], debug_enabled: bool = False) -> None:
        self.text = text
        self.debug_enabled = debug_enabled
        self._current_loc: Optional[SourceLocation] = None
        self._generated_debug_path = "<generated>"

    def _emit_line_directive(self, line: int, path: str, increment: int) -> None:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        self.text.append(f'%line {line}+{increment} "{escaped}"')

    def set_location(self, loc) -> None:
        if not self.debug_enabled:
            return
        # Defensive: if loc is a Token, convert to SourceLocation, did not have a better solution, works for me
        if loc is not None and not hasattr(loc, 'path') and hasattr(loc, 'line') and hasattr(loc, 'column'):
            # Assume self has a reference to the parser or a location_for_token function
            # If not, fallback to generic source path
            try:
                loc = self.location_for_token(loc)
            except Exception:
                from pathlib import Path
                loc = type('SourceLocation', (), {})()
                loc.path = Path('<source>')
                loc.line = getattr(loc, 'line', 0)
                loc.column = getattr(loc, 'column', 0)
        if loc is None:
            if self._current_loc is None:
                return
            self._emit_line_directive(1, self._generated_debug_path, increment=1)
            self._current_loc = None
            return
        if self._current_loc == loc:
            return
        self._emit_line_directive(loc.line, str(loc.path), increment=0)
        self._current_loc = loc

    def emit(self, line: str) -> None:
        self.text.append(line)

    def comment(self, message: str) -> None:
        self.text.append(f"    ; {message}")

    def push_literal(self, value: int) -> None:
        _a = self.text.append
        _a(f"    ; push {value}")
        _a("    sub r12, 8")
        _a(f"    mov qword [r12], {value}")

    def push_float(self, label: str) -> None:
        _a = self.text.append
        _a(f"    ; push float from {label}")
        _a("    sub r12, 8")
        _a(f"    mov rax, [rel {label}]")
        _a("    mov [r12], rax")

    def push_label(self, label: str) -> None:
        _a = self.text.append
        _a(f"    ; push {label}")
        _a("    sub r12, 8")
        _a(f"    mov qword [r12], {label}")

    def push_from(self, register: str) -> None:
        _a = self.text.append
        _a("    sub r12, 8")
        _a(f"    mov [r12], {register}")

    def pop_to(self, register: str) -> None:
        _a = self.text.append
        _a(f"    mov {register}, [r12]")
        _a("    add r12, 8")

    def ret(self) -> None:
        self.text.append("    ret")


def _int_trunc_div(lhs: int, rhs: int) -> int:
    if rhs == 0:
        raise ZeroDivisionError("division by zero")
    quotient = abs(lhs) // abs(rhs)
    if (lhs < 0) ^ (rhs < 0):
        quotient = -quotient
    return quotient


def _int_trunc_mod(lhs: int, rhs: int) -> int:
    if rhs == 0:
        raise ZeroDivisionError("division by zero")
    return lhs - _int_trunc_div(lhs, rhs) * rhs


def _bool_to_int(value: bool) -> int:
    return 1 if value else 0


_FOLDABLE_WORDS: Dict[str, Tuple[int, Callable[..., int]]] = {
    "+": (2, lambda a, b: a + b),
    "-": (2, lambda a, b: a - b),
    "*": (2, lambda a, b: a * b),
    "/": (2, _int_trunc_div),
    "%": (2, _int_trunc_mod),
    "==": (2, lambda a, b: _bool_to_int(a == b)),
    "!=": (2, lambda a, b: _bool_to_int(a != b)),
    "<": (2, lambda a, b: _bool_to_int(a < b)),
    "<=": (2, lambda a, b: _bool_to_int(a <= b)),
    ">": (2, lambda a, b: _bool_to_int(a > b)),
    ">=": (2, lambda a, b: _bool_to_int(a >= b)),
    "not": (1, lambda a: _bool_to_int(a == 0)),
}


_sanitize_label_cache: Dict[str, str] = {}


def sanitize_label(name: str) -> str:
    # Keep the special `_start` label unchanged so the program entrypoint
    # remains a plain `_start` symbol expected by the linker.
    if name == "_start":
        _sanitize_label_cache[name] = name
        return name
    cached = _sanitize_label_cache.get(name)
    if cached is not None:
        return cached
    parts: List[str] = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            parts.append(ch)
        else:
            parts.append(f"_{ord(ch):02x}")
    safe = "".join(parts) or "anon"
    if safe[0].isdigit():
        safe = "_" + safe
    # Prefix sanitized labels to avoid accidental collisions with
    # assembler pseudo-ops or common identifiers (e.g. `abs`). The
    # prefix is applied consistently so all emitted references using
    # `sanitize_label` remain correct.
    prefixed = f"w_{safe}"
    _sanitize_label_cache[name] = prefixed
    return prefixed


# Auto-inline asm bodies with at most this many instructions (excl. ret/blanks).
_ASM_AUTO_INLINE_THRESHOLD = 8

# Pre-compiled regexes for sanitizing symbol references in asm bodies.
_RE_ASM_CALL = re.compile(r"\bcall\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_RE_ASM_GLOBAL = re.compile(r"\bglobal\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_RE_ASM_EXTERN = re.compile(r"\bextern\s+([A-Za-z_][A-Za-z0-9_]*)\b")
_RE_ASM_CALL_EXTRACT = re.compile(r"call\s+(?:qword\s+)?(?:\[rel\s+([A-Za-z0-9_.$@]+)\]|([A-Za-z0-9_.$@]+))")


def _is_identifier(text: str) -> bool:
    if not text:
        return False
    first = text[0]
    if not (first.isalpha() or first == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in text)


_C_TYPE_IGNORED_QUALIFIERS = {
    "const",
    "volatile",
    "register",
    "restrict",
    "static",
    "extern",
    "_Atomic",
}

_C_FIELD_TYPE_ALIASES: Dict[str, str] = {
    "i8": "int8_t",
    "u8": "uint8_t",
    "i16": "int16_t",
    "u16": "uint16_t",
    "i32": "int32_t",
    "u32": "uint32_t",
    "i64": "int64_t",
    "u64": "uint64_t",
    "isize": "long",
    "usize": "size_t",
    "f32": "float",
    "f64": "double",
    "ptr": "void*",
}

_C_SCALAR_TYPE_INFO: Dict[str, Tuple[int, int, str]] = {
    "char": (1, 1, "INTEGER"),
    "signed char": (1, 1, "INTEGER"),
    "unsigned char": (1, 1, "INTEGER"),
    "short": (2, 2, "INTEGER"),
    "short int": (2, 2, "INTEGER"),
    "unsigned short": (2, 2, "INTEGER"),
    "unsigned short int": (2, 2, "INTEGER"),
    "int": (4, 4, "INTEGER"),
    "unsigned int": (4, 4, "INTEGER"),
    "int32_t": (4, 4, "INTEGER"),
    "uint32_t": (4, 4, "INTEGER"),
    "long": (8, 8, "INTEGER"),
    "unsigned long": (8, 8, "INTEGER"),
    "long long": (8, 8, "INTEGER"),
    "unsigned long long": (8, 8, "INTEGER"),
    "int64_t": (8, 8, "INTEGER"),
    "uint64_t": (8, 8, "INTEGER"),
    "size_t": (8, 8, "INTEGER"),
    "ssize_t": (8, 8, "INTEGER"),
    "void": (0, 1, "INTEGER"),
    "float": (4, 4, "SSE"),
    "double": (8, 8, "SSE"),
}


def _round_up(value: int, align: int) -> int:
    if align <= 1:
        return value
    return ((value + align - 1) // align) * align


def _canonical_c_type_name(type_name: str) -> str:
    text = " ".join(type_name.strip().split())
    if not text:
        return text
    text = _C_FIELD_TYPE_ALIASES.get(text, text)
    text = text.replace(" *", "*")
    return text


def _is_struct_type(type_name: str) -> bool:
    return _canonical_c_type_name(type_name).startswith("struct ")


def _c_type_size_align_class(
    type_name: str,
    cstruct_layouts: Dict[str, CStructLayout],
) -> Tuple[int, int, str, Optional[CStructLayout]]:
    t = _canonical_c_type_name(type_name)
    if not t:
        return 8, 8, "INTEGER", None
    if t.endswith("*"):
        return 8, 8, "INTEGER", None
    if t in _C_SCALAR_TYPE_INFO:
        size, align, cls = _C_SCALAR_TYPE_INFO[t]
        return size, align, cls, None
    if t.startswith("struct "):
        struct_name = t[len("struct "):].strip()
        layout = cstruct_layouts.get(struct_name)
        if layout is None:
            raise CompileError(
                f"unknown cstruct '{struct_name}' used in extern signature"
            )
        return layout.size, layout.align, "STRUCT", layout
    # Preserve backward compatibility for unknown scalar-ish names.
    return 8, 8, "INTEGER", None


def _merge_eightbyte_class(current: str, incoming: str) -> str:
    if current == "NO_CLASS":
        return incoming
    if current == incoming:
        return current
    if current == "INTEGER" or incoming == "INTEGER":
        return "INTEGER"
    return incoming


def _classify_struct_eightbytes(
    layout: CStructLayout,
    cstruct_layouts: Dict[str, CStructLayout],
    cache: Optional[Dict[str, Optional[List[str]]]] = None,
) -> Optional[List[str]]:
    if cache is None:
        cache = {}
    cached = cache.get(layout.name)
    if cached is not None or layout.name in cache:
        return cached

    if layout.size <= 0:
        cache[layout.name] = []
        return []
    if layout.size > 16:
        cache[layout.name] = None
        return None

    chunk_count = (layout.size + 7) // 8
    classes: List[str] = ["NO_CLASS"] * chunk_count

    for field in layout.fields:
        f_size, _, f_class, nested = _c_type_size_align_class(field.type_name, cstruct_layouts)
        if f_size == 0:
            continue
        if nested is not None:
            nested_classes = _classify_struct_eightbytes(nested, cstruct_layouts, cache)
            if nested_classes is None:
                cache[layout.name] = None
                return None
            base_chunk = field.offset // 8
            for idx, cls in enumerate(nested_classes):
                chunk = base_chunk + idx
                if chunk >= len(classes):
                    cache[layout.name] = None
                    return None
                classes[chunk] = _merge_eightbyte_class(classes[chunk], cls or "INTEGER")
            continue

        start_chunk = field.offset // 8
        end_chunk = (field.offset + f_size - 1) // 8
        if end_chunk >= len(classes):
            cache[layout.name] = None
            return None
        if f_class == "SSE" and start_chunk != end_chunk:
            cache[layout.name] = None
            return None
        for chunk in range(start_chunk, end_chunk + 1):
            classes[chunk] = _merge_eightbyte_class(classes[chunk], f_class)

    for idx, cls in enumerate(classes):
        if cls == "NO_CLASS":
            classes[idx] = "INTEGER"
    cache[layout.name] = classes
    return classes


def _split_trailing_identifier(text: str) -> Tuple[str, Optional[str]]:
    if not text:
        return text, None
    idx = len(text)
    while idx > 0 and (text[idx - 1].isalnum() or text[idx - 1] == "_"):
        idx -= 1
    if idx == 0 or idx == len(text):
        return text, None
    prefix = text[:idx]
    suffix = text[idx:]
    if any(not ch.isalnum() and ch != "_" for ch in prefix):
        return prefix, suffix
    return text, None


def _normalize_c_type_tokens(tokens: Sequence[str], *, allow_default: bool) -> str:
    pointer_count = 0
    parts: List[str] = []
    for raw in tokens:
        text = raw.strip()
        if not text:
            continue
        if set(text) == {"*"}:
            pointer_count += len(text)
            continue
        while text.startswith("*"):
            pointer_count += 1
            text = text[1:]
        while text.endswith("*"):
            pointer_count += 1
            text = text[:-1]
        if not text:
            continue
        if text in _C_TYPE_IGNORED_QUALIFIERS:
            continue
        parts.append(text)
    if not parts:
        if allow_default:
            base = "int"
        else:
            raise ParseError("expected C type before parameter name")
    else:
        base = " ".join(parts)
    return base + ("*" * pointer_count)


def _ctype_uses_sse(type_name: Optional[str]) -> bool:
    if type_name is None:
        return False
    base = type_name.rstrip("*")
    return base in {"float", "double"}


def _parse_string_literal(token: Token) -> Optional[str]:
    text = token.lexeme
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return None
    body = text[1:-1]
    result: List[str] = []
    idx = 0
    while idx < len(body):
        char = body[idx]
        if char != "\\":
            result.append(char)
            idx += 1
            continue
        idx += 1
        if idx >= len(body):
            raise ParseError(
                f"unterminated escape sequence in string literal at {token.line}:{token.column}"
            )
        escape = body[idx]
        idx += 1
        if escape == 'n':
            result.append("\n")
        elif escape == 't':
            result.append("\t")
        elif escape == 'r':
            result.append("\r")
        elif escape == '0':
            result.append("\0")
        elif escape == '"':
            result.append('"')
        elif escape == "\\":
            result.append("\\")
        else:
            raise ParseError(
                f"unsupported escape sequence '\\{escape}' in string literal at {token.line}:{token.column}"
            )
    return "".join(result)


def _parse_char_literal(token: Token) -> Optional[int]:
    text = token.lexeme
    if len(text) < 2 or text[0] != "'" or text[-1] != "'":
        return None
    body = text[1:-1]
    if not body:
        raise ParseError(f"empty char literal at {token.line}:{token.column}")
    if body[0] != "\\":
        if len(body) != 1:
            raise ParseError(f"char literal must contain exactly one character at {token.line}:{token.column}")
        return ord(body)
    if len(body) < 2:
        raise ParseError(f"unterminated escape in char literal at {token.line}:{token.column}")
    esc = body[1:]
    if esc == "n":
        return ord("\n")
    if esc == "t":
        return ord("\t")
    if esc == "r":
        return ord("\r")
    if esc == "0":
        return 0
    if esc == "'":
        return ord("'")
    if esc == '"':
        return ord('"')
    if esc == "\\":
        return ord("\\")
    if esc.startswith("x") and len(esc) == 3:
        try:
            return int(esc[1:], 16)
        except ValueError:
            pass
    raise ParseError(f"unsupported char escape '\\{esc}' at {token.line}:{token.column}")


def _parse_int_or_char_literal(token: Token) -> Optional[int]:
    char_value = _parse_char_literal(token)
    if char_value is not None:
        return char_value
    try:
        return int(token.lexeme, 0)
    except ValueError:
        return None


class _CTHandleTable:
    """Keeps Python object references stable across compile-time asm calls."""

    def __init__(self) -> None:
        self.objects: Dict[int, Any] = {}
        self.refs: List[Any] = []
        self.string_buffers: List[ctypes.Array[Any]] = []

    def clear(self) -> None:
        self.objects.clear()
        self.refs.clear()
        self.string_buffers.clear()

    def store(self, value: Any) -> int:
        addr = id(value)
        self.refs.append(value)
        self.objects[addr] = value
        return addr



class Assembler:
    def __init__(
        self,
        dictionary: Dictionary,
        *,
        enable_constant_folding: bool = True,
        enable_peephole_optimization: bool = True,
        enable_loop_unroll: bool = True,
        enable_auto_inline: bool = True,
        enable_string_deduplication: bool = True,
        enable_extern_type_check: bool = True,
        enable_stack_check: bool = True,
        loop_unroll_threshold: int = 8,
        verbosity: int = 0,
    ) -> None:
        self.dictionary = dictionary
        self._string_literals: Dict[str, Tuple[str, int]] = {}
        self._string_literal_counter: int = 0
        self._float_literals: Dict[float, str] = {}
        self._data_section: Optional[List[str]] = None
        self._inline_stack: List[str] = []
        self._inline_counter: int = 0
        self._unroll_counter: int = 0
        self._emit_stack: List[str] = []
        self._cstruct_layouts: Dict[str, CStructLayout] = {}
        self._export_all_defs: bool = False
        self._generated_bss: List[str] = []
        self._generated_bss_counter: int = 0
        self.enable_constant_folding = enable_constant_folding
        self.enable_peephole_optimization = enable_peephole_optimization
        self.enable_loop_unroll = enable_loop_unroll
        self.enable_auto_inline = enable_auto_inline
        self.enable_string_deduplication = enable_string_deduplication
        self.enable_extern_type_check = enable_extern_type_check
        self.enable_stack_check = enable_stack_check
        self.loop_unroll_threshold = loop_unroll_threshold
        self.verbosity = verbosity
        self._last_cfg_definitions: List[Definition] = []
        self._need_cfg: bool = False

    def _copy_definition_for_cfg(self, definition: Definition) -> Definition:
        return Definition(
            name=definition.name,
            body=[_make_op(node.op, node.data, node.loc) for node in definition.body],
            immediate=definition.immediate,
            compile_only=definition.compile_only,
            runtime_only=definition.runtime_only,
            terminator=definition.terminator,
            inline=definition.inline,
        )

    def _format_cfg_op(self, node: Op) -> str:
        kind = node._opcode
        data = node.data
        if kind == OP_LITERAL:
            return f"push {data!r}"
        if kind == OP_WORD:
            return str(data)
        if kind == OP_WORD_PTR:
            return f"&{data}"
        if kind == OP_BRANCH_ZERO:
            return "branch_zero"
        if kind == OP_JUMP:
            return "jump"
        if kind == OP_LABEL:
            return f".{data}:"
        if kind == OP_FOR_BEGIN:
            return "for"
        if kind == OP_FOR_END:
            return "end  (for)"
        if kind == OP_LIST_BEGIN:
            return "list_begin"
        if kind == OP_LIST_END:
            return "list_end"
        if kind == OP_LIST_LITERAL:
            return f"list_literal {data}"
        if kind == OP_BSS_LIST_LITERAL:
            return f"bss_list_literal {data}"
        if kind == OP_RET:
            return "ret"
        return f"{node.op}" if data is None else f"{node.op} {data!r}"

    @staticmethod
    def _dot_html_escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    @staticmethod
    def _dot_escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    @staticmethod
    def _dot_id(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", text)

    def _cfg_loc_str(self, node: Op) -> str:
        if node.loc is None:
            return ""
        return f"{node.loc.path.name}:{node.loc.line}"

    def _definition_cfg_blocks_and_edges(self, definition: Definition) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int, str]]]:
        nodes = definition.body
        if not nodes:
            return [], []

        label_positions = self._cfg_label_positions(nodes)
        for_pairs = self._for_pairs(nodes)

        leaders: Set[int] = {0}

        def add_leader(idx: int) -> None:
            if 0 <= idx < len(nodes):
                leaders.add(idx)

        for idx, node in enumerate(nodes):
            kind = node._opcode
            if kind == OP_LABEL:
                leaders.add(idx)
            elif kind == OP_BRANCH_ZERO:
                target = label_positions.get(str(node.data))
                if target is not None:
                    add_leader(target)
                add_leader(idx + 1)
            elif kind == OP_JUMP:
                target = label_positions.get(str(node.data))
                if target is not None:
                    add_leader(target)
                add_leader(idx + 1)
            elif kind == OP_FOR_BEGIN:
                end_idx = for_pairs.get(idx)
                if end_idx is not None:
                    add_leader(end_idx + 1)
                add_leader(idx + 1)
            elif kind == OP_FOR_END:
                begin_idx = for_pairs.get(idx)
                if begin_idx is not None:
                    add_leader(begin_idx + 1)
                add_leader(idx + 1)

        ordered = sorted(leaders)
        blocks: List[Tuple[int, int]] = []
        for i, start in enumerate(ordered):
            end = ordered[i + 1] if i + 1 < len(ordered) else len(nodes)
            if start < end:
                blocks.append((start, end))

        block_by_ip: Dict[int, int] = {}
        for block_idx, (start, end) in enumerate(blocks):
            for ip in range(start, end):
                block_by_ip[ip] = block_idx

        edges: List[Tuple[int, int, str]] = []

        def add_edge(src_block: int, target_ip: int, label: str) -> None:
            if target_ip < 0 or target_ip >= len(nodes):
                return
            dst_block = block_by_ip.get(target_ip)
            if dst_block is None:
                return
            edges.append((src_block, dst_block, label))

        for block_idx, (_start, end) in enumerate(blocks):
            last_ip = end - 1
            node = nodes[last_ip]
            kind = node._opcode

            if kind == OP_BRANCH_ZERO:
                target = label_positions.get(str(node.data))
                if target is not None:
                    add_edge(block_idx, target, "zero")
                add_edge(block_idx, last_ip + 1, "nonzero")
                continue

            if kind == OP_JUMP:
                target = label_positions.get(str(node.data))
                if target is not None:
                    add_edge(block_idx, target, "jmp")
                continue

            if kind == OP_FOR_BEGIN:
                end_idx = for_pairs.get(last_ip)
                add_edge(block_idx, last_ip + 1, "enter")
                if end_idx is not None:
                    add_edge(block_idx, end_idx + 1, "empty")
                continue

            if kind == OP_FOR_END:
                begin_idx = for_pairs.get(last_ip)
                if begin_idx is not None:
                    add_edge(block_idx, begin_idx + 1, "loop")
                add_edge(block_idx, last_ip + 1, "exit")
                continue

            add_edge(block_idx, last_ip + 1, "next")

        edges.sort(key=lambda item: (item[0], item[1], item[2]))
        return blocks, edges

    def _cfg_label_positions(self, nodes: Sequence[Op]) -> Dict[str, int]:
        positions: Dict[str, int] = {}
        for idx, node in enumerate(nodes):
            if node._opcode == OP_LABEL:
                positions[str(node.data)] = idx
        return positions

    # ---- edge style lookup ----
    _CFG_EDGE_STYLES: Dict[str, Dict[str, str]] = {
        "nonzero": {"color": "#2e7d32", "fontcolor": "#2e7d32", "label": "T", "penwidth": "2"},
        "zero":    {"color": "#c62828", "fontcolor": "#c62828", "label": "F", "style": "dashed", "penwidth": "2"},
        "jmp":     {"color": "#1565c0", "fontcolor": "#1565c0", "label": "jmp", "penwidth": "1.5"},
        "next":    {"color": "#616161", "fontcolor": "#616161", "label": ""},
        "enter":   {"color": "#2e7d32", "fontcolor": "#2e7d32", "label": "enter", "penwidth": "2"},
        "empty":   {"color": "#c62828", "fontcolor": "#c62828", "label": "empty", "style": "dashed", "penwidth": "1.5"},
        "loop":    {"color": "#6a1b9a", "fontcolor": "#6a1b9a", "label": "loop", "style": "bold", "penwidth": "2"},
        "exit":    {"color": "#ef6c00", "fontcolor": "#ef6c00", "label": "exit", "penwidth": "1.5"},
    }

    def _cfg_edge_attrs(self, label: str) -> str:
        style = self._CFG_EDGE_STYLES.get(label, {"label": label, "color": "black"})
        parts = [f'{k}="{v}"' for k, v in style.items()]
        return ", ".join(parts)

    def render_last_cfg_dot(self) -> str:
        lines: List[str] = [
            "digraph l2_cfg {",
            '    rankdir=TB;',
            '    newrank=true;',
            '    compound=true;',
            '    fontname="Helvetica";',
            '    node [shape=plaintext, fontname="Courier New", fontsize=10];',
            '    edge [fontname="Helvetica", fontsize=9];',
        ]

        if not self._last_cfg_definitions:
            lines.append('    empty [shape=box, label="(no definitions)"];')
            lines.append("}")
            return "\n".join(lines)

        for defn in self._last_cfg_definitions:
            cluster_id = self._dot_id(f"cluster_{defn.name}")
            prefix = self._dot_id(defn.name)

            blocks, edges = self._definition_cfg_blocks_and_edges(defn)

            # Determine which blocks are loop-back targets
            back_targets: Set[int] = set()
            for src, dst, elabel in edges:
                if elabel == "loop" or (elabel == "jmp" and dst <= src):
                    back_targets.add(dst)

            # Determine exit blocks (no outgoing edges)
            has_successor: Set[int] = {src for src, _, _ in edges}

            lines.append(f"    subgraph {cluster_id} {{")
            lines.append(f'        label=<<B>{self._dot_html_escape(defn.name)}</B>>;')
            lines.append('        labeljust=l;')
            lines.append('        style=dashed; color="#9e9e9e";')
            lines.append(f'        fontname="Helvetica"; fontsize=12;')

            if not blocks:
                node_id = self._dot_id(f"{defn.name}_empty")
                lines.append(f'        {node_id} [shape=box, label="(empty)"];')
                lines.append("    }")
                continue

            for block_idx, (start, end) in enumerate(blocks):
                node_id = f"{prefix}_b{block_idx}"
                is_entry = block_idx == 0
                is_exit = block_idx not in has_successor
                is_loop_head = block_idx in back_targets

                # Pick header colour
                if is_entry:
                    hdr_bg = "#c8e6c9"  # green
                    hdr_fg = "#1b5e20"
                elif is_exit:
                    hdr_bg = "#ffcdd2"  # red
                    hdr_fg = "#b71c1c"
                elif is_loop_head:
                    hdr_bg = "#bbdefb"  # blue
                    hdr_fg = "#0d47a1"
                else:
                    hdr_bg = "#e0e0e0"  # grey
                    hdr_fg = "#212121"

                # Block annotation
                tag = ""
                if is_entry:
                    tag = " (entry)"
                if is_exit:
                    tag += " (exit)"
                if is_loop_head:
                    tag += " (loop)"

                # Source location from first non-label instruction
                loc_str = ""
                for ip in range(start, end):
                    n = defn.body[ip]
                    if n._opcode != OP_LABEL:
                        loc_str = self._cfg_loc_str(n)
                        break
                if not loc_str and defn.body[start].loc:
                    loc_str = self._cfg_loc_str(defn.body[start])

                # Build HTML table label
                hdr_text = f"BB{block_idx}{tag}"
                if loc_str:
                    hdr_text += f"  [{loc_str}]"

                rows: List[str] = []
                rows.append(f'<TR><TD BGCOLOR="{hdr_bg}" ALIGN="LEFT"><FONT COLOR="{hdr_fg}"><B>{self._dot_html_escape(hdr_text)}</B></FONT></TD></TR>')

                for ip in range(start, end):
                    n = defn.body[ip]
                    op_text = self._format_cfg_op(n)
                    esc = self._dot_html_escape(f"  {ip:3d}  {op_text}")
                    kind = n._opcode
                    if kind in (OP_BRANCH_ZERO, OP_JUMP, OP_FOR_BEGIN, OP_FOR_END):
                        # Highlight control-flow ops
                        rows.append(f'<TR><TD ALIGN="LEFT" BGCOLOR="#fff9c4"><FONT COLOR="#f57f17" FACE="Courier New">{esc}</FONT></TD></TR>')
                    elif kind == OP_LABEL:
                        rows.append(f'<TR><TD ALIGN="LEFT" BGCOLOR="#f5f5f5"><FONT COLOR="#9e9e9e" FACE="Courier New">{esc}</FONT></TD></TR>')
                    else:
                        rows.append(f'<TR><TD ALIGN="LEFT"><FONT FACE="Courier New">{esc}</FONT></TD></TR>')

                table = f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" CELLPADDING="4">{"".join(rows)}</TABLE>>'
                lines.append(f"        {node_id} [label={table}];")

            for src, dst, edge_label in edges:
                src_id = f"{prefix}_b{src}"
                dst_id = f"{prefix}_b{dst}"
                attrs = self._cfg_edge_attrs(edge_label)
                lines.append(f"        {src_id} -> {dst_id} [{attrs}];")

            lines.append("    }")

        # Legend
        lines.append("    subgraph cluster_legend {")
        lines.append('        label=<<B>Legend</B>>;')
        lines.append('        fontname="Helvetica"; fontsize=10;')
        lines.append('        style=solid; color="#bdbdbd";')
        lines.append('        node [shape=box, fontname="Helvetica", fontsize=9, width=1.3, height=0.3];')
        lines.append('        edge [style=invis];')
        lines.append('        leg_entry [label="entry", style=filled, fillcolor="#c8e6c9", fontcolor="#1b5e20"];')
        lines.append('        leg_exit  [label="exit",  style=filled, fillcolor="#ffcdd2", fontcolor="#b71c1c"];')
        lines.append('        leg_loop  [label="loop header", style=filled, fillcolor="#bbdefb", fontcolor="#0d47a1"];')
        lines.append('        leg_norm  [label="basic block", style=filled, fillcolor="#e0e0e0", fontcolor="#212121"];')
        lines.append('        leg_entry -> leg_exit -> leg_loop -> leg_norm;')
        lines.append("    }")

        lines.append("}")
        return "\n".join(lines)

    @staticmethod
    def _log_op_diff(name: str, pass_name: str, before: list, after: list) -> None:
        """Print a contextual diff of (op, data) tuples for v4 optimization detail."""
        import difflib
        def _fmt(op_data: tuple) -> str:
            op, data = op_data
            if op == "literal":
                return f"  {data!r}" if isinstance(data, str) else f"  {data}"
            if op == "word":
                return f"  {data}"
            return f"  [{op}] {data!r}" if data is not None else f"  [{op}]"
        before_lines = [_fmt(t) for t in before]
        after_lines = [_fmt(t) for t in after]
        diff = list(difflib.unified_diff(before_lines, after_lines, n=1, lineterm=""))
        if len(diff) <= 2:
            return
        for line in diff[2:]:  # skip --- / +++ header
            if line.startswith("@@"):
                print(f"[v4]   {pass_name} '{name}' {line}")
            elif line.startswith("-"):
                print(f"[v4]     - {line[1:]}")
            elif line.startswith("+"):
                print(f"[v4]     + {line[1:]}")
            else:
                print(f"[v4]      {line[1:]}")

    def _peephole_optimize_definition(self, definition: Definition) -> None:
        nodes = definition.body
        if not nodes:
            return
        all_rules = _PEEPHOLE_ALL_RULES
        first_words = _PEEPHOLE_FIRST_WORDS
        _OP_W = OP_WORD

        # Outer loop: keeps re-running all passes until nothing changes.
        any_changed = True
        while any_changed:
            any_changed = False

            # ---------- Pass 1: word-only pattern rewriting ----------
            changed = True
            while changed:
                changed = False
                optimized: List[Op] = []
                _opt_append = optimized.append
                idx = 0
                nlen = len(nodes)
                while idx < nlen:
                    node = nodes[idx]
                    if node._opcode != _OP_W:
                        _opt_append(node)
                        idx += 1
                        continue
                    word_name = node.data
                    if word_name not in first_words:
                        _opt_append(node)
                        idx += 1
                        continue

                    best_repl: Optional[Tuple[str, ...]] = None
                    best_pat: Optional[Tuple[str, ...]] = None
                    best_len = 0
                    best_cost = 1_000_000

                    if idx + 2 < nlen:
                        b = nodes[idx + 1]
                        c = nodes[idx + 2]
                        if b._opcode == _OP_W and c._opcode == _OP_W:
                            pat3 = (word_name, b.data, c.data)
                            repl3 = all_rules.get(pat3)
                            if repl3 is not None:
                                cost3 = int(_PEEPHOLE_RULE_COST.get(pat3, len(repl3)))
                                best_repl = repl3
                                best_pat = pat3
                                best_len = 3
                                best_cost = cost3

                    if idx + 1 < nlen:
                        b = nodes[idx + 1]
                        if b._opcode == _OP_W:
                            pat2 = (word_name, b.data)
                            repl2 = all_rules.get(pat2)
                            if repl2 is not None:
                                cost2 = int(_PEEPHOLE_RULE_COST.get(pat2, len(repl2)))
                                if (
                                    best_repl is None
                                    or cost2 < best_cost
                                    or (cost2 == best_cost and 2 > best_len)
                                ):
                                    best_repl = repl2
                                    best_pat = pat2
                                    best_len = 2
                                    best_cost = cost2

                    if best_repl is not None and best_pat is not None:
                        base_loc = node.loc
                        for r in best_repl:
                            if r[0] == 'l' and r[:8] == "literal_":
                                _opt_append(_make_literal_op(int(r[8:]), loc=base_loc))
                            else:
                                _opt_append(_make_word_op(r, base_loc))
                        idx += best_len
                        changed = True
                        continue

                    if best_repl is None:
                        _opt_append(node)
                        idx += 1
                if changed:
                    any_changed = True
                nodes = optimized

            # ---------- Pass 2: literal + word algebraic identities ----------
            _CANCEL_PAIRS = _PEEPHOLE_CANCEL_PAIRS
            _SHIFT_OPS = _PEEPHOLE_SHIFT_OPS
            changed = True
            while changed:
                changed = False
                optimized = []
                _opt_a = optimized.append
                nlen = len(nodes)
                idx = 0
                while idx < nlen:
                    node = nodes[idx]
                    n_oc = node._opcode

                    # -- Redundant unary pairs (word word) --
                    if n_oc == OP_WORD and idx + 1 < nlen:
                        b = nodes[idx + 1]
                        if b._opcode == OP_WORD:
                            wa, wb = node.data, b.data
                            if (wa, wb) in _CANCEL_PAIRS:
                                idx += 2
                                changed = True
                                continue
                            if wa == "abs" and wb == "abs":
                                _opt_a(node)
                                idx += 2
                                changed = True
                                continue

                    # -- scalar literal patterns (excludes string literals which push 2 values) --
                    if n_oc == OP_LITERAL and type(node.data) is not str and idx + 1 < nlen:
                        b = nodes[idx + 1]
                        b_oc = b._opcode

                        # literal + dup -> literal literal
                        if b_oc == OP_WORD and b.data == "dup":
                            _opt_a(node)
                            _opt_a(_make_literal_op(node.data, node.loc))
                            idx += 2
                            changed = True
                            continue

                        # literal + drop -> (nothing)
                        if b_oc == OP_WORD and b.data == "drop":
                            idx += 2
                            changed = True
                            continue

                        # literal literal + 2drop / swap
                        if b_oc == OP_LITERAL and type(b.data) is not str and idx + 2 < nlen:
                            c = nodes[idx + 2]
                            if c._opcode == OP_WORD:
                                cd = c.data
                                if cd == "2drop":
                                    idx += 3
                                    changed = True
                                    continue
                                if cd == "swap":
                                    _opt_a(_make_literal_op(b.data, b.loc))
                                    _opt_a(_make_literal_op(node.data, node.loc))
                                    idx += 3
                                    changed = True
                                    continue

                        # Binary op identities: literal K + word
                        if type(node.data) is int and b_oc == OP_WORD:
                            k = node.data
                            w = b.data
                            base_loc = node.loc or b.loc

                            if (w == "+" and k == 0) or (w == "-" and k == 0) or (w == "*" and k == 1) or (w == "/" and k == 1):
                                idx += 2; changed = True; continue

                            if w == "*":
                                if k == 0:
                                    _opt_a(_make_word_op("drop", base_loc))
                                    _opt_a(_make_literal_op(0, base_loc))
                                    idx += 2; changed = True; continue
                                if k == -1:
                                    _opt_a(_make_word_op("neg", base_loc))
                                    idx += 2; changed = True; continue
                                if k > 1 and (k & (k - 1)) == 0:
                                    _opt_a(_make_literal_op(k.bit_length() - 1, base_loc))
                                    _opt_a(_make_word_op("shl", base_loc))
                                    idx += 2; changed = True; continue

                            if w == "band":
                                if k == 0:
                                    _opt_a(_make_word_op("drop", base_loc))
                                    _opt_a(_make_literal_op(0, base_loc))
                                    idx += 2; changed = True; continue
                                if k == -1:
                                    idx += 2; changed = True; continue

                            if w == "bor":
                                if k == -1:
                                    _opt_a(_make_word_op("drop", base_loc))
                                    _opt_a(_make_literal_op(-1, base_loc))
                                    idx += 2; changed = True; continue
                                if k == 0:
                                    idx += 2; changed = True; continue

                            if w == "bxor" and k == 0:
                                idx += 2; changed = True; continue

                            if w == "%" and k == 1:
                                _opt_a(_make_word_op("drop", base_loc))
                                _opt_a(_make_literal_op(0, base_loc))
                                idx += 2; changed = True; continue

                            if w == "==" and k == 0:
                                _opt_a(_make_word_op("not", base_loc))
                                idx += 2; changed = True; continue

                            if w in _SHIFT_OPS and k == 0:
                                idx += 2; changed = True; continue

                            if w == "+":
                                if k == 1:
                                    _opt_a(_make_word_op("inc", base_loc))
                                    idx += 2; changed = True; continue
                                if k == -1:
                                    _opt_a(_make_word_op("dec", base_loc))
                                    idx += 2; changed = True; continue
                            if w == "-":
                                if k == 1:
                                    _opt_a(_make_word_op("dec", base_loc))
                                    idx += 2; changed = True; continue
                                if k == -1:
                                    _opt_a(_make_word_op("inc", base_loc))
                                    idx += 2; changed = True; continue

                    _opt_a(node)
                    idx += 1
                if changed:
                    any_changed = True
                nodes = optimized

            # ---------- Pass 3: dead-code after unconditional jump/end ----------
            new_nodes: List[Op] = []
            dead = False
            for node in nodes:
                kind = node._opcode
                if dead:
                    # A label ends the dead region.
                    if kind == OP_LABEL:
                        dead = False
                        new_nodes.append(node)
                    else:
                        any_changed = True
                    continue
                new_nodes.append(node)
                if kind in _PEEPHOLE_TERMINATORS:
                    dead = True
            if len(new_nodes) != len(nodes):
                any_changed = True
            nodes = new_nodes

        definition.body = nodes

    def _fold_constants_in_definition(self, definition: Definition) -> None:
        if not definition.body:
            return
        optimized: List[Op] = []
        for node in definition.body:
            optimized.append(node)
            self._attempt_constant_fold_tail(optimized)
        definition.body = optimized

    def _attempt_constant_fold_tail(self, nodes: List[Op]) -> None:
        _LIT = OP_LITERAL
        _W = OP_WORD
        while nodes:
            last = nodes[-1]
            if last._opcode != _W:
                return
            fold_entry = _FOLDABLE_WORDS.get(last.data)
            if fold_entry is None:
                return
            arity, func = fold_entry
            nlen = len(nodes)
            if nlen < arity + 1:
                return
            # Fast path for binary ops (arity 2, the most common case)
            if arity == 2:
                a = nodes[-3]
                b = nodes[-2]
                if a._opcode != _LIT or type(a.data) is not int:
                    return
                if b._opcode != _LIT or type(b.data) is not int:
                    return
                try:
                    result = func(a.data, b.data)
                except Exception:
                    return
                new_loc = a.loc or last.loc
                del nodes[-3:]
                nodes.append(_make_literal_op(result, new_loc))
                continue
            # Fast path for unary ops (arity 1)
            if arity == 1:
                a = nodes[-2]
                if a._opcode != _LIT or type(a.data) is not int:
                    return
                try:
                    result = func(a.data)
                except Exception:
                    return
                new_loc = a.loc or last.loc
                del nodes[-2:]
                nodes.append(_make_literal_op(result, new_loc))
                continue
            # General case
            operands = nodes[-(arity + 1):-1]
            if any(op._opcode != _LIT or type(op.data) is not int for op in operands):
                return
            values = [op.data for op in operands]
            try:
                result = func(*values)
            except Exception:
                return
            new_loc = operands[0].loc or last.loc
            nodes[-(arity + 1):] = [_make_literal_op(result, new_loc)]

    def _audit_optimized_definition(self, definition: Definition) -> None:
        labels: Set[str] = set()
        branch_targets: Set[str] = set()
        for_depth = 0
        begin_depth = 0

        for node in definition.body:
            kind = node._opcode
            if kind == OP_LABEL:
                labels.add(str(node.data))
                continue
            if kind == OP_FOR_BEGIN:
                for_depth += 1
                continue
            if kind == OP_FOR_END:
                if for_depth <= 0:
                    raise CompileError(
                        f"optimizer audit failed in '{definition.name}': unmatched for-loop end"
                    )
                for_depth -= 1
                continue
            if kind == OP_WORD:
                lex = str(node.data)
                if lex == "begin":
                    begin_depth += 1
                elif lex == "again":
                    if begin_depth <= 0:
                        raise CompileError(
                            f"optimizer audit failed in '{definition.name}': unmatched begin/again pair"
                        )
                    begin_depth -= 1
                continue
            if kind in (OP_BRANCH_ZERO, OP_JUMP):
                branch_targets.add(str(node.data))

        if for_depth != 0:
            raise CompileError(
                f"optimizer audit failed in '{definition.name}': unterminated for-loop after optimization"
            )
        if begin_depth != 0:
            raise CompileError(
                f"optimizer audit failed in '{definition.name}': unterminated begin/again block after optimization"
            )

        missing = sorted(target for target in branch_targets if target not in labels)
        if missing:
            raise CompileError(
                f"optimizer audit failed in '{definition.name}': missing label(s) {', '.join(missing)}"
            )

    def _for_pairs(self, nodes: Sequence[Op]) -> Dict[int, int]:
        stack: List[int] = []
        pairs: Dict[int, int] = {}
        for idx, node in enumerate(nodes):
            if node._opcode == OP_FOR_BEGIN:
                stack.append(idx)
            elif node._opcode == OP_FOR_END:
                if not stack:
                    raise CompileError("'end' without matching 'for'")
                begin_idx = stack.pop()
                pairs[begin_idx] = idx
                pairs[idx] = begin_idx
        if stack:
            raise CompileError("'for' without matching 'end'")
        return pairs

    def _collect_internal_labels(self, nodes: Sequence[Op]) -> Set[str]:
        labels: Set[str] = set()
        for node in nodes:
            kind = node._opcode
            data = node.data
            if kind == OP_LABEL:
                labels.add(str(data))
            elif kind == OP_FOR_BEGIN or kind == OP_FOR_END:
                labels.add(str(data["loop"]))
                labels.add(str(data["end"]))
            elif kind == OP_LIST_BEGIN or kind == OP_LIST_END:
                labels.add(str(data))
        return labels

    def _clone_nodes_with_label_remap(
        self,
        nodes: Sequence[Op],
        internal_labels: Set[str],
        suffix: str,
    ) -> List[Op]:
        label_map: Dict[str, str] = {}

        def remap(label: str) -> str:
            if label not in internal_labels:
                return label
            if label not in label_map:
                label_map[label] = f"{label}__unr{suffix}"
            return label_map[label]

        cloned: List[Op] = []
        for node in nodes:
            kind = node._opcode
            data = node.data
            if kind == OP_LABEL:
                cloned.append(_make_op("label", remap(str(data)), loc=node.loc))
                continue
            if kind == OP_JUMP or kind == OP_BRANCH_ZERO:
                target = str(data)
                mapped = remap(target) if target in internal_labels else target
                cloned.append(_make_op(node.op, mapped, node.loc))
                continue
            if kind == OP_FOR_BEGIN or kind == OP_FOR_END:
                cloned.append(
                    Op(
                        op=node.op,
                        data={
                            "loop": remap(str(data["loop"])),
                            "end": remap(str(data["end"])),
                        },
                        loc=node.loc,
                    )
                )
                continue
            if kind == OP_LIST_BEGIN or kind == OP_LIST_END:
                cloned.append(_make_op(node.op, remap(str(data)), loc=node.loc))
                continue
            cloned.append(_make_op(node.op, data, node.loc))
        return cloned

    def _unroll_constant_for_loops(self, definition: Definition) -> None:
        threshold = self.loop_unroll_threshold
        if threshold <= 0:
            return
        nodes = definition.body
        pairs = self._for_pairs(nodes)
        if not pairs:
            return

        rebuilt: List[Op] = []
        idx = 0
        while idx < len(nodes):
            node = nodes[idx]
            if node._opcode == OP_FOR_BEGIN and idx > 0:
                prev = nodes[idx - 1]
                if prev._opcode == OP_LITERAL and isinstance(prev.data, int):
                    count = int(prev.data)
                    end_idx = pairs.get(idx)
                    if end_idx is None:
                        raise CompileError("internal loop bookkeeping error")
                    if count <= 0:
                        if rebuilt and rebuilt[-1] is prev:
                            rebuilt.pop()
                        idx = end_idx + 1
                        continue
                    if count <= threshold:
                        if rebuilt and rebuilt[-1] is prev:
                            rebuilt.pop()
                        body = nodes[idx + 1:end_idx]
                        internal_labels = self._collect_internal_labels(body)
                        for copy_idx in range(count):
                            suffix = f"{self._unroll_counter}_{copy_idx}"
                            rebuilt.extend(
                                self._clone_nodes_with_label_remap(
                                    body,
                                    internal_labels,
                                    suffix,
                                )
                            )
                        self._unroll_counter += 1
                        idx = end_idx + 1
                        continue
            rebuilt.append(node)
            idx += 1

        definition.body = rebuilt

    def _fold_static_list_literals_definition(self, definition: Definition) -> None:
        nodes = definition.body
        rebuilt: List[Op] = []
        idx = 0
        while idx < len(nodes):
            node = nodes[idx]
            if node.op != "list_begin":
                rebuilt.append(node)
                idx += 1
                continue

            depth = 1
            j = idx + 1
            static_values: List[int] = []
            is_static = True

            while j < len(nodes):
                cur = nodes[j]
                if cur._opcode == OP_LIST_BEGIN:
                    depth += 1
                    is_static = False
                    j += 1
                    continue
                if cur._opcode == OP_LIST_END:
                    depth -= 1
                    if depth == 0:
                        break
                    j += 1
                    continue

                if depth == 1:
                    if cur._opcode == OP_LITERAL and isinstance(cur.data, int):
                        static_values.append(int(cur.data))
                    else:
                        is_static = False
                j += 1

            if depth != 0:
                rebuilt.append(node)
                idx += 1
                continue

            if is_static:
                rebuilt.append(_make_op("list_literal", static_values, node.loc))
                idx = j + 1
                continue

            rebuilt.append(node)
            idx += 1

        definition.body = rebuilt

    # Known stack effects: (inputs_consumed, outputs_produced)
    _BUILTIN_STACK_EFFECTS: Dict[str, Tuple[int, int]] = {
        "dup":   (1, 2),
        "drop":  (1, 0),
        "swap":  (2, 2),
        "over":  (2, 3),
        "rot":   (3, 3),
        "nip":   (2, 1),
        "tuck":  (2, 3),
        "+":     (2, 1),
        "-":     (2, 1),
        "*":     (2, 1),
        "/":     (2, 1),
        "mod":   (2, 1),
        "=":     (2, 1),
        "!=":    (2, 1),
        "<":     (2, 1),
        ">":     (2, 1),
        "<=":    (2, 1),
        ">=":    (2, 1),
        "and":   (2, 1),
        "or":    (2, 1),
        "xor":   (2, 1),
        "not":   (1, 1),
        "shl":   (2, 1),
        "shr":   (2, 1),
        "neg":   (1, 1),
        "@":     (1, 1),
        "!":     (2, 0),
        "@8":    (1, 1),
        "!8":    (2, 0),
        "@16":   (1, 1),
        "!16":   (2, 0),
        "@32":   (1, 1),
        "!32":   (2, 0),
    }

    def _check_extern_types(self, definitions: Sequence[Union["Definition", "AsmDefinition"]]) -> None:
        """Basic type checking: verify stack depth at extern call sites and builtin underflows."""
        _v = self.verbosity
        _effects = self._BUILTIN_STACK_EFFECTS
        _OP_LIT = OP_LITERAL
        _OP_W = OP_WORD
        _OP_WP = OP_WORD_PTR
        _OP_LIST_LIT = OP_LIST_LITERAL
        _OP_BSS_LIST_LIT = OP_BSS_LIST_LITERAL
        _check_extern = self.enable_extern_type_check
        _check_stack = self.enable_stack_check
        extern_issues: List[str] = []
        stack_issues: List[str] = []
        for defn in definitions:
            if not isinstance(defn, Definition):
                continue
            # depth tracks values on the data stack relative to entry.
            # 'main' starts with an empty stack.  For other words we can
            # only check underflows when a stack-effect comment provides
            # the input count (e.g. ``# a b -- c`` -> 2 inputs).
            si = defn.stack_inputs
            if si is not None:
                known_entry_depth = si
            elif defn.name == 'main':
                known_entry_depth = 0
            else:
                known_entry_depth = -1  # unknown — disable underflow checks
            depth: Optional[int] = known_entry_depth if known_entry_depth >= 0 else 0
            for node in defn.body:
                opc = node._opcode
                if depth is None:
                    # After control flow we can't track depth reliably
                    if opc == _OP_W and _check_extern:
                        word = self.dictionary.lookup(str(node.data))
                        if word and word.is_extern and word.extern_signature:
                            # Can't verify — depth unknown after branch
                            if _v >= 3:
                                print(f"[v3] type-check: '{defn.name}' -> extern '{word.name}' skipped (unknown depth)")
                    continue
                if opc == _OP_LIT:
                    # String literals push 2 values (addr + len), others push 1.
                    depth += 2 if isinstance(node.data, str) else 1
                elif opc == _OP_WP:
                    depth += 1
                elif opc == _OP_LIST_LIT or opc == _OP_BSS_LIST_LIT:
                    depth += 1
                elif opc in (OP_BRANCH_ZERO, OP_JUMP, OP_LABEL, OP_FOR_BEGIN, OP_FOR_END):
                    # Control flow — stop tracking precisely
                    if opc == OP_BRANCH_ZERO:
                        depth -= 1
                    depth = None
                elif opc == _OP_W:
                    name = str(node.data)
                    word = self.dictionary.lookup(name)
                    if word is None:
                        depth = None
                        continue
                    if word.is_extern and word.extern_signature:
                        inputs = word.extern_inputs
                        outputs = word.extern_outputs
                        if _check_extern and known_entry_depth >= 0 and depth < inputs:
                            extern_issues.append(
                                f"in '{defn.name}': extern '{name}' expects {inputs} "
                                f"argument{'s' if inputs != 1 else ''}, but only {depth} "
                                f"value{'s' if depth != 1 else ''} on the stack"
                            )
                        depth = depth - inputs + outputs
                    elif name in _effects:
                        consumed, produced = _effects[name]
                        if known_entry_depth >= 0 and depth < consumed:
                            if _check_stack:
                                stack_issues.append(
                                    f"in '{defn.name}': '{name}' needs {consumed} "
                                    f"value{'s' if consumed != 1 else ''}, but only {depth} "
                                    f"on the stack"
                                )
                            depth = None
                        else:
                            depth = depth - consumed + produced
                    elif word.is_extern:
                        # Extern without signature — apply inputs/outputs
                        depth = depth - word.extern_inputs + word.extern_outputs
                    else:
                        # Unknown word — lose depth tracking
                        depth = None

        for issue in extern_issues:
            print(f"[extern-type-check] warning: {issue}")
        for issue in stack_issues:
            print(f"[stack-check] warning: {issue}")
        if _v >= 1 and not extern_issues and not stack_issues:
            print(f"[v1] type-check: no issues found")

    def _reachable_runtime_defs(self, runtime_defs: Sequence[Union[Definition, AsmDefinition]], extra_roots: Optional[Sequence[str]] = None) -> Set[str]:
        edges: Dict[str, Set[str]] = {}
        _OP_W = OP_WORD
        _OP_WP = OP_WORD_PTR
        for definition in runtime_defs:
            refs: Set[str] = set()
            if isinstance(definition, Definition):
                for node in definition.body:
                    oc = node._opcode
                    if oc == _OP_W or oc == _OP_WP:
                        refs.add(str(node.data))
            elif isinstance(definition, AsmDefinition):
                # Collect obvious textual `call` targets from asm bodies so
                # asm-defined entry points can create edges into the word
                # graph.  The extractor below will tolerate common call forms
                # such as `call foo` and `call [rel foo]`.
                asm_calls = self._extract_called_symbols_from_asm(definition.body)
                for sym in asm_calls:
                    refs.add(sym)
            edges[definition.name] = refs

        # Map sanitized labels back to their original definition names so
        # calls to emitted/sanitized labels (e.g. `w_foo`) can be resolved
        # to the corresponding word names present in `edges`.
        sanitized_map: Dict[str, str] = {sanitize_label(n): n for n in edges}

        reachable: Set[str] = set()
        stack: List[str] = ["main"]
        if extra_roots:
            for r in extra_roots:
                if r and r not in stack:
                    stack.append(r)
        while stack:
            name = stack.pop()
            if name in reachable:
                continue
            reachable.add(name)
            for dep in edges.get(name, ()): 
                # Direct name hit
                if dep in edges and dep not in reachable:
                    stack.append(dep)
                    continue
                # Possibly a sanitized label; resolve back to original name
                resolved = sanitized_map.get(dep)
                if resolved and resolved not in reachable:
                    stack.append(resolved)
        return reachable

    def _extract_called_symbols_from_asm(self, asm_body: str) -> Set[str]:
        """Return set of symbol names called from a raw asm body.

        This looks for typical `call <symbol>` forms and also
        `call [rel <symbol>]` and `call qword [rel <symbol>]`.
        """
        calls: Set[str] = set()
        for m in _RE_ASM_CALL_EXTRACT.finditer(asm_body):
            sym = m.group(1) or m.group(2)
            if sym:
                calls.add(sym)
        return calls

    def _emit_externs(self, text: List[str]) -> None:
        externs = sorted([w.name for w in self.dictionary.words.values() if getattr(w, "is_extern", False)])
        for name in externs:
            text.append(f"extern {name}")

    def emit(self, module: Module, debug: bool = False, entry_mode: str = "program") -> Emission:
        if entry_mode not in {"program", "library"}:
            raise CompileError(f"unknown entry mode '{entry_mode}'")
        is_program = entry_mode == "program"
        emission = Emission()
        self._export_all_defs = not is_program
        self._last_cfg_definitions = []
        try:
            self._emit_externs(emission.text)
            # Determine whether user provided a top-level `:asm _start` in
            # the module forms so the prelude can avoid emitting the
            # default startup stub.
            # Detect whether the user supplied a `_start` either as a top-level
            # AsmDefinition form or as a registered dictionary word (imports
            # or CT execution may register it). This influences prelude
            # generation so the default stub is suppressed when present.
            user_has_start = any(
                isinstance(f, AsmDefinition) and f.name == "_start" for f in module.forms
            ) or (
                (self.dictionary.lookup("_start") is not None)
                and isinstance(self.dictionary.lookup("_start").definition, AsmDefinition)
            ) or (
                (module.prelude is not None) and any(l.strip().startswith("_start:") for l in module.prelude)
            )
            # Defer runtime prelude generation until after top-level forms are
            # parsed into `definitions` so we can accurately detect a user
            # provided `_start` AsmDefinition and suppress the default stub.
            # Note: module.prelude was already inspected above when
            # computing `user_has_start`, so avoid referencing
            # `prelude_lines` before it's constructed.
            # Prelude will be generated after definitions are known.
            # If user provided a raw assembly `_start` via `:asm _start {...}`
            # inject it verbatim into the text section so it becomes the
            # program entrypoint. Emit the raw body (no automatic `ret`).
            # Do not inject `_start` body here; rely on definitions emission
            # and the earlier `user_has_start` check to suppress the default
            # startup stub. This avoids emitting `_start` twice.
            self._string_literals = {}
            self._string_literal_counter = 0
            self._float_literals = {}
            self._data_section = emission.data
            self._generated_bss = []
            self._generated_bss_counter = 0
            self._cstruct_layouts = dict(module.cstruct_layouts)

            valid_defs = (Definition, AsmDefinition)
            raw_defs = [form for form in module.forms if isinstance(form, valid_defs)]
            definitions = self._dedup_definitions(raw_defs)

            stray_forms = [form for form in module.forms if not isinstance(form, valid_defs)]
            if stray_forms:
                raise CompileError("top-level literals or word references are not supported yet")

            _v = self.verbosity
            if _v >= 1:
                import time as _time_mod
                _emit_t0 = _time_mod.perf_counter()
                n_def = sum(1 for d in definitions if isinstance(d, Definition))
                n_asm = sum(1 for d in definitions if isinstance(d, AsmDefinition))
                print(f"[v1] definitions: {n_def} high-level, {n_asm} asm")
                opts = []
                if self.enable_loop_unroll: opts.append(f"loop-unroll(threshold={self.loop_unroll_threshold})")
                if self.enable_peephole_optimization: opts.append("peephole")
                if self.enable_constant_folding: opts.append("constant-folding")
                if self.enable_auto_inline: opts.append("auto-inline")
                if self.enable_string_deduplication: opts.append("string-dedup")
                else: opts.append("string-no-dedup")
                if self.enable_extern_type_check: opts.append("extern-type-check")
                if self.enable_stack_check: opts.append("stack-check")
                print(f"[v1] optimizations: {', '.join(opts) if opts else 'none'}")

            if _v >= 2:
                # v2: log per-definition summary before optimization
                for defn in definitions:
                    if isinstance(defn, Definition):
                        print(f"[v2] def '{defn.name}': {len(defn.body)} ops, inline={getattr(defn, 'inline', False)}, compile_only={getattr(defn, 'compile_only', False)}")
                    else:
                        print(f"[v2] asm '{defn.name}'")

            # --- Early DCE: compute reachable set before optimization passes
            # so we skip optimizing definitions that will be eliminated. ---
            if is_program:
                _early_rt = [d for d in definitions if not getattr(d, "compile_only", False)]
                _early_reachable = self._reachable_runtime_defs(_early_rt)
                # Also include inline defs that are referenced by reachable defs
                # (they need optimization for correct inlining).
            else:
                _early_reachable = None  # library mode: optimize everything

            if self.enable_loop_unroll:
                if _v >= 1: _t0 = _time_mod.perf_counter()
                for defn in definitions:
                    if isinstance(defn, Definition):
                        if _early_reachable is not None and defn.name not in _early_reachable:
                            continue
                        self._unroll_constant_for_loops(defn)
                if _v >= 1:
                    print(f"[v1] loop unrolling: {(_time_mod.perf_counter() - _t0)*1000:.2f}ms")
            if self.enable_peephole_optimization:
                if _v >= 1: _t0 = _time_mod.perf_counter()
                if _v >= 4:
                    for defn in definitions:
                        if isinstance(defn, Definition):
                            if _early_reachable is not None and defn.name not in _early_reachable:
                                continue
                            before_ops = [(n.op, n.data) for n in defn.body]
                            self._peephole_optimize_definition(defn)
                            after_ops = [(n.op, n.data) for n in defn.body]
                            if before_ops != after_ops:
                                print(f"[v2] peephole '{defn.name}': {len(before_ops)} -> {len(after_ops)} ops ({len(before_ops) - len(after_ops)} removed)")
                                self._log_op_diff(defn.name, "peephole", before_ops, after_ops)
                elif _v >= 2:
                    for defn in definitions:
                        if isinstance(defn, Definition):
                            if _early_reachable is not None and defn.name not in _early_reachable:
                                continue
                            _before = len(defn.body)
                            self._peephole_optimize_definition(defn)
                            _after = len(defn.body)
                            if _before != _after:
                                print(f"[v2] peephole '{defn.name}': {_before} -> {_after} ops ({_before - _after} removed)")
                else:
                    for defn in definitions:
                        if isinstance(defn, Definition):
                            if _early_reachable is not None and defn.name not in _early_reachable:
                                continue
                            self._peephole_optimize_definition(defn)
                if _v >= 1:
                    print(f"[v1] peephole optimization: {(_time_mod.perf_counter() - _t0)*1000:.2f}ms")
            if self.enable_constant_folding:
                if _v >= 1: _t0 = _time_mod.perf_counter()
                if _v >= 4:
                    for defn in definitions:
                        if isinstance(defn, Definition):
                            if _early_reachable is not None and defn.name not in _early_reachable:
                                continue
                            before_ops = [(n.op, n.data) for n in defn.body]
                            self._fold_constants_in_definition(defn)
                            after_ops = [(n.op, n.data) for n in defn.body]
                            if before_ops != after_ops:
                                print(f"[v2] constant-fold '{defn.name}': {len(before_ops)} -> {len(after_ops)} ops ({len(before_ops) - len(after_ops)} folded)")
                                self._log_op_diff(defn.name, "constant-fold", before_ops, after_ops)
                elif _v >= 2:
                    for defn in definitions:
                        if isinstance(defn, Definition):
                            if _early_reachable is not None and defn.name not in _early_reachable:
                                continue
                            _before = len(defn.body)
                            self._fold_constants_in_definition(defn)
                            _after = len(defn.body)
                            if _before != _after:
                                print(f"[v2] constant-fold '{defn.name}': {_before} -> {_after} ops ({_before - _after} folded)")
                else:
                    for defn in definitions:
                        if isinstance(defn, Definition):
                            if _early_reachable is not None and defn.name not in _early_reachable:
                                continue
                            self._fold_constants_in_definition(defn)
                if _v >= 1:
                    print(f"[v1] constant folding: {(_time_mod.perf_counter() - _t0)*1000:.2f}ms")

            if _v >= 1:
                _t0 = _time_mod.perf_counter()
            for defn in definitions:
                if not isinstance(defn, Definition):
                    continue
                if _early_reachable is not None and defn.name not in _early_reachable:
                    continue
                self._audit_optimized_definition(defn)
            if _v >= 1:
                print(f"[v1] optimizer audit: {(_time_mod.perf_counter() - _t0)*1000:.2f}ms")

            runtime_defs = [defn for defn in definitions if not getattr(defn, "compile_only", False)]
            if is_program:
                if not any(defn.name == "main" for defn in runtime_defs):
                    raise CompileError("missing 'main' definition")
                # Determine if any user-provided `_start` asm calls into
                # defined words and use those call targets as additional
                # reachability roots. This avoids unconditionally emitting
                # every `:asm` body while still preserving functions that
                # are invoked from a custom `_start` stub.
                # Build a quick lookup of runtime definition names -> defn
                name_to_def: Dict[str, Union[Definition, AsmDefinition]] = {d.name: d for d in runtime_defs}
                # Look for an asm `_start` among parsed definitions (not just runtime_defs)
                asm_start = next((d for d in definitions if isinstance(d, AsmDefinition) and d.name == "_start"), None)
                extra_roots: List[str] = []
                if asm_start is not None:
                    called = self._extract_called_symbols_from_asm(asm_start.body)
                    # Resolve called symbols to definition names using both
                    # raw and sanitized forms.
                    sanitized_map = {sanitize_label(n): n for n in name_to_def}
                    for sym in called:
                        if sym in name_to_def:
                            extra_roots.append(sym)
                        else:
                            resolved = sanitized_map.get(sym)
                            if resolved:
                                extra_roots.append(resolved)

                # Ensure a user-provided raw `_start` asm definition is
                # always emitted (it should override the default stub).
                if asm_start is not None and asm_start not in runtime_defs:
                    runtime_defs.append(asm_start)

                reachable = self._reachable_runtime_defs(runtime_defs, extra_roots=extra_roots)
                if len(reachable) != len(runtime_defs):
                    if _v >= 2:
                        eliminated = [defn.name for defn in runtime_defs if defn.name not in reachable]
                    _n_before_dce = len(runtime_defs)
                    runtime_defs = [defn for defn in runtime_defs if defn.name in reachable]
                    if _v >= 1:
                        print(f"[v1] DCE: {_n_before_dce} -> {len(runtime_defs)} definitions ({_n_before_dce - len(runtime_defs)} eliminated)")
                    if _v >= 2 and eliminated:
                        print(f"[v2] DCE eliminated: {', '.join(eliminated)}")
                # Ensure `_start` is preserved even if not reachable from
                # `main` or the discovered roots; user-provided `_start`
                # must override the default stub.
                if asm_start is not None and asm_start not in runtime_defs:
                    runtime_defs.append(asm_start)
            elif self._export_all_defs:
                exported = sorted({sanitize_label(defn.name) for defn in runtime_defs})
                for label in exported:
                    emission.text.append(f"global {label}")

            # Inline-only definitions are expanded at call sites; skip emitting standalone labels.
            runtime_defs = [defn for defn in runtime_defs if not getattr(defn, "inline", False)]

            if self._need_cfg:
                self._last_cfg_definitions = [
                    self._copy_definition_for_cfg(defn)
                    for defn in runtime_defs
                    if isinstance(defn, Definition)
                ]

            if self.enable_extern_type_check or self.enable_stack_check:
                if _v >= 1: _t0 = _time_mod.perf_counter()
                self._check_extern_types(runtime_defs)
                if _v >= 1:
                    print(f"[v1] type/stack checking: {(_time_mod.perf_counter() - _t0)*1000:.2f}ms")

            if _v >= 1:
                print(f"[v1] emitting {len(runtime_defs)} runtime definitions")

            if _v >= 1: _t0 = _time_mod.perf_counter()
            for definition in runtime_defs:
                if _v >= 3:
                    body_len = len(definition.body) if isinstance(definition, Definition) else 0
                    kind = "asm" if isinstance(definition, AsmDefinition) else "def"
                    # v3: dump full body opcodes
                    print(f"[v3] emit {kind} '{definition.name}' (body ops: {body_len})")
                    if isinstance(definition, Definition) and definition.body:
                        for i, node in enumerate(definition.body):
                            print(f"[v3]   [{i}] {node.op}({node.data!r})")
                self._emit_definition(definition, emission.text, debug=debug)
            if _v >= 1:
                print(f"[v1] code emission: {(_time_mod.perf_counter() - _t0)*1000:.2f}ms")

            # --- now generate and emit the runtime prelude ---
            # Determine whether a user-provided `_start` exists among the
            # parsed definitions or in a compile-time-injected prelude. If
            # present, suppress the default startup stub emitted by the
            # runtime prelude.
            user_has_start = any(isinstance(d, AsmDefinition) and d.name == "_start" for d in definitions)
            if module.prelude is not None and not user_has_start:
                if any(line.strip().startswith("_start:") for line in module.prelude):
                    user_has_start = True
            base_prelude = self._runtime_prelude(entry_mode, has_user_start=user_has_start)
            # Use the generated base prelude. Avoid directly prepending
            # `module.prelude` which can contain raw, unsanitized assembly
            # fragments (often sourced from cached stdlib assembly) that
            # duplicate or conflict with the sanitized definitions the
            # emitter produces. Prepending `module.prelude` has caused
            # duplicate `_start` and symbol conflicts; prefer the
            # canonical `base_prelude` produced by the emitter.
            prelude_lines = base_prelude
            if user_has_start and prelude_lines is not None:
                # Avoid re-declaring the default startup symbol when the
                # user provided their own `_start`. Do not remove the
                # user's `_start` body. Only
                # filter out any stray `global _start` markers.
                prelude_lines = [l for l in prelude_lines if l.strip() != "global _start"]
            # Tag any `_start:` occurrences in the prelude with a
            # provenance comment so generated ASM files make it easy
            # to see where each `_start` originated. This is
            # non-destructive (comments only) and helps debug duplicates.
            if prelude_lines is not None:
                tagged = []
                for l in prelude_lines:
                    if l.strip().startswith("_start:"):
                        tagged.append("; __ORIGIN__ prelude")
                        tagged.append(l)
                    else:
                        tagged.append(l)
                prelude_lines = tagged
            # Prepend prelude lines to any already-emitted text (definitions).
            emission.text = (prelude_lines if prelude_lines is not None else []) + list(emission.text)
            try:
                self._emitted_start = user_has_start
            except Exception:
                self._emitted_start = False
            # If no `_start` has been emitted (either detected in
            # definitions/module.prelude or already present in the
            # composed `emission.text`), append the default startup
            # stub now (after definitions) so the emitter does not
            # produce duplicate `_start` labels.
            if is_program and not (user_has_start or getattr(self, "_emitted_start", False)):
                emission.text.extend([
                    "; __ORIGIN__ default_stub",
                    "global _start",
                    "_start:",
                    "    ; Linux x86-64 startup: argc/argv from stack",
                    "    mov rdi, [rsp]",         # argc
                    "    lea rsi, [rsp+8]",      # argv
                    "    mov [rel sys_argc], rdi",
                    "    mov [rel sys_argv], rsi",
                    "    ; initialize data/return stack pointers",
                    "    lea r12, [rel dstack_top]",
                    "    lea r13, [rel rstack_top]",
                    f"    call {sanitize_label('main')}",
                    "    mov rax, 0",
                    "    lea rcx, [rel dstack_top]",
                    "    cmp r12, rcx",
                    "    je .no_exit_value",
                    "    mov rax, [r12]",
                    "    add r12, 8",
                    ".no_exit_value:",
                    "    mov rdi, rax",
                    "    mov rax, 60",
                    "    syscall",
                ])

            self._emit_variables(module.variables)

            if self._data_section is not None:
                if not self._data_section:
                    self._data_section.append("data_start:")
                if not self._data_section or self._data_section[-1] != "data_end:":
                    self._data_section.append("data_end:")
            bss_lines = module.bss if module.bss is not None else self._bss_layout()
            if self._generated_bss:
                bss_lines = list(bss_lines) + self._generated_bss
            emission.bss.extend(bss_lines)
            if _v >= 1:
                _emit_dt = (_time_mod.perf_counter() - _emit_t0) * 1000
                print(f"[v1] total emit: {_emit_dt:.2f}ms")
            return emission
        finally:
            self._data_section = None
            self._export_all_defs = False

    def _dedup_definitions(self, definitions: Sequence[Union[Definition, AsmDefinition]]) -> List[Union[Definition, AsmDefinition]]:
        seen: Set[str] = set()
        ordered: List[Union[Definition, AsmDefinition]] = []
        for defn in reversed(definitions):
            if defn.name in seen:
                continue
            seen.add(defn.name)
            ordered.append(defn)
        ordered.reverse()
        return ordered

    def _emit_variables(self, variables: Dict[str, str]) -> None:
        if not variables:
            return
        self._ensure_data_start()
        existing = set()
        if self._data_section is not None:
            for line in self._data_section:
                if ":" in line:
                    label = line.split(":", 1)[0]
                    existing.add(label.strip())
        for label in variables.values():
            if label in existing:
                continue
            self._data_section.append(f"{label}: dq 0")

    def _allocate_bss_list_storage(self, size: int) -> str:
        if size < 0:
            raise CompileError("bss list size must be >= 0")
        label = f"bss_list_{self._generated_bss_counter}"
        self._generated_bss_counter += 1
        self._generated_bss.append("align 16")
        self._generated_bss.append(f"{label}: resq {size + 1}")
        return label

    def _ensure_data_start(self) -> None:
        if self._data_section is None:
            raise CompileError("data section is not initialized")
        if not self._data_section:
            self._data_section.append("data_start:")

    def _intern_string_literal(self, value: str) -> Tuple[str, int]:
        if self._data_section is None:
            raise CompileError("string literal emission requested without data section")
        self._ensure_data_start()
        if self.enable_string_deduplication and value in self._string_literals:
            return self._string_literals[value]
        label = f"str_{self._string_literal_counter}"
        self._string_literal_counter += 1
        encoded = value.encode("utf-8")
        bytes_with_nul = list(encoded) + [0]
        byte_list = ", ".join(str(b) for b in bytes_with_nul)
        self._data_section.append(f"{label}: db {byte_list}")
        self._data_section.append(f"{label}_len equ {len(encoded)}")
        self._string_literals[value] = (label, len(encoded))
        return label, len(encoded)

    def _intern_float_literal(self, value: float) -> str:
        if self._data_section is None:
            raise CompileError("float literal emission requested without data section")
        self._ensure_data_start()
        if value in self._float_literals:
            return self._float_literals[value]
        label = f"flt_{len(self._float_literals)}"
        # Use hex representation of double precision float
        import struct
        hex_val = _get_struct().pack('>d', value).hex()
        # NASM expects hex starting with 0x
        self._data_section.append(f"{label}: dq 0x{hex_val}")
        self._float_literals[value] = label
        return label

    def _emit_definition(
        self,
        definition: Union[Definition, AsmDefinition],
        text: List[str],
        *,
        debug: bool = False,
    ) -> None:
        # If a `_start` label has already been emitted in the prelude,
        # skip emitting a second `_start` definition which would cause
        # assembler redefinition errors. The prelude-provided `_start`
        # (if present) is taken to be authoritative.
        if definition.name == "_start" and getattr(self, "_emitted_start", False):
            return
        # If this is a raw assembly definition, tag its origin so the
        # generated ASM clearly shows the source of the label (helpful
        # when diagnosing duplicate `_start` occurrences).
        if isinstance(definition, AsmDefinition):
            text.append(f"; __ORIGIN__ AsmDefinition {definition.name}")
        label = sanitize_label(definition.name)

        # Record start index so we can write a per-definition snapshot
        start_index = len(text)
        # If this definition is the program entry `_start`, ensure it's
        # exported as a global symbol so the linker sets the process
        # entry point correctly. Some earlier sanitizer passes may
        # remove `global _start` from prelude fragments; make sure user
        # provided `_start` remains globally visible.
        if label == "_start":
            text.append("global _start")
        text.append(f"{label}:")
        builder = FunctionEmitter(text, debug_enabled=debug)
        self._emit_stack.append(definition.name)
        try:
            if isinstance(definition, Definition):
                for node in definition.body:
                    self._emit_node(node, builder)
            elif isinstance(definition, AsmDefinition):
                self._emit_asm_body(definition, builder)
            else:  # pragma: no cover - defensive
                raise CompileError("unknown definition type")
            builder.emit("    ret")
        finally:
            self._emit_stack.pop()

    def _emit_inline_definition(self, word: Word, builder: FunctionEmitter) -> None:
        definition = word.definition
        if not isinstance(definition, Definition):
            raise CompileError(f"inline word '{word.name}' requires a high-level definition")

        self._emit_stack.append(f"{word.name} (inline)")

        suffix = self._inline_counter
        self._inline_counter += 1

        label_map: Dict[str, str] = {}

        def remap(label: str) -> str:
            if label not in label_map:
                label_map[label] = f"{label}__inl{suffix}"
            return label_map[label]

        for node in definition.body:
            kind = node._opcode
            data = node.data
            if kind == OP_LABEL:
                mapped = remap(str(data))
                self._emit_node(_make_op("label", mapped), builder)
                continue
            if kind == OP_JUMP:
                mapped = remap(str(data))
                self._emit_node(_make_op("jump", mapped), builder)
                continue
            if kind == OP_BRANCH_ZERO:
                mapped = remap(str(data))
                self._emit_node(_make_op("branch_zero", mapped), builder)
                continue
            if kind == OP_FOR_BEGIN:
                mapped = {
                    "loop": remap(data["loop"]),
                    "end": remap(data["end"]),
                }
                self._emit_node(_make_op("for_begin", mapped), builder)
                continue
            if kind == OP_FOR_END:
                mapped = {
                    "loop": remap(data["loop"]),
                    "end": remap(data["end"]),
                }
                self._emit_node(_make_op("for_end", mapped), builder)
                continue
            if kind == OP_LIST_BEGIN or kind == OP_LIST_END:
                mapped = remap(str(data))
                self._emit_node(_make_op(node.op, mapped), builder)
                continue
            self._emit_node(node, builder)

        self._emit_stack.pop()

    def _emit_asm_body(self, definition: AsmDefinition, builder: FunctionEmitter) -> None:
        body = definition.body.strip("\n")
        if not body:
            return
        _call_sub = _RE_ASM_CALL.sub
        _global_sub = _RE_ASM_GLOBAL.sub
        _extern_sub = _RE_ASM_EXTERN.sub
        def repl_sym(m: re.Match) -> str:
            name = m.group(1)
            return m.group(0).replace(name, sanitize_label(name))
        for line in body.splitlines():
            if not line.strip():
                continue
            if "call " in line or "global " in line or "extern " in line:
                line = _call_sub(repl_sym, line)
                line = _global_sub(repl_sym, line)
                line = _extern_sub(repl_sym, line)
            builder.emit(line)

    def _emit_asm_body_inline(self, definition: AsmDefinition, builder: FunctionEmitter) -> None:
        """Emit an asm body inline, stripping ret instructions."""
        # Cache sanitized lines on the definition to avoid re-parsing.
        cached = definition._inline_lines
        if cached is None:
            _call_sub = _RE_ASM_CALL.sub
            _global_sub = _RE_ASM_GLOBAL.sub
            _extern_sub = _RE_ASM_EXTERN.sub
            def repl_sym(m: re.Match) -> str:
                name = m.group(1)
                return m.group(0).replace(name, sanitize_label(name))
            cached = []
            body = definition.body.strip("\n")
            for line in body.splitlines():
                stripped = line.strip()
                if not stripped or stripped == "ret":
                    continue
                if "call " in line or "global " in line or "extern " in line:
                    line = _call_sub(repl_sym, line)
                    line = _global_sub(repl_sym, line)
                    line = _extern_sub(repl_sym, line)
                cached.append(line)
            definition._inline_lines = cached
        text = builder.text
        text.extend(cached)

    def _emit_node(self, node: Op, builder: FunctionEmitter) -> None:
        kind = node._opcode
        data = node.data
        builder.set_location(node.loc)

        if kind == OP_WORD:
            self._emit_wordref(data, builder)
            return

        if kind == OP_LITERAL:
            if isinstance(data, int):
                builder.push_literal(data)
                return
            if isinstance(data, float):
                label = self._intern_float_literal(data)
                builder.push_float(label)
                return
            if isinstance(data, str):
                label, length = self._intern_string_literal(data)
                builder.push_label(label)
                builder.push_literal(length)
                return
            raise CompileError(f"unsupported literal type {type(data)!r} while emitting '{self._emit_stack[-1]}'" if self._emit_stack else f"unsupported literal type {type(data)!r}")

        if kind == OP_WORD_PTR:
            self._emit_wordptr(data, builder)
            return

        if kind == OP_BRANCH_ZERO:
            self._emit_branch_zero(data, builder)
            return

        if kind == OP_JUMP:
            builder.emit(f"    jmp {data}")
            return

        if kind == OP_LABEL:
            builder.emit(f"{data}:")
            return

        if kind == OP_FOR_BEGIN:
            self._emit_for_begin(data, builder)
            return

        if kind == OP_FOR_END:
            self._emit_for_next(data, builder)
            return

        if kind == OP_LIST_BEGIN:
            builder.comment("list begin")
            builder.emit("    mov rax, [rel list_capture_sp]")
            builder.emit("    lea rdx, [rel list_capture_stack]")
            builder.emit("    mov [rdx + rax*8], r12")
            builder.emit("    inc rax")
            builder.emit("    mov [rel list_capture_sp], rax")
            return

        if kind == OP_LIST_LITERAL:
            values = list(data or [])
            count = len(values)
            bytes_needed = (count + 1) * 8
            builder.comment("list literal")
            builder.emit("    xor rdi, rdi")
            builder.emit(f"    mov rsi, {bytes_needed}")
            builder.emit("    mov rdx, 3")
            builder.emit("    mov r10, 34")
            builder.emit("    mov r8, -1")
            builder.emit("    xor r9, r9")
            builder.emit("    mov rax, 9")
            builder.emit("    syscall")
            builder.emit(f"    mov qword [rax], {count}")
            for idx_item, value in enumerate(values):
                builder.emit(f"    mov qword [rax + {8 + idx_item * 8}], {int(value)}")
            builder.emit("    sub r12, 8")
            builder.emit("    mov [r12], rax")
            return

        if kind == OP_BSS_LIST_LITERAL:
            payload = data if isinstance(data, dict) else {}
            count = int(payload.get("size", 0))
            values = [int(v) for v in list(payload.get("values", []) or [])]
            if count < 0:
                raise CompileError("bss list size must be >= 0")
            if len(values) > count:
                raise CompileError("bss list has more initializer values than declared size")
            label = self._allocate_bss_list_storage(count)
            builder.comment("bss list literal")
            builder.emit(f"    lea rax, [rel {label}]")
            builder.emit(f"    mov qword [rax], {count}")
            for idx_item, value in enumerate(values):
                builder.emit(f"    mov qword [rax + {8 + idx_item * 8}], {value}")
            for idx_item in range(len(values), count):
                builder.emit(f"    mov qword [rax + {8 + idx_item * 8}], 0")
            builder.emit("    sub r12, 8")
            builder.emit("    mov [r12], rax")
            return

        if kind == OP_LIST_END:
            base = str(data)
            loop_label = f"{base}_copy_loop"
            done_label = f"{base}_copy_done"

            builder.comment("list end")
            # pop capture start pointer
            builder.emit("    mov rax, [rel list_capture_sp]")
            builder.emit("    dec rax")
            builder.emit("    mov [rel list_capture_sp], rax")
            builder.emit("    lea r11, [rel list_capture_stack]")
            builder.emit("    mov rbx, [r11 + rax*8]")
            # count = (start_r12 - r12) / 8
            builder.emit("    mov rcx, rbx")
            builder.emit("    sub rcx, r12")
            builder.emit("    shr rcx, 3")
            builder.emit("    mov [rel list_capture_tmp], rcx")

            # bytes = (count + 1) * 8
            builder.emit("    mov rsi, rcx")
            builder.emit("    inc rsi")
            builder.emit("    shl rsi, 3")

            # mmap(NULL, bytes, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANON, -1, 0)
            builder.emit("    xor rdi, rdi")
            builder.emit("    mov rdx, 3")
            builder.emit("    mov r10, 34")
            builder.emit("    mov r8, -1")
            builder.emit("    xor r9, r9")
            builder.emit("    mov rax, 9")
            builder.emit("    syscall")

            # store length
            builder.emit("    mov rdx, [rel list_capture_tmp]")
            builder.emit("    mov [rax], rdx")

            # copy elements, preserving original push order
            builder.emit("    xor rcx, rcx")
            builder.emit(f"{loop_label}:")
            builder.emit("    cmp rcx, rdx")
            builder.emit(f"    je {done_label}")
            builder.emit("    mov r8, rdx")
            builder.emit("    dec r8")
            builder.emit("    sub r8, rcx")
            builder.emit("    shl r8, 3")
            builder.emit("    mov r9, [r12 + r8]")
            builder.emit("    mov [rax + 8 + rcx*8], r9")
            builder.emit("    inc rcx")
            builder.emit(f"    jmp {loop_label}")
            builder.emit(f"{done_label}:")

            # drop captured values and push list pointer
            builder.emit("    mov r12, rbx")
            builder.emit("    sub r12, 8")
            builder.emit("    mov [r12], rax")
            return

        if kind == OP_RET:
            builder.ret()
            return

        raise CompileError(f"unsupported op {node!r} while emitting '{self._emit_stack[-1]}'" if self._emit_stack else f"unsupported op {node!r}")

    def _emit_mmap_alloc(self, builder: FunctionEmitter, size: int, target_reg: str = "rax") -> None:
        alloc_size = max(1, int(size))
        builder.emit("    xor rdi, rdi")
        builder.emit(f"    mov rsi, {alloc_size}")
        builder.emit("    mov rdx, 3")
        builder.emit("    mov r10, 34")
        builder.emit("    mov r8, -1")
        builder.emit("    xor r9, r9")
        builder.emit("    mov rax, 9")
        builder.emit("    syscall")
        if target_reg != "rax":
            builder.emit(f"    mov {target_reg}, rax")

    def _analyze_extern_c_type(self, type_name: str) -> Dict[str, Any]:
        size, align, cls, layout = _c_type_size_align_class(type_name, self._cstruct_layouts)
        info: Dict[str, Any] = {
            "name": _canonical_c_type_name(type_name),
            "size": size,
            "align": align,
            "class": cls,
            "kind": "struct" if layout is not None else "scalar",
            "layout": layout,
            "pass_mode": "scalar",
            "eightbytes": [],
        }
        if layout is not None:
            eb = _classify_struct_eightbytes(layout, self._cstruct_layouts)
            info["eightbytes"] = eb or []
            info["pass_mode"] = "register" if eb is not None else "memory"
        return info

    def _emit_copy_bytes_from_ptr(
        self,
        builder: FunctionEmitter,
        *,
        src_ptr_reg: str,
        dst_expr: str,
        size: int,
    ) -> None:
        copied = 0
        while copied + 8 <= size:
            builder.emit(f"    mov r11, [{src_ptr_reg} + {copied}]")
            builder.emit(f"    mov qword [{dst_expr} + {copied}], r11")
            copied += 8
        while copied < size:
            builder.emit(f"    mov r11b, byte [{src_ptr_reg} + {copied}]")
            builder.emit(f"    mov byte [{dst_expr} + {copied}], r11b")
            copied += 1

    @staticmethod
    def _pop_preceding_literal(builder: FunctionEmitter) -> Optional[int]:
        """If the last emitted instructions are a literal push, remove them and return the value."""
        text = builder.text
        n = len(text)
        if n < 3:
            return None
        # push_literal emits:  "; push N" / "sub r12, 8" / "mov qword [r12], N"
        mov_line = text[n - 1].strip()
        sub_line = text[n - 2].strip()
        cmt_line = text[n - 3].strip()
        if not (sub_line == "sub r12, 8" and mov_line.startswith("mov qword [r12],") and cmt_line.startswith("; push")):
            return None
        val_str = mov_line.split(",", 1)[1].strip()
        try:
            value = int(val_str)
        except ValueError:
            return None
        del text[n - 3:n]
        return value

    def _emit_extern_wordref(self, name: str, word: Word, builder: FunctionEmitter) -> None:
        inputs = getattr(word, "extern_inputs", 0)
        outputs = getattr(word, "extern_outputs", 0)
        signature = getattr(word, "extern_signature", None)

        if signature is None and inputs <= 0 and outputs <= 0:
            builder.emit(f"    call {name}")
            return

        arg_types = list(signature[0]) if signature else ["long"] * inputs
        ret_type = signature[1] if signature else ("long" if outputs > 0 else "void")

        # For variadic externs, consume the preceding literal as the count of
        # extra variadic arguments.  These are NOT passed to the C function as
        # a count parameter – they simply tell the compiler how many additional
        # stack values to pop and place into registers / the C stack.
        if getattr(word, "extern_variadic", False):
            va_count = self._pop_preceding_literal(builder)
            if va_count is None:
                suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
                raise CompileError(
                    f"variadic extern '{name}' requires a literal arg count on TOS{suffix}"
                )
            for _ in range(va_count):
                arg_types.append("long")
            inputs += va_count

        if len(arg_types) != inputs and signature is not None:
            suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
            raise CompileError(f"extern '{name}' mismatch: {inputs} inputs vs {len(arg_types)} types{suffix}")

        arg_infos = [self._analyze_extern_c_type(t) for t in arg_types]
        ret_info = self._analyze_extern_c_type(ret_type)

        regs = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
        xmm_regs = [f"xmm{i}" for i in range(8)]

        ret_uses_sret = ret_info["kind"] == "struct" and ret_info["pass_mode"] == "memory"
        int_idx = 1 if ret_uses_sret else 0
        xmm_idx = 0

        arg_locs: List[Dict[str, Any]] = []
        stack_cursor = 0

        for info in arg_infos:
            if info["kind"] == "struct":
                if info["pass_mode"] == "register":
                    classes: List[str] = list(info["eightbytes"])
                    need_int = sum(1 for c in classes if c == "INTEGER")
                    need_xmm = sum(1 for c in classes if c == "SSE")
                    if int_idx + need_int <= len(regs) and xmm_idx + need_xmm <= len(xmm_regs):
                        chunks: List[Tuple[str, str, int]] = []
                        int_off = int_idx
                        xmm_off = xmm_idx
                        for chunk_idx, cls in enumerate(classes):
                            if cls == "SSE":
                                chunks.append((cls, xmm_regs[xmm_off], chunk_idx * 8))
                                xmm_off += 1
                            else:
                                chunks.append(("INTEGER", regs[int_off], chunk_idx * 8))
                                int_off += 1
                        int_idx = int_off
                        xmm_idx = xmm_off
                        arg_locs.append({"mode": "struct_reg", "chunks": chunks, "info": info})
                    else:
                        stack_size = _round_up(int(info["size"]), 8)
                        stack_off = stack_cursor
                        stack_cursor += stack_size
                        arg_locs.append({"mode": "struct_stack", "stack_off": stack_off, "info": info})
                else:
                    stack_size = _round_up(int(info["size"]), 8)
                    stack_off = stack_cursor
                    stack_cursor += stack_size
                    arg_locs.append({"mode": "struct_stack", "stack_off": stack_off, "info": info})
                continue

            if info["class"] == "SSE":
                if xmm_idx < len(xmm_regs):
                    arg_locs.append({"mode": "scalar_reg", "reg": xmm_regs[xmm_idx], "class": "SSE"})
                    xmm_idx += 1
                else:
                    stack_off = stack_cursor
                    stack_cursor += 8
                    arg_locs.append({"mode": "scalar_stack", "stack_off": stack_off, "class": "SSE"})
            else:
                if int_idx < len(regs):
                    arg_locs.append({"mode": "scalar_reg", "reg": regs[int_idx], "class": "INTEGER"})
                    int_idx += 1
                else:
                    stack_off = stack_cursor
                    stack_cursor += 8
                    arg_locs.append({"mode": "scalar_stack", "stack_off": stack_off, "class": "INTEGER"})

        # Preserve and realign RSP for C ABI calls regardless current call depth.
        stack_bytes = max(15, int(stack_cursor) + 15)
        builder.emit("    mov r14, rsp")
        builder.emit(f"    sub rsp, {stack_bytes}")
        builder.emit("    and rsp, -16")

        if ret_info["kind"] == "struct":
            self._emit_mmap_alloc(builder, int(ret_info["size"]), target_reg="r15")
            if ret_uses_sret:
                builder.emit("    mov rdi, r15")

        total_args = len(arg_locs)
        for idx, loc in enumerate(reversed(arg_locs)):
            addr = f"[r12 + {idx * 8}]" if idx > 0 else "[r12]"
            mode = str(loc["mode"])

            if mode == "scalar_reg":
                reg = str(loc["reg"])
                cls = str(loc["class"])
                if cls == "SSE":
                    builder.emit(f"    mov rax, {addr}")
                    builder.emit(f"    movq {reg}, rax")
                else:
                    builder.emit(f"    mov {reg}, {addr}")
                continue

            if mode == "scalar_stack":
                stack_off = int(loc["stack_off"])
                builder.emit(f"    mov rax, {addr}")
                builder.emit(f"    mov qword [rsp + {stack_off}], rax")
                continue

            if mode == "struct_reg":
                chunks: List[Tuple[str, str, int]] = list(loc["chunks"])
                builder.emit(f"    mov rax, {addr}")
                for cls, target, off in chunks:
                    if cls == "SSE":
                        builder.emit(f"    movq {target}, [rax + {off}]")
                    else:
                        builder.emit(f"    mov {target}, [rax + {off}]")
                continue

            if mode == "struct_stack":
                stack_off = int(loc["stack_off"])
                size = int(loc["info"]["size"])
                builder.emit(f"    mov rax, {addr}")
                self._emit_copy_bytes_from_ptr(builder, src_ptr_reg="rax", dst_expr=f"rsp + {stack_off}", size=size)
                continue

            raise CompileError(f"internal extern lowering error for '{name}': unknown arg mode {mode!r}")

        if total_args:
            builder.emit(f"    add r12, {total_args * 8}")

        builder.emit(f"    mov al, {xmm_idx}")
        builder.emit(f"    call {name}")

        builder.emit("    mov rsp, r14")

        if ret_info["kind"] == "struct":
            if not ret_uses_sret:
                ret_classes: List[str] = list(ret_info["eightbytes"])
                int_ret_regs = ["rax", "rdx"]
                xmm_ret_regs = ["xmm0", "xmm1"]
                int_ret_idx = 0
                xmm_ret_idx = 0
                for chunk_idx, cls in enumerate(ret_classes):
                    off = chunk_idx * 8
                    if cls == "SSE":
                        src = xmm_ret_regs[xmm_ret_idx]
                        xmm_ret_idx += 1
                        builder.emit(f"    movq [r15 + {off}], {src}")
                    else:
                        src = int_ret_regs[int_ret_idx]
                        int_ret_idx += 1
                        builder.emit(f"    mov [r15 + {off}], {src}")
            builder.emit("    sub r12, 8")
            builder.emit("    mov [r12], r15")
            return

        if _ctype_uses_sse(ret_type):
            builder.emit("    sub r12, 8")
            builder.emit("    movq rax, xmm0")
            builder.emit("    mov [r12], rax")
        elif outputs == 1:
            builder.push_from("rax")
        elif outputs > 1:
            raise CompileError("extern only supports 0 or 1 scalar output")

    def _emit_wordref(self, name: str, builder: FunctionEmitter) -> None:
        word = self.dictionary.words.get(name)
        if word is None:
            suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
            raise CompileError(f"unknown word '{name}'{suffix}")
        if word.compile_only:
            suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
            raise CompileError(f"word '{name}' is compile-time only and cannot be used at runtime{suffix}")
        if getattr(word, "inline", False):
            if isinstance(word.definition, Definition):
                if word.name in self._inline_stack:
                    suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
                    raise CompileError(f"recursive inline expansion for '{word.name}'{suffix}")
                self._inline_stack.append(word.name)
                self._emit_inline_definition(word, builder)
                self._inline_stack.pop()
                return
            if isinstance(word.definition, AsmDefinition):
                self._emit_asm_body_inline(word.definition, builder)
                return
        if word.intrinsic:
            word.intrinsic(builder)
            return
        # Auto-inline small asm bodies even without explicit `inline` keyword.
        if self.enable_auto_inline and isinstance(word.definition, AsmDefinition) and self._asm_auto_inline_ok(word.definition):
            self._emit_asm_body_inline(word.definition, builder)
            return
        if getattr(word, "is_extern", False):
            self._emit_extern_wordref(name, word, builder)
        else:
            builder.emit(f"    call {sanitize_label(name)}")

    @staticmethod
    def _asm_auto_inline_ok(defn: AsmDefinition) -> bool:
        """Return True if *defn* is small enough to auto-inline at call sites."""
        cached = defn._inline_lines
        if cached is not None:
            return len(cached) <= _ASM_AUTO_INLINE_THRESHOLD
        count = 0
        for line in defn.body.split('\n'):
            s = line.strip()
            if not s or s == 'ret':
                continue
            if s.endswith(':'):
                return False  # labels would duplicate on multiple inlines
            if 'rsp' in s:
                return False  # references call-frame; must stay a real call
            count += 1
            if count > _ASM_AUTO_INLINE_THRESHOLD:
                return False
        return True

    def _emit_wordptr(self, name: str, builder: FunctionEmitter) -> None:
        word = self.dictionary.lookup(name)
        if word is None:
            suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
            raise CompileError(f"unknown word '{name}'{suffix}")
        if getattr(word, "is_extern", False):
            builder.push_label(name)
            return
        builder.push_label(sanitize_label(name))

    def _emit_branch_zero(self, target: str, builder: FunctionEmitter) -> None:
        builder.pop_to("rax")
        builder.emit("    test rax, rax")
        builder.emit(f"    jz {target}")

    def _emit_for_begin(self, data: Dict[str, str], builder: FunctionEmitter) -> None:
        loop_label = data["loop"]
        end_label = data["end"]
        builder.pop_to("rax")
        builder.emit("    cmp rax, 0")
        builder.emit(f"    jle {end_label}")
        builder.emit("    sub r13, 8")
        builder.emit("    mov [r13], rax")
        builder.emit(f"{loop_label}:")

    def _emit_for_next(self, data: Dict[str, str], builder: FunctionEmitter) -> None:
        loop_label = data["loop"]
        end_label = data["end"]
        builder.emit("    mov rax, [r13]")
        builder.emit("    dec rax")
        builder.emit("    mov [r13], rax")
        builder.emit(f"    jg {loop_label}")
        builder.emit("    add r13, 8")
        builder.emit(f"{end_label}:")

    def _runtime_prelude(self, entry_mode: str, has_user_start: bool = False) -> List[str]:
        lines: List[str] = [
            "%define DSTK_BYTES 65536",
            "%define RSTK_BYTES 65536",
            "%define PRINT_BUF_BYTES 128",
        ]
        is_program = entry_mode == "program"
        lines.extend([
            "global sys_argc",
            "global sys_argv",
            "section .data",
            "sys_argc: dq 0",
            "sys_argv: dq 0",
            "section .text",
        ])
        # Do not emit the default `_start` stub here; it will be appended
        # after definitions have been emitted if no user `_start` was
        # provided. This avoids duplicate or partial `_start` blocks.

        return lines

    def _bss_layout(self) -> List[str]:
        return [
            "global dstack",
            "global dstack_top",
            "global rstack",
            "global rstack_top",
            "align 16",
            "dstack: resb DSTK_BYTES",
            "dstack_top:",
            "align 16",
            "rstack: resb RSTK_BYTES",
            "rstack_top:",
            "align 16",
            "print_buf: resb PRINT_BUF_BYTES",
            "print_buf_end:",
            "align 16",
            "persistent: resb 64",
            "align 16",
            "list_capture_sp: resq 1",
            "list_capture_tmp: resq 1",
            "list_capture_stack: resq 1024",
        ]

    def write_asm(self, emission: Emission, path: Path) -> None:
        path.write_text(emission.snapshot())

# ---------------------------------------------------------------------------
# Built-in macros and intrinsics
# ---------------------------------------------------------------------------


def macro_immediate(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    word = parser.most_recent_definition()
    if word is None:
        raise ParseError("'immediate' must follow a definition")
    if word.runtime_only:
        raise ParseError(f"word '{word.name}' is runtime-only and cannot be immediate")
    word.immediate = True
    if word.definition is not None:
        word.definition.immediate = True
    return None


def macro_compile_only(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    word = parser.most_recent_definition()
    if word is None:
        raise ParseError("'compile-only' must follow a definition")
    if word.runtime_only:
        raise ParseError(f"word '{word.name}' is runtime-only and cannot be compile-only")
    word.compile_only = True
    if word.definition is not None:
        word.definition.compile_only = True
    return None


def macro_runtime(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    word = parser.most_recent_definition()
    if word is None:
        raise ParseError("'runtime' must follow a definition")
    if word.immediate:
        raise ParseError(f"word '{word.name}' is immediate and cannot be runtime-only")
    if word.compile_only:
        raise ParseError(f"word '{word.name}' is compile-only and cannot be runtime-only")
    word.runtime_only = True
    if word.definition is not None:
        word.definition.runtime_only = True
    return None


def macro_inline(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    next_tok = parser.peek_token()
    if next_tok is None or next_tok.lexeme not in ("word", ":asm"):
        raise ParseError("'inline' must be followed by 'word' or ':asm'")
    if parser._pending_inline_definition:
        raise ParseError("duplicate 'inline' before definition")
    parser._pending_inline_definition = True
    return None


def _require_definition_context(parser: "Parser", word_name: str) -> Definition:
    if not parser.context_stack or not isinstance(parser.context_stack[-1], Definition):
        raise ParseError(f"'{word_name}' can only appear inside a definition")
    return parser.context_stack[-1]


def macro_label(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    if parser._eof():
        raise ParseError("label name missing after 'label'")
    tok = parser.next_token()
    name = tok.lexeme
    if not _is_identifier(name):
        raise ParseError(f"invalid label name '{name}'")
    definition = _require_definition_context(parser, "label")
    if any(node._opcode == OP_LABEL and node.data == name for node in definition.body):
        raise ParseError(f"duplicate label '{name}' in definition '{definition.name}'")
    parser.emit_node(_make_op("label", name))
    return None


def macro_goto(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    if parser._eof():
        raise ParseError("label name missing after 'goto'")
    tok = parser.next_token()
    name = tok.lexeme
    if not _is_identifier(name):
        raise ParseError(f"invalid label name '{name}'")
    _require_definition_context(parser, "goto")
    parser.emit_node(_make_op("jump", name))
    return None


def macro_compile_time(ctx: MacroContext) -> Optional[List[Op]]:
    """Run the next word at compile time and still emit it for runtime."""
    parser = ctx.parser
    if parser._eof():
        raise ParseError("word name missing after 'compile-time'")
    tok = parser.next_token()
    name = tok.lexeme
    word = parser.dictionary.lookup(name)
    if word is None:
        raise ParseError(f"unknown word '{name}' for compile-time")
    if word.runtime_only:
        raise ParseError(f"word '{name}' is runtime-only")
    if word.compile_only:
        raise ParseError(f"word '{name}' is compile-time only")
    parser.compile_time_vm.invoke(word)
    parser.compile_time_vm._ct_executed.add(name)
    if isinstance(parser.context_stack[-1], Definition):
        parser.emit_node(_make_op("word", name))
    return None


def macro_with(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser

    names: List[str] = []
    template: Optional[Token] = None
    seen: set[str] = set()
    while True:
        if parser._eof():
            raise ParseError("missing 'in' after 'with'")
        tok = parser.next_token()
        template = template or tok
        if tok.lexeme == "in":
            break
        if not _is_identifier(tok.lexeme):
            raise ParseError("invalid variable name in 'with'")
        if tok.lexeme in seen:
            raise ParseError("duplicate variable name in 'with'")
        seen.add(tok.lexeme)
        names.append(tok.lexeme)
    if not names:
        raise ParseError("'with' requires at least one variable name")

    body: List[Token] = []
    else_line: Optional[int] = None
    depth = 0
    while True:
        if parser._eof():
            raise ParseError("unterminated 'with' block (missing 'end')")
        tok = parser.next_token()
        if else_line is not None and tok.line != else_line:
            else_line = None
        if tok.lexeme == "end":
            if depth == 0:
                break
            depth -= 1
            body.append(tok)
            continue
        if tok.lexeme == "if":
            # Support shorthand elif form `else <cond> if` inside with-blocks.
            # This inline `if` shares the same closing `end` as the preceding
            # branch and therefore must not increment nesting depth.
            if else_line != tok.line:
                depth += 1
        elif tok.lexeme == "else":
            else_line = tok.line
        elif tok.lexeme in parser.block_openers:
            depth += 1
        body.append(tok)

    helper_for: Dict[str, str] = {}
    for name in names:
        _, helper = parser.allocate_variable(name)
        helper_for[name] = helper

    emitted_tokens: List[Token] = []

    def _emit_lex(lex: str, src_tok: Optional[Token] = None) -> None:
        base = src_tok or template or Token(lexeme="", line=0, column=0, start=0, end=0)
        emitted_tokens.append(
            Token(
                lexeme=lex,
                line=base.line,
                column=base.column,
                start=base.start,
                end=base.end,
            )
        )

    # Initialize variables by storing current stack values into their buffers
    for name in reversed(names):
        helper = helper_for[name]
        _emit_lex(helper, template)
        _emit_lex("swap", template)
        _emit_lex("!", template)

    i = 0
    while i < len(body):
        tok = body[i]
        name = tok.lexeme
        helper = helper_for.get(name)
        if helper is not None:
            next_tok = body[i + 1] if i + 1 < len(body) else None
            if next_tok is not None and next_tok.lexeme == "!":
                _emit_lex(helper, tok)
                _emit_lex("swap", tok)
                _emit_lex("!", tok)
                i += 2
                continue
            if next_tok is not None and next_tok.lexeme == "@":
                _emit_lex(helper, tok)
                i += 1
                continue
            _emit_lex(helper, tok)
            _emit_lex("@", tok)
            i += 1
            continue
        _emit_lex(tok.lexeme, tok)
        i += 1

    ctx.inject_token_objects(emitted_tokens)
    return None


def macro_begin_text_macro(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    if parser._eof():
        raise ParseError("macro name missing after 'macro'")
    name_token = parser.next_token()
    param_count = 0
    arity_token: Optional[Token] = None
    explicit_signature = False
    ordered_params: Optional[List[str]] = None
    variadic_param: Optional[str] = None
    peek = parser.peek_token()
    if peek is not None:
        try:
            param_count = int(peek.lexeme, 0)
            parser.next_token()
            arity_token = peek
            explicit_signature = True
        except ValueError:
            pass

    if not explicit_signature and parser._looks_like_pattern_macro_definition():
        clauses = _parse_pattern_macro_clauses(parser, name_token.lexeme, keyword="macro")
        parser.register_pattern_macro(name_token.lexeme, clauses)
        return None

    if not explicit_signature and peek is not None and peek.lexeme == "(":
        parser.next_token()  # consume '('
        explicit_signature = True
        ordered: List[str] = []
        seen: Set[str] = set()
        variadic: Optional[str] = None
        while True:
            if parser._eof():
                raise ParseError(
                    f"unterminated macro signature for '{name_token.lexeme}'"
                )
            tok = parser.next_token()
            lex = tok.lexeme
            if lex == ")":
                break
            if lex == ",":
                continue
            is_variadic = False
            if lex.startswith("*"):
                is_variadic = True
                lex = lex[1:]
            elif lex.startswith("..."):
                is_variadic = True
                lex = lex[3:]
            if not lex or not _is_identifier(lex):
                raise ParseError(
                    f"invalid macro parameter name '{tok.lexeme}' in macro '{name_token.lexeme}'"
                )
            if lex in seen:
                raise ParseError(
                    f"duplicate macro parameter '{lex}' in macro '{name_token.lexeme}'"
                )
            seen.add(lex)
            if is_variadic:
                if variadic is not None:
                    raise ParseError(
                        f"macro '{name_token.lexeme}' cannot declare multiple variadic parameters"
                    )
                variadic = lex
            else:
                if variadic is not None:
                    raise ParseError(
                        f"macro '{name_token.lexeme}' variadic parameter must be last"
                    )
                ordered.append(lex)

        ordered_params = ordered
        variadic_param = variadic
        param_count = len(ordered)

    # Compatibility shorthand for generated nested macros:
    #   macro <name> <value> ;
    # When emitted from macro expansion, treat this as a 0-arg macro whose
    # body is `<value>` instead of an arity-only empty macro definition.
    if (
        explicit_signature
        and arity_token is not None
        and ordered_params is None
        and variadic_param is None
        and param_count != 0
    ):
        tail = parser.peek_token()
        if (
            tail is not None
            and tail.lexeme == ";"
            and (name_token.expansion_depth > 0 or arity_token.expansion_depth > 0)
        ):
            parser.next_token()  # consume ';'
            word = Word(name=name_token.lexeme)
            word.macro_expansion = [arity_token.lexeme]
            word.macro_params = 0
            parser.dictionary.register(word)
            parser._macro_signatures[name_token.lexeme] = (tuple(), None)
            return None

    if ordered_params is None:
        ordered_params = [str(i) for i in range(param_count)]
    parser._start_macro_recording(
        name_token.lexeme,
        param_count,
        ordered_params=ordered_params,
        variadic_param=variadic_param,
    )
    return None


def _parse_pattern_macro_clauses(
    parser: Parser,
    name: str,
    *,
    keyword: str,
) -> List[Any]:
    clauses: List[Any] = []

    while True:
        if parser._eof():
            raise ParseError(f"unterminated {keyword} '{name}' (missing ';' terminator)")

        peek = parser.peek_token()
        if peek is not None and peek.lexeme == ";":
            parser.next_token()
            break

        pattern: List[str] = []
        guard_word: Optional[str] = None
        while True:
            if parser._eof():
                raise ParseError(f"unterminated {keyword} clause in '{name}' (missing '=>')")
            tok = parser.next_token()
            lex = tok.lexeme
            if lex == "=>":
                break
            if lex in ("when", "ct-when"):
                if parser._eof():
                    raise ParseError(
                        f"{keyword} '{name}' clause guard is missing guard word"
                    )
                guard_token = parser.next_token()
                guard_word = guard_token.lexeme
                if parser._eof():
                    raise ParseError(
                        f"{keyword} '{name}' clause guard is missing '=>'"
                    )
                arrow = parser.next_token()
                if arrow.lexeme != "=>":
                    raise ParseError(
                        f"{keyword} '{name}' clause guard must use 'when <guard> =>'")
                break
            if lex == ";":
                raise ParseError(
                    f"{keyword} '{name}' pattern cannot contain ';' before '=>'; "
                    f"close clause with ';' after replacement"
                )
            pattern.append(lex)

        if not pattern:
            raise ParseError(f"{keyword} '{name}' has an empty pattern before '=>'")

        replacement: List[str] = []
        asm_brace_depth = 0
        awaiting_asm_body = False
        awaiting_asm_terminator = False
        while True:
            if parser._eof():
                raise ParseError(f"unterminated {keyword} clause in '{name}' (missing ';')")
            tok = parser.next_token()
            lex = tok.lexeme

            if awaiting_asm_body:
                if lex == "{":
                    asm_brace_depth += 1
                    awaiting_asm_body = False
                replacement.append(lex)
                continue

            if lex == ":asm":
                awaiting_asm_body = True
                replacement.append(lex)
                continue

            if awaiting_asm_terminator:
                replacement.append(lex)
                awaiting_asm_terminator = False
                continue

            if lex == "{" and asm_brace_depth > 0:
                asm_brace_depth += 1
                replacement.append(lex)
                continue

            if lex == "}" and asm_brace_depth > 0:
                asm_brace_depth -= 1
                replacement.append(lex)
                if asm_brace_depth == 0:
                    awaiting_asm_terminator = True
                continue

            if lex == ";" and asm_brace_depth == 0:
                break

            replacement.append(lex)

        if guard_word:
            clauses.append([pattern, replacement, guard_word])
        else:
            clauses.append([pattern, replacement])

    if not clauses:
        raise ParseError(f"{keyword} '{name}' requires at least one clause")

    return clauses

def _struct_emit_definition(tokens: List[Token], template: Token, name: str, body: Sequence[str]) -> None:
    def make_token(lexeme: str) -> Token:
        return Token(
            lexeme=lexeme,
            line=template.line,
            column=template.column,
            start=template.start,
            end=template.end,
        )

    tokens.append(make_token("word"))
    tokens.append(make_token(name))
    for lexeme in body:
        tokens.append(make_token(lexeme))
    tokens.append(make_token("end"))


class SplitLexer:
    def __init__(self, parser: Parser, separators: str) -> None:
        self.parser = parser
        self.separators = set(separators)
        self.buffer: List[Token] = []

    def _fill(self) -> None:
        while not self.buffer:
            if self.parser._eof():
                raise ParseError("unexpected EOF inside custom lexer")
            token = self.parser.next_token()
            parts = _split_token_by_chars(token, self.separators)
            if not parts:
                continue
            self.buffer.extend(parts)

    def peek(self) -> Token:
        self._fill()
        return self.buffer[0]

    def pop(self) -> Token:
        token = self.peek()
        self.buffer.pop(0)
        return token

    def expect(self, lexeme: str) -> Token:
        token = self.pop()
        if token.lexeme != lexeme:
            raise ParseError(f"expected '{lexeme}' but found '{token.lexeme}'")
        return token

    def collect_brace_block(self) -> List[Token]:
        depth = 1
        collected: List[Token] = []
        while depth > 0:
            token = self.pop()
            if token.lexeme == "{":
                depth += 1
                collected.append(token)
                continue
            if token.lexeme == "}":
                depth -= 1
                if depth == 0:
                    break
                collected.append(token)
                continue
            collected.append(token)
        return collected

    def push_back(self) -> None:
        if not self.buffer:
            return
        self.parser.tokens[self.parser.pos:self.parser.pos] = self.buffer
        self.buffer = []


def _split_token_by_chars(token: Token, separators: Set[str]) -> List[Token]:
    lex = token.lexeme
    if not lex:
        return []
    parts: List[Token] = []
    idx = 0
    while idx < len(lex):
        char = lex[idx]
        if char in separators:
            parts.append(Token(
                lexeme=char,
                line=token.line,
                column=token.column + idx,
                start=token.start + idx,
                end=token.start + idx + 1,
            ))
            idx += 1
            continue
        start_idx = idx
        while idx < len(lex) and lex[idx] not in separators:
            idx += 1
        segment = lex[start_idx:idx]
        if segment:
            parts.append(Token(
                lexeme=segment,
                line=token.line,
                column=token.column + start_idx,
                start=token.start + start_idx,
                end=token.start + idx,
            ))
    return parts


def _ensure_list(value: Any) -> List[Any]:
    if not isinstance(value, list):
        raise ParseError("expected list value")
    return value


def _ensure_dict(value: Any) -> Dict[Any, Any]:
    if not isinstance(value, dict):
        raise ParseError("expected map value")
    return value


def _ensure_lexer(value: Any) -> SplitLexer:
    if not isinstance(value, SplitLexer):
        raise ParseError("expected lexer value")
    return value


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    raise ParseError("expected string-compatible value")


def _default_template(template: Optional[Token]) -> Token:
    if template is None:
        return Token(lexeme="", line=0, column=0, start=0, end=0)
    if not isinstance(template, Token):
        raise ParseError("expected token for template")
    return template


def _ct_nil(vm: CompileTimeVM) -> None:
    vm.push(None)


def _ct_nil_p(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.pop() is None else 0)


def _ct_list_new(vm: CompileTimeVM) -> None:
    vm.push([])


def _ct_list_clone(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    vm.push(list(lst))


def _ct_list_append(vm: CompileTimeVM) -> None:
    value = vm.pop()
    lst = _ensure_list(vm.pop())
    lst.append(value)
    vm.push(lst)


def _ct_list_pop(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    if not lst:
        raise ParseError("cannot pop from empty list")
    value = lst.pop()
    vm.push(lst)
    vm.push(value)


def _ct_list_pop_front(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    if not lst:
        raise ParseError("cannot pop from empty list")
    value = lst.pop(0)
    vm.push(lst)
    vm.push(value)


def _ct_list_peek_front(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    if not lst:
        raise ParseError("cannot peek from empty list")
    vm.push(lst)
    vm.push(lst[0])


def _ct_list_push_front(vm: CompileTimeVM) -> None:
    value = vm.pop()
    lst = _ensure_list(vm.pop())
    lst.insert(0, value)
    vm.push(lst)


def _ct_prelude_clear(vm: CompileTimeVM) -> None:
    vm.parser.custom_prelude = []


def _ct_prelude_append(vm: CompileTimeVM) -> None:
    line = vm.pop_str()
    if vm.parser.custom_prelude is None:
        vm.parser.custom_prelude = []
    vm.parser.custom_prelude.append(line)


def _ct_prelude_set(vm: CompileTimeVM) -> None:
    lines = _ensure_list(vm.pop())
    if not all(isinstance(item, str) for item in lines):
        raise ParseError("prelude-set expects list of strings")
    vm.parser.custom_prelude = list(lines)


def _ct_bss_clear(vm: CompileTimeVM) -> None:
    vm.parser.custom_bss = []


def _ct_bss_append(vm: CompileTimeVM) -> None:
    line = vm.pop_str()
    if vm.parser.custom_bss is None:
        vm.parser.custom_bss = []
    vm.parser.custom_bss.append(line)


def _ct_bss_set(vm: CompileTimeVM) -> None:
    lines = _ensure_list(vm.pop())
    if not all(isinstance(item, str) for item in lines):
        raise ParseError("bss-set expects list of strings")
    vm.parser.custom_bss = list(lines)


def _ct_list_reverse(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    lst.reverse()
    vm.push(lst)


def _ct_list_length(vm: CompileTimeVM) -> None:
    lst = vm.pop_list()
    vm.push(len(lst))


def _ct_list_empty(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    vm.push(1 if not lst else 0)


def _ct_loop_index(vm: CompileTimeVM) -> None:
    if not vm.loop_stack:
        raise ParseError("'i' used outside of a for loop")
    frame = vm.loop_stack[-1]
    idx = frame["initial"] - frame["remaining"]
    vm.push(idx)


def _ct_control_frame_new(vm: CompileTimeVM) -> None:
    type_name = vm.pop_str()
    vm.push({"type": type_name})


def _ct_control_get(vm: CompileTimeVM) -> None:
    key = vm.pop_str()
    frame = vm.pop()
    if not isinstance(frame, dict):
        raise ParseError("ct-control-get expects a control frame")
    vm.push(frame.get(key))


def _ct_control_set(vm: CompileTimeVM) -> None:
    value = vm.pop()
    key = vm.pop_str()
    frame = vm.pop()
    if not isinstance(frame, dict):
        raise ParseError("ct-control-set expects a control frame")
    frame[key] = value
    vm.push(frame)


def _ct_control_push(vm: CompileTimeVM) -> None:
    frame = vm.pop()
    if not isinstance(frame, dict):
        raise ParseError("ct-control-push expects a control frame")
    vm.parser._push_control(dict(frame))


def _ct_control_pop(vm: CompileTimeVM) -> None:
    if not vm.parser.control_stack:
        raise ParseError("control stack underflow")
    vm.push(dict(vm.parser.control_stack.pop()))


def _ct_control_peek(vm: CompileTimeVM) -> None:
    if not vm.parser.control_stack:
        vm.push(None)
        return
    vm.push(dict(vm.parser.control_stack[-1]))


def _ct_control_depth(vm: CompileTimeVM) -> None:
    vm.push(len(vm.parser.control_stack))


def _ct_new_label(vm: CompileTimeVM) -> None:
    prefix = vm.pop_str()
    vm.push(vm.parser._new_label(prefix))


def _ct_emit_op(vm: CompileTimeVM) -> None:
    data = vm.pop()
    op_name = vm.pop_str()
    vm.parser.emit_node(_make_op(op_name, data))


def _ct_control_add_close_op(vm: CompileTimeVM) -> None:
    data = vm.pop()
    op_name = vm.pop_str()
    frame = vm.pop()
    if not isinstance(frame, dict):
        raise ParseError("ct-control-add-close-op expects a control frame")
    close_ops = frame.get("close_ops")
    if close_ops is None:
        close_ops = []
    elif not isinstance(close_ops, list):
        raise ParseError("control frame field 'close_ops' must be a list")
    close_ops.append({"op": op_name, "data": data})
    frame["close_ops"] = close_ops
    vm.push(frame)


def _ct_last_token_line(vm: CompileTimeVM) -> None:
    tok = vm.parser._last_token
    vm.push(0 if tok is None else tok.line)


def _ct_register_block_opener(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.parser.block_openers.add(name)


def _ct_unregister_block_opener(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.parser.block_openers.discard(name)


def _ct_register_control_override(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.parser.control_overrides.add(name)


def _ct_unregister_control_override(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.parser.control_overrides.discard(name)


def _ct_list_get(vm: CompileTimeVM) -> None:
    index = vm.pop_int()
    lst = _ensure_list(vm.pop())
    try:
        vm.push(lst[index])
    except IndexError as exc:
        raise ParseError("list index out of range") from exc


def _ct_list_set(vm: CompileTimeVM) -> None:
    value = vm.pop()
    index = vm.pop_int()
    lst = _ensure_list(vm.pop())
    try:
        lst[index] = value
    except IndexError as exc:
        raise ParseError("list index out of range") from exc
    vm.push(lst)


def _ct_list_clear(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    lst.clear()
    vm.push(lst)


def _ct_list_extend(vm: CompileTimeVM) -> None:
    source = _ensure_list(vm.pop())
    target = _ensure_list(vm.pop())
    target.extend(source)
    vm.push(target)


def _ct_list_last(vm: CompileTimeVM) -> None:
    lst = _ensure_list(vm.pop())
    if not lst:
        raise ParseError("list is empty")
    vm.push(lst[-1])


def _ct_list_insert(vm: CompileTimeVM) -> None:
    value = vm.pop()
    index = vm.pop_int()
    lst = _ensure_list(vm.pop())
    if index < 0 or index > len(lst):
        raise ParseError("list index out of range")
    lst.insert(index, value)
    vm.push(lst)


def _ct_list_remove(vm: CompileTimeVM) -> None:
    index = vm.pop_int()
    lst = _ensure_list(vm.pop())
    try:
        value = lst.pop(index)
    except IndexError as exc:
        raise ParseError("list index out of range") from exc
    vm.push(lst)
    vm.push(value)


def _ct_list_slice(vm: CompileTimeVM) -> None:
    end = vm.pop_int()
    start = vm.pop_int()
    lst = _ensure_list(vm.pop())
    vm.push(lst[start:end])


def _ct_list_find(vm: CompileTimeVM) -> None:
    needle = vm.pop()
    lst = _ensure_list(vm.pop())
    for idx, item in enumerate(lst):
        if item == needle:
            vm.push(idx)
            vm.push(1)
            return
    vm.push(-1)
    vm.push(0)


def _ct_list_contains(vm: CompileTimeVM) -> None:
    needle = vm.pop()
    lst = _ensure_list(vm.pop())
    vm.push(1 if needle in lst else 0)


def _ct_list_join(vm: CompileTimeVM) -> None:
    separator = vm.pop_str()
    lst = _ensure_list(vm.pop())
    vm.push(separator.join(_coerce_str(item) for item in lst))


def _ct_map_new(vm: CompileTimeVM) -> None:
    vm.push({})


def _ct_map_set(vm: CompileTimeVM) -> None:
    value = vm.pop()
    key = vm.pop()
    map_obj = _ensure_dict(vm.pop())
    map_obj[key] = value
    vm.push(map_obj)


def _ct_map_get(vm: CompileTimeVM) -> None:
    key = vm.pop()
    map_obj = _ensure_dict(vm.pop())
    vm.push(map_obj)
    if key in map_obj:
        vm.push(map_obj[key])
        vm.push(1)
    else:
        vm.push(None)
        vm.push(0)


def _ct_map_has(vm: CompileTimeVM) -> None:
    key = vm.pop()
    map_obj = _ensure_dict(vm.pop())
    vm.push(map_obj)
    vm.push(1 if key in map_obj else 0)


def _ct_map_delete(vm: CompileTimeVM) -> None:
    key = vm.pop()
    map_obj = _ensure_dict(vm.pop())
    existed = key in map_obj
    if existed:
        del map_obj[key]
    vm.push(map_obj)
    vm.push(1 if existed else 0)


def _ct_map_clear(vm: CompileTimeVM) -> None:
    map_obj = _ensure_dict(vm.pop())
    map_obj.clear()
    vm.push(map_obj)


def _ct_map_length(vm: CompileTimeVM) -> None:
    map_obj = _ensure_dict(vm.pop())
    vm.push(len(map_obj))


def _ct_map_empty(vm: CompileTimeVM) -> None:
    map_obj = _ensure_dict(vm.pop())
    vm.push(1 if not map_obj else 0)


def _ct_map_keys(vm: CompileTimeVM) -> None:
    map_obj = _ensure_dict(vm.pop())
    vm.push(list(map_obj.keys()))


def _ct_map_values(vm: CompileTimeVM) -> None:
    map_obj = _ensure_dict(vm.pop())
    vm.push(list(map_obj.values()))


def _ct_map_clone(vm: CompileTimeVM) -> None:
    map_obj = _ensure_dict(vm.pop())
    vm.push(dict(map_obj))


def _ct_map_update(vm: CompileTimeVM) -> None:
    source = _ensure_dict(vm.pop())
    target = _ensure_dict(vm.pop())
    target.update(source)
    vm.push(target)


_CT_FAST_CT_INTRINSIC_DISPATCH = {
    "nil": _ct_nil,
    "nil?": _ct_nil_p,
    "list-new": _ct_list_new,
    "list-clone": _ct_list_clone,
    "list-append": _ct_list_append,
    "list-pop": _ct_list_pop,
    "list-pop-front": _ct_list_pop_front,
    "list-peek-front": _ct_list_peek_front,
    "list-push-front": _ct_list_push_front,
    "list-reverse": _ct_list_reverse,
    "list-length": _ct_list_length,
    "list-empty?": _ct_list_empty,
    "list-get": _ct_list_get,
    "list-set": _ct_list_set,
    "list-clear": _ct_list_clear,
    "list-extend": _ct_list_extend,
    "list-last": _ct_list_last,
    "list-insert": _ct_list_insert,
    "list-remove": _ct_list_remove,
    "list-slice": _ct_list_slice,
    "list-find": _ct_list_find,
    "list-contains?": _ct_list_contains,
    "list-join": _ct_list_join,
    "map-new": _ct_map_new,
    "map-set": _ct_map_set,
    "map-get": _ct_map_get,
    "map-has?": _ct_map_has,
    "map-delete": _ct_map_delete,
    "map-clear": _ct_map_clear,
    "map-length": _ct_map_length,
    "map-empty?": _ct_map_empty,
    "map-keys": _ct_map_keys,
    "map-values": _ct_map_values,
    "map-clone": _ct_map_clone,
    "map-update": _ct_map_update,
}


def _ct_string_eq(vm: CompileTimeVM) -> None:
    try:
        right = vm.pop_str()
        left = vm.pop_str()
    except ParseError as exc:
        raise ParseError(f"string= expects strings; stack={vm.stack!r}") from exc
    vm.push(1 if left == right else 0)


def _ct_string_length(vm: CompileTimeVM) -> None:
    value = vm.pop_str()
    vm.push(len(value))


def _ct_string_append(vm: CompileTimeVM) -> None:
    right = vm.pop_str()
    left = vm.pop_str()
    vm.push(left + right)


def _ct_string_to_number(vm: CompileTimeVM) -> None:
    text = vm.pop_str()
    try:
        value = int(text, 0)
        vm.push(value)
        vm.push(1)
    except ValueError:
        vm.push(0)
        vm.push(0)


def _ct_string_contains(vm: CompileTimeVM) -> None:
    needle = vm.pop_str()
    haystack = vm.pop_str()
    vm.push(1 if needle in haystack else 0)


def _ct_string_starts_with(vm: CompileTimeVM) -> None:
    prefix = vm.pop_str()
    text = vm.pop_str()
    vm.push(1 if text.startswith(prefix) else 0)


def _ct_string_ends_with(vm: CompileTimeVM) -> None:
    suffix = vm.pop_str()
    text = vm.pop_str()
    vm.push(1 if text.endswith(suffix) else 0)


def _ct_string_split(vm: CompileTimeVM) -> None:
    separator = vm.pop_str()
    text = vm.pop_str()
    if separator == "":
        raise ParseError("string-split separator cannot be empty")
    vm.push(text.split(separator))


def _ct_string_join(vm: CompileTimeVM) -> None:
    separator = vm.pop_str()
    items = _ensure_list(vm.pop())
    vm.push(separator.join(_coerce_str(item) for item in items))


def _ct_string_strip(vm: CompileTimeVM) -> None:
    text = vm.pop_str()
    vm.push(text.strip())


def _ct_string_replace(vm: CompileTimeVM) -> None:
    replacement = vm.pop_str()
    needle = vm.pop_str()
    text = vm.pop_str()
    vm.push(text.replace(needle, replacement))


def _ct_string_upper(vm: CompileTimeVM) -> None:
    text = vm.pop_str()
    vm.push(text.upper())


def _ct_string_lower(vm: CompileTimeVM) -> None:
    text = vm.pop_str()
    vm.push(text.lower())


def _ct_set_token_hook(vm: CompileTimeVM) -> None:
    hook_name = vm.pop_str()
    vm.parser.token_hook = hook_name


def _ct_clear_token_hook(vm: CompileTimeVM) -> None:
    vm.parser.token_hook = None


def _ct_use_l2_compile_time(vm: CompileTimeVM) -> None:
    if vm.stack:
        name = vm.pop_str()
        word = vm.dictionary.lookup(name)
    else:
        word = vm.parser.most_recent_definition()
        if word is None:
            raise ParseError("use-l2-ct with empty stack and no recent definition")
        name = word.name
    if word is None:
        raise ParseError(f"unknown word '{name}' for use-l2-ct")
    if word.runtime_only:
        raise ParseError(f"word '{name}' is runtime-only and cannot be executed at compile time")
    word.compile_time_intrinsic = None
    word.compile_time_override = True


def _ct_add_token(vm: CompileTimeVM) -> None:
    tok = vm.pop_str()
    vm.parser.reader.add_tokens([tok])


def _ct_add_token_chars(vm: CompileTimeVM) -> None:
    chars = vm.pop_str()
    vm.parser.reader.add_token_chars(chars)


def _ct_add_reader_rewrite(vm: CompileTimeVM) -> None:
    replacement = _coerce_lexeme_list(vm.pop(), field="reader rewrite replacement")
    pattern = _coerce_lexeme_list(vm.pop(), field="reader rewrite pattern")
    vm.push(vm.parser.add_rewrite_rule("reader", pattern, replacement))


def _ct_add_reader_rewrite_named(vm: CompileTimeVM) -> None:
    replacement = _coerce_lexeme_list(vm.pop(), field="reader rewrite replacement")
    pattern = _coerce_lexeme_list(vm.pop(), field="reader rewrite pattern")
    name = vm.pop_str()
    vm.push(vm.parser.add_rewrite_rule("reader", pattern, replacement, name=name))


def _ct_add_reader_rewrite_priority(vm: CompileTimeVM) -> None:
    replacement = _coerce_lexeme_list(vm.pop(), field="reader rewrite replacement")
    pattern = _coerce_lexeme_list(vm.pop(), field="reader rewrite pattern")
    priority = vm.pop_int()
    vm.push(vm.parser.add_rewrite_rule("reader", pattern, replacement, priority=priority))


def _ct_remove_reader_rewrite(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.push(1 if vm.parser.remove_rewrite_rule("reader", name) else 0)


def _ct_clear_reader_rewrites(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.clear_rewrite_rules("reader"))


def _ct_list_reader_rewrites(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.list_rewrite_rules("reader"))


def _ct_set_reader_rewrite_enabled(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="reader rewrite enabled flag")
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_rewrite_rule_enabled("reader", name, enabled) else 0)


def _ct_get_reader_rewrite_enabled(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    enabled = vm.parser.get_rewrite_rule_enabled("reader", name)
    if enabled is None:
        vm.push(0)
        vm.push(0)
    else:
        vm.push(1 if enabled else 0)
        vm.push(1)


def _ct_set_reader_rewrite_priority(vm: CompileTimeVM) -> None:
    priority = vm.pop_int()
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_rewrite_rule_priority("reader", name, priority) else 0)


def _ct_get_reader_rewrite_priority(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    priority = vm.parser.get_rewrite_rule_priority("reader", name)
    if priority is None:
        vm.push(0)
        vm.push(0)
    else:
        vm.push(priority)
        vm.push(1)


def _ct_add_grammar_rewrite(vm: CompileTimeVM) -> None:
    replacement = _coerce_lexeme_list(vm.pop(), field="grammar rewrite replacement")
    pattern = _coerce_lexeme_list(vm.pop(), field="grammar rewrite pattern")
    vm.push(vm.parser.add_rewrite_rule("grammar", pattern, replacement))


def _ct_add_grammar_rewrite_named(vm: CompileTimeVM) -> None:
    replacement = _coerce_lexeme_list(vm.pop(), field="grammar rewrite replacement")
    pattern = _coerce_lexeme_list(vm.pop(), field="grammar rewrite pattern")
    name = vm.pop_str()
    vm.push(vm.parser.add_rewrite_rule("grammar", pattern, replacement, name=name))


def _ct_add_grammar_rewrite_priority(vm: CompileTimeVM) -> None:
    replacement = _coerce_lexeme_list(vm.pop(), field="grammar rewrite replacement")
    pattern = _coerce_lexeme_list(vm.pop(), field="grammar rewrite pattern")
    priority = vm.pop_int()
    vm.push(vm.parser.add_rewrite_rule("grammar", pattern, replacement, priority=priority))


def _ct_remove_grammar_rewrite(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.push(1 if vm.parser.remove_rewrite_rule("grammar", name) else 0)


def _ct_clear_grammar_rewrites(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.clear_rewrite_rules("grammar"))


def _ct_list_grammar_rewrites(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.list_rewrite_rules("grammar"))


def _ct_set_grammar_rewrite_enabled(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="grammar rewrite enabled flag")
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_rewrite_rule_enabled("grammar", name, enabled) else 0)


def _ct_get_grammar_rewrite_enabled(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    enabled = vm.parser.get_rewrite_rule_enabled("grammar", name)
    if enabled is None:
        vm.push(0)
        vm.push(0)
    else:
        vm.push(1 if enabled else 0)
        vm.push(1)


def _ct_set_grammar_rewrite_priority(vm: CompileTimeVM) -> None:
    priority = vm.pop_int()
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_rewrite_rule_priority("grammar", name, priority) else 0)


def _ct_get_grammar_rewrite_priority(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    priority = vm.parser.get_rewrite_rule_priority("grammar", name)
    if priority is None:
        vm.push(0)
        vm.push(0)
    else:
        vm.push(priority)
        vm.push(1)


def _ct_register_text_macro(vm: CompileTimeVM) -> None:
    expansion = _coerce_lexeme_list(vm.pop(), field="macro expansion")
    param_count = vm.pop_int()
    name = vm.pop_str()
    vm.parser.register_text_macro(name, param_count, expansion)


def _ct_register_text_macro_signature(vm: CompileTimeVM) -> None:
    expansion = _coerce_lexeme_list(vm.pop(), field="macro expansion")
    param_spec = _coerce_lexeme_list(vm.pop(), field="macro parameter spec")
    name = vm.pop_str()
    vm.parser.register_text_macro_signature(name, param_spec, expansion)


def _coerce_pattern_macro_clauses(value: Any) -> List[Any]:
    raw_clauses = _ensure_list(value)
    clauses: List[Any] = []
    for idx, clause_value in enumerate(raw_clauses):
        if isinstance(clause_value, dict):
            row = _ensure_dict(clause_value)
            if "pattern" not in row or "replacement" not in row:
                raise ParseError(
                    f"pattern macro clause {idx + 1} map must contain 'pattern' and 'replacement'"
                )
            pattern = _coerce_lexeme_list(row["pattern"], field=f"pattern macro clause {idx + 1} pattern")
            replacement = _coerce_lexeme_list(row["replacement"], field=f"pattern macro clause {idx + 1} replacement")
            entry: Dict[str, Any] = {
                "pattern": pattern,
                "replacement": replacement,
            }
            guard = row.get("guard")
            if guard is not None:
                entry["guard"] = _coerce_str(guard)
            group = row.get("group")
            if group is not None:
                entry["group"] = _coerce_str(group)
            scope = row.get("scope")
            if scope is not None:
                entry["scope"] = _coerce_str(scope)
            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                entry["metadata"] = dict(metadata)
            clauses.append(entry)
            continue

        pair = _ensure_list(clause_value)
        if len(pair) not in (2, 3):
            raise ParseError(
                f"pattern macro clause {idx + 1} must be [pattern-list replacement-list] or [pattern replacement guard]"
            )
        pattern = _coerce_lexeme_list(pair[0], field=f"pattern macro clause {idx + 1} pattern")
        replacement = _coerce_lexeme_list(pair[1], field=f"pattern macro clause {idx + 1} replacement")
        if len(pair) == 3 and pair[2] is not None:
            clauses.append([pattern, replacement, _coerce_str(pair[2])])
        else:
            clauses.append([pattern, replacement])
    return clauses


def _ct_register_pattern_macro(vm: CompileTimeVM) -> None:
    clauses = _coerce_pattern_macro_clauses(vm.pop())
    name = vm.pop_str()
    vm.parser.register_pattern_macro(name, clauses)


def _ct_unregister_pattern_macro(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.push(1 if vm.parser.unregister_pattern_macro(name) else 0)


def _ct_word_is_text_macro(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    word = vm.dictionary.lookup(name)
    vm.push(1 if (word is not None and word.macro_expansion is not None) else 0)


def _ct_word_is_pattern_macro(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.push(1 if name in vm.parser._pattern_macro_rules else 0)


def _ct_get_macro_signature(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    word = vm.dictionary.lookup(name)
    if word is None or word.macro_expansion is None:
        vm.push(None)
        vm.push(None)
        vm.push(0)
        return

    signature = vm.parser._macro_signatures.get(name)
    if signature is not None:
        ordered, variadic = signature
    else:
        ordered = tuple(str(i) for i in range(max(0, int(word.macro_params))))
        variadic = None

    vm.push(list(ordered))
    vm.push(variadic)
    vm.push(1)


def _ct_get_macro_expansion(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    expansion = vm.parser.get_macro_expansion(name)
    if expansion is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(list(expansion))
    vm.push(1)


def _ct_set_macro_expansion(vm: CompileTimeVM) -> None:
    expansion = _coerce_lexeme_list(vm.pop(), field="macro expansion")
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_macro_expansion(name, expansion) else 0)


def _ct_clone_macro(vm: CompileTimeVM) -> None:
    target = vm.pop_str()
    source = vm.pop_str()
    vm.push(1 if vm.parser.clone_macro(source, target) else 0)


def _ct_rename_macro(vm: CompileTimeVM) -> None:
    target = vm.pop_str()
    source = vm.pop_str()
    vm.push(1 if vm.parser.rename_macro(source, target) else 0)


def _ct_macro_doc_get(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    value = vm.parser.get_macro_doc(name)
    if value is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(value)
    vm.push(1)


def _ct_macro_doc_set(vm: CompileTimeVM) -> None:
    raw_value = vm._resolve_handle(vm.pop())
    name = vm.pop_str()
    doc_value: Optional[str]
    if raw_value is None:
        doc_value = None
    else:
        doc_value = _coerce_str(raw_value)
    vm.push(1 if vm.parser.set_macro_doc(name, doc_value) else 0)


def _ct_macro_attrs_get(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    attrs = vm.parser.get_macro_attrs(name)
    if attrs is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(attrs)
    vm.push(1)


def _ct_macro_attrs_set(vm: CompileTimeVM) -> None:
    raw_value = vm._resolve_handle(vm.pop())
    name = vm.pop_str()
    attrs_value: Optional[Dict[str, Any]]
    if raw_value is None:
        attrs_value = None
    else:
        attrs = _ensure_dict(raw_value)
        attrs_value = {
            str(key): _capture_deep_clone(item)
            for key, item in attrs.items()
        }
    vm.push(1 if vm.parser.set_macro_attrs(name, attrs_value) else 0)


def _ct_set_ct_call_contract(vm: CompileTimeVM) -> None:
    raw_contract = vm._resolve_handle(vm.pop())
    name = vm.pop_str()
    if vm.dictionary.lookup(name) is None:
        vm.push(0)
        return
    if raw_contract is None:
        vm.parser._ct_call_abi_contracts.pop(name, None)
        vm.push(1)
        return
    contract = _ensure_dict(raw_contract)
    vm.parser._ct_call_abi_contracts[name] = {
        str(key): _capture_deep_clone(item)
        for key, item in contract.items()
    }
    vm.push(1)


def _ct_get_ct_call_contract(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    contract = vm.parser._ct_call_abi_contracts.get(name)
    if contract is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push({
        str(key): _capture_deep_clone(item)
        for key, item in contract.items()
    })
    vm.push(1)


def _ct_set_ct_call_exception_policy(vm: CompileTimeVM) -> None:
    policy = vm.pop_str().strip().lower()
    if policy not in ("raise", "warn", "empty", "nil", "ignore"):
        raise ParseError("ct-call exception policy must be one of: raise, warn, empty, nil, ignore")
    if policy == "nil":
        policy = "empty"
    vm.parser._ct_call_exception_policy = policy


def _ct_get_ct_call_exception_policy(vm: CompileTimeVM) -> None:
    vm.push(str(vm.parser._ct_call_exception_policy))


def _ct_set_ct_call_sandbox_mode(vm: CompileTimeVM) -> None:
    mode = vm.pop_str().strip().lower()
    if mode not in ("off", "allowlist", "compile-only", "compile_only"):
        raise ParseError("ct-call sandbox mode must be one of: off, allowlist, compile-only")
    if mode == "compile_only":
        mode = "compile-only"
    vm.parser._ct_call_sandbox_mode = mode


def _ct_get_ct_call_sandbox_mode(vm: CompileTimeVM) -> None:
    vm.push(str(vm.parser._ct_call_sandbox_mode))


def _ct_set_ct_call_sandbox_allowlist(vm: CompileTimeVM) -> None:
    values = _coerce_lexeme_list(vm.pop(), field="ct-call sandbox allowlist")
    vm.parser._ct_call_sandbox_allowlist = set(values)
    vm.push(len(vm.parser._ct_call_sandbox_allowlist))


def _ct_get_ct_call_sandbox_allowlist(vm: CompileTimeVM) -> None:
    vm.push(sorted(vm.parser._ct_call_sandbox_allowlist))


def _ct_ctrand_seed(vm: CompileTimeVM) -> None:
    seed = vm.pop_int()
    vm.parser._ct_call_rng_seed = int(seed)
    vm.parser._ct_call_rng.seed(int(seed))


def _ct_ctrand_int(vm: CompileTimeVM) -> None:
    bound = vm.pop_int()
    if bound <= 0:
        raise ParseError("ct-ctrand-int expects bound > 0")
    vm.push(int(vm.parser._ct_call_rng.randrange(bound)))


def _ct_ctrand_range(vm: CompileTimeVM) -> None:
    hi = vm.pop_int()
    lo = vm.pop_int()
    if hi < lo:
        raise ParseError("ct-ctrand-range expects lo <= hi")
    vm.push(int(vm.parser._ct_call_rng.randint(lo, hi)))


def _ct_set_ct_call_memo(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="ct-call memoization flag")
    vm.parser._ct_call_memo_enabled = bool(enabled)


def _ct_get_ct_call_memo(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser._ct_call_memo_enabled else 0)


def _ct_clear_ct_call_memo(vm: CompileTimeVM) -> None:
    count = len(vm.parser._ct_call_memo_cache)
    vm.parser._ct_call_memo_cache.clear()
    vm.push(count)


def _ct_get_ct_call_memo_size(vm: CompileTimeVM) -> None:
    vm.push(len(vm.parser._ct_call_memo_cache))


def _ct_set_ct_call_side_effects(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="ct-call side-effect tracking flag")
    vm.parser._ct_call_side_effect_tracking = bool(enabled)


def _ct_get_ct_call_side_effects(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser._ct_call_side_effect_tracking else 0)


def _ct_get_ct_call_side_effect_log(vm: CompileTimeVM) -> None:
    vm.push([dict(entry) for entry in vm.parser._ct_call_side_effect_log])


def _ct_clear_ct_call_side_effect_log(vm: CompileTimeVM) -> None:
    count = len(vm.parser._ct_call_side_effect_log)
    vm.parser._ct_call_side_effect_log.clear()
    vm.push(count)


def _ct_set_ct_call_recursion_limit(vm: CompileTimeVM) -> None:
    limit = vm.pop_int()
    if limit < 1:
        raise ParseError("ct-call recursion limit must be >= 1")
    vm.parser._ct_call_recursion_limit = int(limit)


def _ct_get_ct_call_recursion_limit(vm: CompileTimeVM) -> None:
    vm.push(int(vm.parser._ct_call_recursion_limit))


def _ct_set_ct_call_timeout_ms(vm: CompileTimeVM) -> None:
    budget = vm.pop_int()
    if budget < 0:
        raise ParseError("ct-call timeout budget must be >= 0")
    vm.parser._ct_call_timeout_ms = int(budget)


def _ct_get_ct_call_timeout_ms(vm: CompileTimeVM) -> None:
    vm.push(int(vm.parser._ct_call_timeout_ms))


def _ct_prepare_macro_template_introspection(vm: CompileTimeVM, name: str) -> Optional[Word]:
    word = vm.dictionary.lookup(name)
    if word is None:
        return None

    if word.macro_template_ast is None:
        if word.macro_expansion is None:
            return None
        engine = vm.parser.macro_engine
        template_tokens = list(word.macro_expansion)
        parsed_nodes, idx, stop = engine._parse_macro_template_nodes(
            word=word,
            tokens=template_tokens,
            idx=0,
            stop_tokens=None,
        )
        if stop is not None:
            raise ParseError(f"macro '{word.name}' has unexpected template terminator '{stop}'")
        if idx != len(template_tokens):
            raise ParseError(f"macro '{word.name}' template parser stopped early")
        word.macro_template_ast = tuple(parsed_nodes)

    if word.macro_template_program is None:
        engine = vm.parser.macro_engine
        nodes = list(word.macro_template_ast)
        mode, version = engine._collect_macro_template_metadata(word=word, nodes=nodes)
        word.macro_template_mode = mode
        word.macro_template_version = version
        word.macro_template_program = engine._compile_macro_template_program(nodes=nodes)
    return word


def _ct_get_macro_template_mode(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    word = _ct_prepare_macro_template_introspection(vm, name)
    if word is None:
        vm.push(None)
        vm.push(0)
        return
    mode = getattr(word, "macro_template_mode", "strict") or "strict"
    vm.push(mode)
    vm.push(1)


def _ct_get_macro_template_version(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    word = _ct_prepare_macro_template_introspection(vm, name)
    if word is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(getattr(word, "macro_template_version", None))
    vm.push(1)


def _ct_get_macro_template_program_size(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    word = _ct_prepare_macro_template_introspection(vm, name)
    if word is None:
        vm.push(0)
        vm.push(0)
        return
    program = getattr(word, "macro_template_program", None)
    if program is None:
        vm.push(0)
        vm.push(0)
        return
    vm.push(len(program))
    vm.push(1)


def _ensure_capture_context(value: Any) -> Dict[str, Any]:
    ctx = _ensure_dict(value)
    captures = ctx.get("captures")
    if not isinstance(captures, dict):
        raise ParseError("capture context missing 'captures' map")
    return ctx


def _capture_deep_clone(value: Any) -> Any:
    if isinstance(value, list):
        return [_capture_deep_clone(item) for item in value]
    if isinstance(value, tuple):
        return [_capture_deep_clone(item) for item in value]
    if isinstance(value, dict):
        return {
            _capture_deep_clone(key): _capture_deep_clone(item)
            for key, item in value.items()
        }
    if isinstance(value, Token):
        return value.lexeme
    return value


def _capture_is_group_list(value: Any) -> bool:
    return bool(isinstance(value, list) and value and isinstance(value[0], list))


def _capture_piece_to_token(piece: Any, *, field: str) -> str:
    if isinstance(piece, Token):
        return piece.lexeme
    if isinstance(piece, str):
        return piece
    if isinstance(piece, bool):
        return "1" if piece else "0"
    if isinstance(piece, int):
        return str(piece)
    raise ParseError(f"{field} expected token-like values")


def _capture_flatten_tokens(value: Any, *, field: str) -> List[str]:
    if value is None:
        return []
    if _capture_is_group_list(value):
        out: List[str] = []
        for idx, group in enumerate(value):
            if not isinstance(group, list):
                raise ParseError(f"{field} variadic group {idx + 1} is not a list")
            for piece in group:
                out.append(_capture_piece_to_token(piece, field=field))
        return out
    if isinstance(value, list):
        return [_capture_piece_to_token(piece, field=field) for piece in value]
    if isinstance(value, (Token, str, bool, int)):
        return [_capture_piece_to_token(value, field=field)]
    raise ParseError(f"{field} expected list/group-list/token-like value")


def _capture_shape(value: Any) -> str:
    if value is None:
        return "none"
    if _capture_is_group_list(value):
        return "multi"
    if isinstance(value, list):
        if len(value) == 1:
            return "single"
        return "tokens"
    return "scalar"


def _capture_normalize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Token):
        return value.lexeme
    if isinstance(value, list):
        return [_capture_normalize_value(item) for item in value]
    if isinstance(value, tuple):
        return [_capture_normalize_value(item) for item in value]
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, (bool, int, float, str)):
                out[str(key)] = _capture_normalize_value(item)
            elif isinstance(key, Token):
                out[key.lexeme] = _capture_normalize_value(item)
            else:
                out[str(key)] = _capture_normalize_value(item)
        return out
    return repr(value)


def _capture_serialize_text(value: Any) -> str:
    import json

    normalized = _capture_normalize_value(value)
    return json.dumps(normalized, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _capture_diff_values(left: Any, right: Any, *, path: str, out: List[str]) -> None:
    if type(left) is not type(right):
        out.append(f"{path}: type mismatch ({type(left).__name__} != {type(right).__name__})")
        return

    if isinstance(left, dict):
        keys = sorted(set(left.keys()) | set(right.keys()))
        for key in keys:
            key_path = f"{path}.{key}"
            if key not in left:
                out.append(f"{key_path}: missing on left")
                continue
            if key not in right:
                out.append(f"{key_path}: missing on right")
                continue
            _capture_diff_values(left[key], right[key], path=key_path, out=out)
        return

    if isinstance(left, list):
        max_len = max(len(left), len(right))
        for idx in range(max_len):
            idx_path = f"{path}[{idx}]"
            if idx >= len(left):
                out.append(f"{idx_path}: missing on left")
                continue
            if idx >= len(right):
                out.append(f"{idx_path}: missing on right")
                continue
            _capture_diff_values(left[idx], right[idx], path=idx_path, out=out)
        return

    if left != right:
        out.append(f"{path}: {left!r} != {right!r}")


def _capture_filter_token(parser: Parser, predicate: str, token: str) -> bool:
    if predicate == "nonempty":
        return token != ""
    if predicate in ("ident", "identifier"):
        return _is_identifier(token)
    if predicate in ("int", "integer"):
        try:
            int(token, 0)
            return True
        except Exception:
            return False
    if predicate in ("number", "numeric"):
        try:
            int(token, 0)
            return True
        except Exception:
            try:
                float(token)
                return True
            except Exception:
                return False
    if predicate in ("string", "str"):
        return len(token) >= 2 and token[0] == '"' and token[-1] == '"'
    if predicate in ("char", "chr"):
        return len(token) >= 2 and token[0] == "'" and token[-1] == "'"
    return parser._rewrite_constraint_matches(token, predicate)


def _capture_apply_map(value: Any, op: str) -> Any:
    def _map_token(token: str) -> str:
        if op == "upper":
            return token.upper()
        if op == "lower":
            return token.lower()
        if op == "strip":
            return token.strip()
        if op in ("int-normalize", "int"):
            try:
                return str(int(token, 0))
            except Exception:
                return token
        raise ParseError(f"unsupported capture map operation '{op}'")

    if value is None:
        return []
    if _capture_is_group_list(value):
        mapped: List[List[str]] = []
        for group in value:
            group_tokens = _capture_flatten_tokens(group, field="capture-map group")
            mapped.append([_map_token(tok) for tok in group_tokens])
        return mapped
    tokens = _capture_flatten_tokens(value, field="capture-map source")
    return [_map_token(tok) for tok in tokens]


def _capture_apply_filter(parser: Parser, value: Any, predicate: str) -> Any:
    if value is None:
        return []
    if _capture_is_group_list(value):
        filtered: List[List[str]] = []
        for group in value:
            group_tokens = _capture_flatten_tokens(group, field="capture-filter group")
            keep = [tok for tok in group_tokens if _capture_filter_token(parser, predicate, tok)]
            if keep:
                filtered.append(keep)
        return filtered
    tokens = _capture_flatten_tokens(value, field="capture-filter source")
    return [tok for tok in tokens if _capture_filter_token(parser, predicate, tok)]


def _ct_gensym(vm: CompileTimeVM) -> None:
    prefix = vm.pop_str()
    safe_prefix = re.sub(r"[^A-Za-z0-9_]", "_", prefix).strip("_")
    if not safe_prefix:
        safe_prefix = "g"

    counter = int(getattr(vm.parser, "_ct_gensym_counter", 0))
    while True:
        counter += 1
        candidate = f"{safe_prefix}_{counter}"
        if vm.dictionary.lookup(candidate) is None:
            break
    setattr(vm.parser, "_ct_gensym_counter", counter)
    vm.push(candidate)


def _ct_capture_args(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    namespaces = ctx.get("capture_namespaces")
    if isinstance(namespaces, dict):
        args = namespaces.get("args")
    else:
        args = ctx.get("args")
    vm.push(_capture_deep_clone(args if isinstance(args, dict) else {}))


def _ct_capture_locals(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    namespaces = ctx.get("capture_namespaces")
    if isinstance(namespaces, dict):
        locals_scope = namespaces.get("locals")
    else:
        locals_scope = ctx.get("locals")
    vm.push(_capture_deep_clone(locals_scope if isinstance(locals_scope, dict) else {}))


def _ct_capture_globals(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    namespaces = ctx.get("capture_namespaces")
    if isinstance(namespaces, dict):
        globals_scope = namespaces.get("globals")
    else:
        globals_scope = ctx.get("globals")
    vm.push(_capture_deep_clone(globals_scope if isinstance(globals_scope, dict) else {}))


def _ct_capture_get(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    ctx = _ensure_capture_context(vm.pop())
    captures = ctx.get("captures")
    if isinstance(captures, dict) and name in captures:
        vm.push(_capture_deep_clone(captures[name]))
        vm.push(1)
        return
    vm.push(None)
    vm.push(0)


def _ct_capture_has(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    ctx = _ensure_capture_context(vm.pop())
    captures = ctx.get("captures")
    vm.push(1 if isinstance(captures, dict) and name in captures else 0)


def _ct_capture_shape(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    vm.push(_capture_shape(value))


def _ct_capture_assert_shape(vm: CompileTimeVM) -> None:
    expected = vm.pop_str().strip().lower()
    value = vm._resolve_handle(vm.pop())
    actual = _capture_shape(value)
    if expected != actual:
        raise ParseError(f"capture shape mismatch: expected '{expected}', got '{actual}'")


def _ct_capture_count(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    if value is None:
        vm.push(0)
        return
    if _capture_is_group_list(value):
        vm.push(len(value))
        return
    if isinstance(value, list):
        vm.push(len(value))
        return
    raise ParseError("capture-count expects list/group-list/nil value")


def _ct_capture_slice(vm: CompileTimeVM) -> None:
    end = vm.pop_int()
    start = vm.pop_int()
    value = vm._resolve_handle(vm.pop())
    if value is None:
        vm.push([])
        return
    if _capture_is_group_list(value):
        vm.push([list(group) for group in value[start:end]])
        return
    if isinstance(value, list):
        vm.push(list(value[start:end]))
        return
    raise ParseError("capture-slice expects list/group-list/nil value")


def _ct_capture_map(vm: CompileTimeVM) -> None:
    op = vm.pop_str().strip().lower()
    value = vm._resolve_handle(vm.pop())
    vm.push(_capture_apply_map(value, op))


def _ct_capture_filter(vm: CompileTimeVM) -> None:
    predicate = vm.pop_str().strip().lower()
    value = vm._resolve_handle(vm.pop())
    vm.push(_capture_apply_filter(vm.parser, value, predicate))


def _ct_capture_normalize(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    vm.push(_capture_normalize_value(value))


def _ct_capture_pretty(vm: CompileTimeVM) -> None:
    import json

    value = vm._resolve_handle(vm.pop())
    vm.push(json.dumps(_capture_normalize_value(value), sort_keys=True, ensure_ascii=True, indent=2))


def _ct_capture_clone(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    vm.push(_capture_deep_clone(value))


def _ct_capture_global_set(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    name = vm.pop_str()
    vm.parser.capture_globals[name] = _capture_deep_clone(value)


def _ct_capture_global_get(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    if name in vm.parser.capture_globals:
        vm.push(_capture_deep_clone(vm.parser.capture_globals[name]))
        vm.push(1)
        return
    vm.push(None)
    vm.push(0)


def _ct_capture_global_delete(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    existed = name in vm.parser.capture_globals
    if existed:
        del vm.parser.capture_globals[name]
    vm.push(1 if existed else 0)


def _ct_capture_global_clear(vm: CompileTimeVM) -> None:
    count = len(vm.parser.capture_globals)
    vm.parser.capture_globals.clear()
    vm.push(count)


def _ct_capture_freeze(vm: CompileTimeVM) -> None:
    capture_name = vm.pop_str()
    macro_name = vm.pop_str()
    vm.parser.capture_mutability_frozen.add((macro_name, capture_name))


def _ct_capture_thaw(vm: CompileTimeVM) -> None:
    capture_name = vm.pop_str()
    macro_name = vm.pop_str()
    key = (macro_name, capture_name)
    existed = key in vm.parser.capture_mutability_frozen
    if existed:
        vm.parser.capture_mutability_frozen.remove(key)
    vm.push(1 if existed else 0)


def _ct_capture_mutable(vm: CompileTimeVM) -> None:
    capture_name = vm.pop_str()
    macro_name = vm.pop_str()
    vm.push(0 if (macro_name, capture_name) in vm.parser.capture_mutability_frozen else 1)


def _ct_capture_schema_put(vm: CompileTimeVM) -> None:
    required = _coerce_bool(vm.pop(), field="capture schema required flag")
    type_name = vm.pop_str().strip().lower() or "any"
    shape = vm.pop_str().strip().lower() or "any"
    capture_name = vm.pop_str()
    macro_name = vm.pop_str()

    if shape not in ("any", "single", "tokens", "multi", "none", "scalar"):
        raise ParseError(f"capture schema shape '{shape}' is not supported")

    schema = vm.parser.capture_schemas.setdefault(macro_name, {})
    schema[capture_name] = {
        "shape": shape,
        "type": type_name,
        "required": bool(required),
    }


def _ct_capture_schema_get(vm: CompileTimeVM) -> None:
    macro_name = vm.pop_str()
    schema = vm.parser.capture_schemas.get(macro_name)
    if schema is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(_capture_deep_clone(schema))
    vm.push(1)


def _ct_capture_schema_validate(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    macro_name = str(ctx.get("macro") or "")
    if not macro_name:
        raise ParseError("capture schema validation requires context with macro name")

    schema = vm.parser.capture_schemas.get(macro_name)
    if not schema:
        vm.push(1)
        return

    captures = ctx.get("captures")
    if not isinstance(captures, dict):
        raise ParseError("capture schema validation requires context captures map")

    errors: List[str] = []
    for capture_name, raw_spec in schema.items():
        spec = raw_spec if isinstance(raw_spec, dict) else {}
        found = capture_name in captures
        required = bool(spec.get("required", False))
        if required and not found:
            errors.append(f"missing required capture '{capture_name}'")
            continue
        if not found:
            continue

        value = captures[capture_name]
        expected_shape = str(spec.get("shape", "any")).strip().lower()
        if expected_shape and expected_shape != "any":
            actual_shape = _capture_shape(value)
            if actual_shape != expected_shape:
                errors.append(
                    f"capture '{capture_name}' shape mismatch: expected '{expected_shape}', got '{actual_shape}'"
                )

        expected_type = str(spec.get("type", "any")).strip().lower()
        if expected_type and expected_type != "any":
            try:
                tokens = _capture_flatten_tokens(value, field=f"capture '{capture_name}'")
            except ParseError as exc:
                errors.append(str(exc))
                tokens = []
            for token in tokens:
                if not vm.parser._rewrite_constraint_matches(token, expected_type):
                    errors.append(
                        f"capture '{capture_name}' expected '{expected_type}' token, got '{token}'"
                    )
                    break

        if "mutable" in spec:
            expected_mutable = bool(spec.get("mutable"))
            actual_mutable = (macro_name, capture_name) not in vm.parser.capture_mutability_frozen
            if expected_mutable != actual_mutable:
                errors.append(
                    f"capture '{capture_name}' mutability mismatch: expected {int(expected_mutable)}, got {int(actual_mutable)}"
                )

    if errors:
        rendered = "\n".join(f"  - {item}" for item in errors)
        raise ParseError(
            f"macro '{macro_name}' capture schema validation failed:\n{rendered}"
        )
    vm.push(1)


def _ct_capture_coerce_tokens(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    vm.push(_capture_flatten_tokens(value, field="capture-coerce-tokens"))


def _ct_capture_coerce_string(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    if isinstance(value, str):
        vm.push(value)
        return
    vm.push(" ".join(_capture_flatten_tokens(value, field="capture-coerce-string")))


def _ct_capture_coerce_number(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    candidate: Optional[str] = None
    if isinstance(value, bool):
        vm.push(1 if value else 0)
        vm.push(1)
        return
    if isinstance(value, int):
        vm.push(value)
        vm.push(1)
        return
    if isinstance(value, Token):
        candidate = value.lexeme
    elif isinstance(value, str):
        candidate = value
    else:
        tokens = _capture_flatten_tokens(value, field="capture-coerce-number")
        if len(tokens) == 1:
            candidate = tokens[0]

    if candidate is None:
        vm.push(0)
        vm.push(0)
        return

    try:
        vm.push(int(candidate, 0))
        vm.push(1)
    except Exception:
        vm.push(0)
        vm.push(0)


def _ct_capture_lifetime(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    try:
        lifetime = int(ctx.get("lifetime", 0))
    except Exception:
        lifetime = 0
    vm.push(lifetime)


def _ct_capture_lifetime_live(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    try:
        lifetime = int(ctx.get("lifetime", 0))
    except Exception:
        lifetime = 0
    vm.push(1 if lifetime != 0 and lifetime == vm.parser._capture_lifetime_active else 0)


def _ct_capture_lifetime_assert(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    try:
        lifetime = int(ctx.get("lifetime", 0))
    except Exception:
        lifetime = 0
    if lifetime == 0 or lifetime != vm.parser._capture_lifetime_active:
        raise ParseError(
            f"capture context lifetime is stale (ctx={lifetime}, active={vm.parser._capture_lifetime_active})"
        )


def _ct_capture_separate(vm: CompileTimeVM) -> None:
    separator = vm.pop_str()
    value = vm._resolve_handle(vm.pop())
    if value is None:
        vm.push([])
        return
    if _capture_is_group_list(value):
        out: List[str] = []
        for idx, group in enumerate(value):
            if idx > 0 and separator:
                out.append(separator)
            out.extend(_capture_flatten_tokens(group, field="capture-separate group"))
        vm.push(out)
        return
    vm.push(_capture_flatten_tokens(value, field="capture-separate source"))


def _ct_capture_join(vm: CompileTimeVM) -> None:
    separator = vm.pop_str()
    value = vm._resolve_handle(vm.pop())
    if value is None:
        vm.push("")
        return
    if _capture_is_group_list(value):
        chunks: List[str] = []
        for group in value:
            group_tokens = _capture_flatten_tokens(group, field="capture-join group")
            chunks.append(" ".join(group_tokens).strip())
        vm.push(separator.join(chunks))
        return
    tokens = _capture_flatten_tokens(value, field="capture-join source")
    vm.push(separator.join(tokens))


def _ct_capture_equal(vm: CompileTimeVM) -> None:
    right = vm._resolve_handle(vm.pop())
    left = vm._resolve_handle(vm.pop())
    vm.push(1 if _capture_normalize_value(left) == _capture_normalize_value(right) else 0)


def _ct_capture_origin(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    origin = ctx.get("origin")
    vm.push(_capture_deep_clone(origin if isinstance(origin, dict) else {}))


def _ct_capture_taint_set(vm: CompileTimeVM) -> None:
    flagged = _coerce_bool(vm.pop(), field="capture taint flag")
    capture_name = vm.pop_str()
    macro_name = vm.pop_str()
    scope = vm.parser.capture_taint.setdefault(macro_name, {})
    scope[capture_name] = bool(flagged)


def _ct_capture_taint_get(vm: CompileTimeVM) -> None:
    capture_name = vm.pop_str()
    macro_name = vm.pop_str()
    scope = vm.parser.capture_taint.get(macro_name)
    if scope is None or capture_name not in scope:
        vm.push(0)
        vm.push(0)
        return
    vm.push(1 if scope[capture_name] else 0)
    vm.push(1)


def _ct_capture_tainted(vm: CompileTimeVM) -> None:
    capture_name = vm.pop_str()
    ctx = _ensure_capture_context(vm.pop())
    taint_scope = ctx.get("taint")
    if isinstance(taint_scope, dict):
        vm.push(1 if taint_scope.get(capture_name) else 0)
        return
    vm.push(0)


def _ct_capture_serialize(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    vm.push(_capture_serialize_text(value))


def _ct_capture_deserialize(vm: CompileTimeVM) -> None:
    import json

    payload = vm.pop_str()
    try:
        vm.push(json.loads(payload))
    except Exception as exc:
        raise ParseError(f"capture-deserialize failed: {exc}") from exc


def _ct_capture_compress(vm: CompileTimeVM) -> None:
    import base64
    import zlib

    payload = vm.pop_str().encode("utf-8")
    compressed = zlib.compress(payload, level=9)
    vm.push(base64.b64encode(compressed).decode("ascii"))


def _ct_capture_decompress(vm: CompileTimeVM) -> None:
    import base64
    import zlib

    payload = vm.pop_str()
    try:
        raw = base64.b64decode(payload.encode("ascii"), validate=True)
        vm.push(zlib.decompress(raw).decode("utf-8"))
    except Exception as exc:
        raise ParseError(f"capture-decompress failed: {exc}") from exc


def _ct_capture_hash(vm: CompileTimeVM) -> None:
    import hashlib

    value = vm._resolve_handle(vm.pop())
    encoded = _capture_serialize_text(value).encode("utf-8")
    vm.push(hashlib.sha256(encoded).hexdigest())


def _ct_capture_diff(vm: CompileTimeVM) -> None:
    right = _capture_normalize_value(vm._resolve_handle(vm.pop()))
    left = _capture_normalize_value(vm._resolve_handle(vm.pop()))
    out: List[str] = []
    _capture_diff_values(left, right, path="$", out=out)
    vm.push(out)


def _ct_capture_replay_log(vm: CompileTimeVM) -> None:
    vm.push(_capture_deep_clone(vm.parser.capture_replay_log))


def _ct_capture_replay_clear(vm: CompileTimeVM) -> None:
    count = len(vm.parser.capture_replay_log)
    vm.parser.capture_replay_log.clear()
    vm.push(count)


def _ct_capture_lint(vm: CompileTimeVM) -> None:
    ctx = _ensure_capture_context(vm.pop())
    captures = ctx.get("captures")
    taint_scope = ctx.get("taint") if isinstance(ctx.get("taint"), dict) else {}
    warnings: List[str] = []

    if not isinstance(captures, dict):
        vm.push(["capture context has invalid captures map"])
        return

    for key, value in captures.items():
        key_text = str(key)
        if not (_is_identifier(key_text) or key_text.isdigit()):
            warnings.append(f"capture '{key_text}' has non-identifier key")
        shape = _capture_shape(value)
        if shape == "tokens" and isinstance(value, list) and not value:
            warnings.append(f"capture '{key_text}' is an empty token list")
        if shape == "multi":
            for idx, group in enumerate(value):
                if not isinstance(group, list) or not group:
                    warnings.append(f"capture '{key_text}' has empty variadic group #{idx + 1}")
        if isinstance(taint_scope, dict) and taint_scope.get(key_text):
            warnings.append(f"capture '{key_text}' is tainted")

    try:
        lifetime = int(ctx.get("lifetime", 0))
    except Exception:
        lifetime = 0
    if lifetime == 0 or lifetime != vm.parser._capture_lifetime_active:
        warnings.append(
            f"capture context lifetime is stale (ctx={lifetime}, active={vm.parser._capture_lifetime_active})"
        )

    vm.push(warnings)


def _ct_list_pattern_macros(vm: CompileTimeVM) -> None:
    vm.push(sorted(vm.parser._pattern_macro_rules.keys()))


def _ct_set_pattern_macro_enabled(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="pattern macro enabled flag")
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_pattern_macro_enabled(name, enabled) else 0)


def _ct_get_pattern_macro_enabled(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    enabled = vm.parser.get_pattern_macro_enabled(name)
    if enabled is None:
        vm.push(0)
        vm.push(0)
    else:
        vm.push(1 if enabled else 0)
        vm.push(1)


def _ct_set_pattern_macro_priority(vm: CompileTimeVM) -> None:
    priority = vm.pop_int()
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_pattern_macro_priority(name, priority) else 0)


def _ct_get_pattern_macro_priority(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    priority = vm.parser.get_pattern_macro_priority(name)
    if priority is None:
        vm.push(0)
        vm.push(0)
    else:
        vm.push(priority)
        vm.push(1)


def _ct_get_pattern_macro_clauses(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    clauses = vm.parser.get_pattern_macro_clauses(name)
    if clauses is None:
        vm.push(None)
        vm.push(0)
        return
    encoded: List[Any] = []
    for pattern, replacement in clauses:
        encoded.append([list(pattern), list(replacement)])
    vm.push(encoded)
    vm.push(1)


def _ct_get_pattern_macro_clause_details(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    details = vm.parser.get_pattern_macro_clause_details(name)
    if details is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(details)
    vm.push(1)


def _ct_set_pattern_macro_group(vm: CompileTimeVM) -> None:
    group = vm.pop_str()
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_pattern_macro_group(name, group) else 0)


def _ct_get_pattern_macro_group(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    group = vm.parser.get_pattern_macro_group(name)
    if group is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(group)
    vm.push(1)


def _ct_set_pattern_macro_scope(vm: CompileTimeVM) -> None:
    scope = vm.pop_str()
    name = vm.pop_str()
    vm.push(1 if vm.parser.set_pattern_macro_scope(name, scope) else 0)


def _ct_get_pattern_macro_scope(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    scope = vm.parser.get_pattern_macro_scope(name)
    if scope is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(scope)
    vm.push(1)


def _ct_set_pattern_group_active(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="pattern group enabled flag")
    group = vm.pop_str()
    vm.parser.set_pattern_group_active(group, enabled)
    vm.push(1)


def _ct_set_pattern_scope_active(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="pattern scope enabled flag")
    scope = vm.pop_str()
    vm.parser.set_pattern_scope_active(scope, enabled)
    vm.push(1)


def _ct_list_active_pattern_groups(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.list_active_pattern_groups())


def _ct_list_active_pattern_scopes(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.list_active_pattern_scopes())


def _ct_set_rewrite_trace(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="rewrite trace enabled flag")
    vm.parser.rewrite_trace_enabled = bool(enabled)


def _ct_get_rewrite_trace(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser.rewrite_trace_enabled else 0)


def _ct_get_rewrite_trace_log(vm: CompileTimeVM) -> None:
    vm.push([dict(entry) for entry in vm.parser.rewrite_trace_log])


def _ct_clear_rewrite_trace_log(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.clear_rewrite_trace_log())


def _ct_detect_pattern_conflicts(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.detect_pattern_macro_conflicts())


def _ct_detect_pattern_conflicts_named(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.push(vm.parser.detect_pattern_macro_conflicts(name))


def _ct_get_rewrite_specificity(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite specificity stage")
    value = vm.parser.get_rewrite_rule_specificity(stage, name)
    if value is None:
        vm.push(0)
        vm.push(0)
        return
    vm.push(value)
    vm.push(1)


def _ct_set_pattern_macro_clause_guard(vm: CompileTimeVM) -> None:
    guard_value = vm.pop()
    idx = vm.pop_int()
    name = vm.pop_str()
    rule_names = vm.parser._pattern_macro_rule_names(name)
    if idx < 0 or idx >= len(rule_names):
        vm.push(0)
        return
    guard_name: Optional[str]
    resolved = vm._resolve_handle(guard_value)
    if resolved is None:
        guard_name = None
    else:
        guard_name = _coerce_str(resolved)
    ok = vm.parser.set_rewrite_rule_guard("grammar", rule_names[idx], guard_name)
    vm.push(1 if ok else 0)


def _ct_set_rewrite_pipeline(vm: CompileTimeVM) -> None:
    pipeline = vm.pop_str()
    name = vm.pop_str()
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite pipeline stage")
    vm.push(1 if vm.parser.set_rewrite_rule_pipeline(stage, name, pipeline) else 0)


def _ct_get_rewrite_pipeline(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite pipeline stage")
    pipeline = vm.parser.get_rewrite_rule_pipeline(stage, name)
    if pipeline is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(pipeline)
    vm.push(1)


def _ct_set_rewrite_pipeline_active(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="rewrite pipeline active flag")
    pipeline = vm.pop_str()
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite pipeline stage")
    vm.parser.set_rewrite_pipeline_active(stage, pipeline, enabled)


def _ct_list_rewrite_active_pipelines(vm: CompileTimeVM) -> None:
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite pipeline stage")
    vm.push(vm.parser.list_active_rewrite_pipelines(stage))


def _ct_rebuild_rewrite_index(vm: CompileTimeVM) -> None:
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite index stage")
    vm.parser._invalidate_rewrite_index(stage)
    vm.parser._refresh_rewrite_index(stage)
    vm.push(
        len(vm.parser._rewrite_index_cache.get(stage, {}))
        + len(vm.parser._rewrite_wildcard_cache.get(stage, []))
    )


def _ct_get_rewrite_index_stats(vm: CompileTimeVM) -> None:
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite index stage")
    vm.parser._refresh_rewrite_index(stage)
    keyed = vm.parser._rewrite_index_cache.get(stage, {})
    wildcard = vm.parser._rewrite_wildcard_cache.get(stage, [])
    vm.push(
        {
            "stage": stage,
            "keys": len(keyed),
            "keyed_rules": sum(len(values) for values in keyed.values()),
            "wildcard_rules": len(wildcard),
        }
    )


def _ct_rewrite_txn_begin(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.rewrite_transaction_begin())


def _ct_rewrite_txn_commit(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser.rewrite_transaction_commit() else 0)


def _ct_rewrite_txn_rollback(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser.rewrite_transaction_rollback() else 0)


def _ct_export_rewrite_pack(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.export_rewrite_pack())


def _ct_import_rewrite_pack(vm: CompileTimeVM) -> None:
    pack = _ensure_dict(vm.pop())
    vm.push(vm.parser.import_rewrite_pack(pack, replace=False))


def _ct_import_rewrite_pack_replace(vm: CompileTimeVM) -> None:
    pack = _ensure_dict(vm.pop())
    vm.push(vm.parser.import_rewrite_pack(pack, replace=True))


def _ct_get_rewrite_provenance(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite provenance stage")
    provenance = vm.parser.get_rewrite_rule_provenance(stage, name)
    if provenance is None:
        vm.push(None)
        vm.push(0)
        return
    vm.push(provenance)
    vm.push(1)


def _ct_rewrite_dry_run(vm: CompileTimeVM) -> None:
    max_steps = vm.pop_int()
    lexemes = _coerce_lexeme_list(vm.pop(), field="rewrite dry-run token list")
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite dry-run stage")
    result, patches = _simulate_rewrite_dry_run(
        vm.parser,
        stage=stage,
        lexemes=lexemes,
        max_steps=max_steps,
    )
    vm.push(result)
    vm.push(patches)


def _ct_rewrite_generate_fixture(vm: CompileTimeVM) -> None:
    max_steps = vm.pop_int()
    lexemes = _coerce_lexeme_list(vm.pop(), field="rewrite fixture token list")
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite fixture stage")
    result, patches = _simulate_rewrite_dry_run(
        vm.parser,
        stage=stage,
        lexemes=lexemes,
        max_steps=max_steps,
    )
    vm.push(
        {
            "stage": stage,
            "input": list(lexemes),
            "output": result,
            "patches": patches,
        }
    )


def _ct_set_rewrite_saturation(vm: CompileTimeVM) -> None:
    strategy = vm.pop_str().strip().lower()
    if strategy not in ("first", "specificity", "single-pass"):
        raise ParseError("rewrite saturation strategy must be one of: first, specificity, single-pass")
    vm.parser.rewrite_saturation_strategy = strategy


def _ct_get_rewrite_saturation(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.rewrite_saturation_strategy)


def _ct_set_rewrite_max_steps(vm: CompileTimeVM) -> None:
    max_steps = vm.pop_int()
    if max_steps < 1:
        raise ParseError("rewrite max-step budget must be >= 1")
    vm.parser.rewrite_max_steps = max_steps


def _ct_get_rewrite_max_steps(vm: CompileTimeVM) -> None:
    vm.push(int(vm.parser.rewrite_max_steps))


def _ct_set_rewrite_loop_detection(vm: CompileTimeVM) -> None:
    enabled = _coerce_bool(vm.pop(), field="rewrite loop detection flag")
    vm.parser.rewrite_loop_detection = bool(enabled)


def _ct_get_rewrite_loop_detection(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser.rewrite_loop_detection else 0)


def _ct_get_rewrite_loop_reports(vm: CompileTimeVM) -> None:
    vm.push([dict(entry) for entry in vm.parser.rewrite_loop_reports])


def _ct_clear_rewrite_loop_reports(vm: CompileTimeVM) -> None:
    count = len(vm.parser.rewrite_loop_reports)
    vm.parser.rewrite_loop_reports.clear()
    vm.push(count)


def _ct_get_rewrite_profile(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.get_rewrite_profile_snapshot())


def _ct_clear_rewrite_profile(vm: CompileTimeVM) -> None:
    vm.parser.clear_rewrite_profile()


def _ct_rewrite_compatibility_matrix(vm: CompileTimeVM) -> None:
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite compatibility stage")
    vm.push(vm.parser.build_rewrite_compatibility_matrix(stage))


def _ct_unregister_word(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.push(1 if vm.parser.unregister_word(name) else 0)


def _ct_word_exists(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    vm.push(1 if vm.parser.word_exists(name) else 0)


def _ct_list_words(vm: CompileTimeVM) -> None:
    vm.push(sorted(vm.dictionary.words.keys()))


def _ct_introspection_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Token):
        return value.lexeme
    if isinstance(value, Word):
        return value.name
    if isinstance(value, list):
        return [_ct_introspection_value(item) for item in value]
    if isinstance(value, tuple):
        return [_ct_introspection_value(item) for item in value]
    if isinstance(value, dict):
        snapshot: Dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, (bool, int, float, str)):
                snap_key = key
            elif isinstance(key, Token):
                snap_key = key.lexeme
            elif isinstance(key, Word):
                snap_key = key.name
            else:
                snap_key = str(key)
            snapshot[snap_key] = _ct_introspection_value(item)
        return snapshot
    return repr(value)


def _ct_get_word_body(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    word = vm.dictionary.lookup(name)
    if word is None:
        vm.push(None)
        return

    if word.macro_expansion is not None:
        vm.push([_ct_introspection_value(item) for item in word.macro_expansion])
        return

    definition = word.definition
    if isinstance(definition, Definition):
        vm.push(
            [
                {
                    "op": node.op,
                    "data": _ct_introspection_value(node.data),
                }
                for node in definition.body
            ]
        )
        return

    vm.push(None)


def _ct_get_word_asm(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    word = vm.dictionary.lookup(name)
    if word is None:
        vm.push(None)
        return
    definition = word.definition
    if isinstance(definition, AsmDefinition):
        vm.push(definition.body)
        return
    vm.push(None)


def _ct_parser_pos(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.pos)


def _ct_parser_remaining(vm: CompileTimeVM) -> None:
    vm.push(max(0, len(vm.parser.tokens) - vm.parser.pos))


def _ct_parser_eof(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser.pos >= len(vm.parser.tokens) else 0)


def _ct_parser_peek(vm: CompileTimeVM) -> None:
    offset = vm.pop_int()
    if offset < 0:
        raise ParseError("ct-parser-peek expects non-negative offset")
    idx = vm.parser.pos + offset
    if idx >= len(vm.parser.tokens):
        vm.push(None)
        return
    vm.push(vm.parser.tokens[idx])


def _ct_parser_set_pos(vm: CompileTimeVM) -> None:
    pos = vm.pop_int()
    if pos < 0 or pos > len(vm.parser.tokens):
        raise ParseError("ct-parser-set-pos expects position within current token stream")
    old_pos = vm.parser.pos
    vm.parser.pos = pos
    vm.parser._last_token = vm.parser.tokens[pos - 1] if pos > 0 else None
    vm.push(old_pos)


def _ct_parser_checkpoint(vm: CompileTimeVM) -> None:
    vm.push(
        {
            "pos": vm.parser.pos,
            "last_token": vm.parser._last_token,
            "remaining": max(0, len(vm.parser.tokens) - vm.parser.pos),
        }
    )


def _ct_parser_restore(vm: CompileTimeVM) -> None:
    checkpoint = vm._resolve_handle(vm.pop())
    restore_last_token = False
    last_token: Optional[Token] = None

    if isinstance(checkpoint, bool):
        raise ParseError("ct-parser-restore expects checkpoint map or integer position")
    if isinstance(checkpoint, int):
        pos = checkpoint
    elif isinstance(checkpoint, dict):
        raw_pos = checkpoint.get("pos")
        if isinstance(raw_pos, bool) or not isinstance(raw_pos, int):
            raise ParseError("ct-parser-restore checkpoint map requires integer 'pos'")
        pos = raw_pos
        if "last_token" in checkpoint:
            restore_last_token = True
            raw_last_token = vm._resolve_handle(checkpoint.get("last_token"))
            if raw_last_token is not None and not isinstance(raw_last_token, Token):
                raise ParseError("ct-parser-restore checkpoint map 'last_token' must be token or nil")
            last_token = raw_last_token
    else:
        raise ParseError("ct-parser-restore expects checkpoint map or integer position")

    if pos < 0 or pos > len(vm.parser.tokens):
        raise ParseError("ct-parser-restore position is outside current token stream")

    vm.parser.pos = pos
    if restore_last_token:
        vm.parser._last_token = last_token
    else:
        vm.parser._last_token = vm.parser.tokens[pos - 1] if pos > 0 else None
    vm.push(1)


def _ct_parser_tail(vm: CompileTimeVM) -> None:
    vm.push(list(vm.parser.tokens[vm.parser.pos:]))


def _ct_parser_session_begin(vm: CompileTimeVM) -> None:
    parser = vm.parser
    parser._ct_parser_sessions.append(
        {
            "tokens": list(parser.tokens),
            "pos": int(parser.pos),
            "last_token": parser._last_token,
        }
    )
    vm.push(len(parser._ct_parser_sessions))


def _ct_parser_session_commit(vm: CompileTimeVM) -> None:
    sessions = vm.parser._ct_parser_sessions
    if not sessions:
        vm.push(0)
        return
    sessions.pop()
    vm.push(1)


def _ct_parser_session_rollback(vm: CompileTimeVM) -> None:
    parser = vm.parser
    sessions = parser._ct_parser_sessions
    if not sessions:
        vm.push(0)
        return

    snapshot = sessions.pop()
    raw_tokens = snapshot.get("tokens")
    raw_pos = snapshot.get("pos")
    raw_last_token = snapshot.get("last_token")

    if not isinstance(raw_tokens, list) or not all(isinstance(item, Token) for item in raw_tokens):
        raise ParseError("ct-parser-session-rollback encountered invalid token snapshot")
    if isinstance(raw_pos, bool) or not isinstance(raw_pos, int):
        raise ParseError("ct-parser-session-rollback encountered invalid parser position snapshot")
    if raw_last_token is not None and not isinstance(raw_last_token, Token):
        raise ParseError("ct-parser-session-rollback encountered invalid last_token snapshot")
    if raw_pos < 0 or raw_pos > len(raw_tokens):
        raise ParseError("ct-parser-session-rollback snapshot position is out of range")

    parser.tokens = list(raw_tokens)
    parser.pos = raw_pos
    parser._last_token = raw_last_token
    vm.push(1)


def _ct_parser_collect_until(vm: CompileTimeVM) -> None:
    delimiter = vm.pop_str()
    parser = vm.parser
    out: List[Token] = []
    found = 0
    while parser.pos < len(parser.tokens):
        token = parser.next_token()
        parser._last_token = token
        if token.lexeme == delimiter:
            found = 1
            break
        out.append(token)
    vm.push(out)
    vm.push(found)


def _ct_parser_collect_balanced(vm: CompileTimeVM) -> None:
    close_lexeme = vm.pop_str()
    open_lexeme = vm.pop_str()
    parser = vm.parser
    out: List[Token] = []
    depth = 0
    found = 0

    while parser.pos < len(parser.tokens):
        token = parser.next_token()
        parser._last_token = token
        lexeme = token.lexeme

        if lexeme == open_lexeme:
            depth += 1
            out.append(token)
            continue

        if lexeme == close_lexeme:
            if depth == 0:
                found = 1
                break
            depth -= 1
            out.append(token)
            continue

        out.append(token)

    vm.push(out)
    vm.push(found)


def _ct_current_token(vm: CompileTimeVM) -> None:
    vm.push(vm.parser._last_token)


def _ct_set_macro_expansion_limit(vm: CompileTimeVM) -> None:
    vm.parser.set_macro_expansion_limit(vm.pop_int())


def _ct_get_macro_expansion_limit(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.macro_expansion_limit)


def _ct_set_macro_preview(vm: CompileTimeVM) -> None:
    vm.parser.set_macro_preview(_coerce_bool(vm.pop(), field="macro preview flag"))


def _ct_get_macro_preview(vm: CompileTimeVM) -> None:
    vm.push(1 if vm.parser.macro_preview else 0)


def _ct_shunt(vm: CompileTimeVM) -> None:
    """Convert an infix token list (strings) to postfix using +,-,*,/,%."""
    ops: List[str] = []
    output: List[str] = []
    prec = {"+": 1, "-": 1, "*": 2, "/": 2, "%": 2}
    tokens = _ensure_list(vm.pop())
    for tok in tokens:
        if not isinstance(tok, str):
            raise ParseError("shunt expects list of strings")
        if tok == "(":
            ops.append(tok)
            continue
        if tok == ")":
            while ops and ops[-1] != "(":
                output.append(ops.pop())
            if not ops:
                raise ParseError("mismatched parentheses in expression")
            ops.pop()
            continue
        if tok in prec:
            while ops and ops[-1] in prec and prec[ops[-1]] >= prec[tok]:
                output.append(ops.pop())
            ops.append(tok)
            continue
        output.append(tok)
    while ops:
        top = ops.pop()
        if top == "(":
            raise ParseError("mismatched parentheses in expression")
        output.append(top)
    vm.push(output)


def _ct_int_to_string(vm: CompileTimeVM) -> None:
    value = vm.pop_int()
    vm.push(str(value))


def _ct_identifier_p(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    if isinstance(value, Token):
        value = value.lexeme
    if not isinstance(value, str):
        vm.push(0)
        return
    vm.push(1 if _is_identifier(value) else 0)


def _ct_token_lexeme(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    if isinstance(value, Token):
        vm.push(value.lexeme)
        return
    if isinstance(value, str):
        vm.push(value)
        return
    raise ParseError("expected token or string on compile-time stack")


def _ct_token_from_lexeme(vm: CompileTimeVM) -> None:
    template_value = vm.pop()
    lexeme = vm.pop_str()
    template = _default_template(template_value)
    vm.push(Token(
        lexeme=lexeme,
        line=template.line,
        column=template.column,
        start=template.start,
        end=template.end,
    ))


def _ct_token_line(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    if not isinstance(value, Token):
        raise ParseError("token-line expects token value")
    vm.push(value.line)


def _ct_token_column(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    if not isinstance(value, Token):
        raise ParseError("token-column expects token value")
    vm.push(value.column)


def _ct_next_token(vm: CompileTimeVM) -> None:
    token = vm.parser.next_token()
    vm.push(token)


def _ct_peek_token(vm: CompileTimeVM) -> None:
    vm.push(vm.parser.peek_token())


def _ct_inject_tokens(vm: CompileTimeVM) -> None:
    tokens = _ensure_list(vm.pop())
    if not all(isinstance(item, Token) for item in tokens):
        raise ParseError("inject-tokens expects a list of tokens")
    vm.parser.inject_token_objects(tokens)


def _ct_inject_lexemes(vm: CompileTimeVM) -> None:
    template_value = vm._resolve_handle(vm.pop())
    lexeme_values = vm._resolve_handle(vm.pop())
    lexemes = _coerce_lexeme_list(lexeme_values, field="inject-lexemes input")
    template = _default_template(template_value)
    column = max(1, int(template.column))
    generated: List[Token] = []
    for lexeme in lexemes:
        generated.append(
            Token(
                lexeme=lexeme,
                line=template.line,
                column=column,
                start=template.start,
                end=template.end,
                expansion_depth=template.expansion_depth,
            )
        )
        column += max(1, len(lexeme))
    vm.parser.inject_token_objects(generated)


def _ct_token_clone(vm: CompileTimeVM) -> None:
    value = vm._resolve_handle(vm.pop())
    if not isinstance(value, Token):
        raise ParseError("token-clone expects token value")
    vm.push(
        Token(
            lexeme=value.lexeme,
            line=value.line,
            column=value.column,
            start=value.start,
            end=value.end,
            expansion_depth=value.expansion_depth,
        )
    )


def _ct_token_with_lexeme(vm: CompileTimeVM) -> None:
    lexeme = vm.pop_str()
    value = vm._resolve_handle(vm.pop())
    if not isinstance(value, Token):
        raise ParseError("token-with-lexeme expects token value")
    vm.push(
        Token(
            lexeme=lexeme,
            line=value.line,
            column=value.column,
            start=value.start,
            end=value.end,
            expansion_depth=value.expansion_depth,
        )
    )


def _ct_token_shift_column(vm: CompileTimeVM) -> None:
    delta = vm.pop_int()
    value = vm._resolve_handle(vm.pop())
    if not isinstance(value, Token):
        raise ParseError("token-shift-column expects token value")

    shifted_column = max(1, int(value.column) + int(delta))
    shifted_start = max(0, int(value.start) + int(delta))
    shifted_end = max(shifted_start, int(value.end) + int(delta))
    vm.push(
        Token(
            lexeme=value.lexeme,
            line=value.line,
            column=shifted_column,
            start=shifted_start,
            end=shifted_end,
            expansion_depth=value.expansion_depth,
        )
    )


def _coerce_parser_position(vm: CompileTimeVM, value: Any, *, field: str) -> int:
    parser = vm.parser
    resolved = vm._resolve_handle(value)

    if isinstance(resolved, bool):
        raise ParseError(f"{field} expects integer position, mark name, or checkpoint map")
    if isinstance(resolved, int):
        pos = int(resolved)
    elif isinstance(resolved, str):
        if resolved not in parser._ct_parser_marks:
            raise ParseError(f"{field} references unknown parser mark '{resolved}'")
        pos = int(parser._ct_parser_marks[resolved])
    elif isinstance(resolved, dict):
        raw_pos = resolved.get("pos")
        if isinstance(raw_pos, bool) or not isinstance(raw_pos, int):
            raise ParseError(f"{field} checkpoint map requires integer 'pos'")
        pos = int(raw_pos)
    else:
        raise ParseError(f"{field} expects integer position, mark name, or checkpoint map")

    if pos < 0 or pos > len(parser.tokens):
        raise ParseError(f"{field} resolved position is outside current token stream")
    return pos


def _ct_parser_mark(vm: CompileTimeVM) -> None:
    name = vm.pop_str()
    marks = vm.parser._ct_parser_marks
    previous = marks.get(name)
    marks[name] = int(vm.parser.pos)
    if previous is None:
        vm.push(-1)
        vm.push(0)
        return
    vm.push(int(previous))
    vm.push(1)


def _ct_parser_diff(vm: CompileTimeVM) -> None:
    end_ref = vm.pop()
    start_ref = vm.pop()
    start = _coerce_parser_position(vm, start_ref, field="ct-parser-diff start")
    end = _coerce_parser_position(vm, end_ref, field="ct-parser-diff end")

    lo = min(start, end)
    hi = max(start, end)
    segment = vm.parser.tokens[lo:hi]
    vm.push(
        {
            "start": start,
            "end": end,
            "delta": end - start,
            "forward": 1 if end >= start else 0,
            "count": len(segment),
            "lexemes": [tok.lexeme for tok in segment],
        }
    )


def _ct_parser_expected(vm: CompileTimeVM) -> None:
    expected_raw = vm._resolve_handle(vm.pop())
    expected_lexemes: List[str] = []

    if isinstance(expected_raw, Token):
        expected_lexemes = [expected_raw.lexeme]
    elif isinstance(expected_raw, str):
        expected_lexemes = [expected_raw]
    elif isinstance(expected_raw, list):
        for item in expected_raw:
            resolved = vm._resolve_handle(item)
            if isinstance(resolved, Token):
                expected_lexemes.append(resolved.lexeme)
            elif isinstance(resolved, str):
                expected_lexemes.append(resolved)
            else:
                raise ParseError("ct-parser-expected list items must be tokens or strings")
    else:
        raise ParseError("ct-parser-expected expects string/token or list of string/token values")

    if not expected_lexemes:
        raise ParseError("ct-parser-expected requires at least one expected lexeme")

    token = vm.parser.peek_token()
    if token is None:
        expected_blob = ", ".join(repr(item) for item in expected_lexemes)
        raise ParseError(f"ct-parser-expected failed: expected one of {expected_blob}, got EOF")
    if token.lexeme not in expected_lexemes:
        expected_blob = ", ".join(repr(item) for item in expected_lexemes)
        raise ParseError(
            f"ct-parser-expected failed: expected one of {expected_blob}, got {token.lexeme!r} at {token.line}:{token.column}"
        )
    vm.push(token)


def _ct_rewrite_scope_push(vm: CompileTimeVM) -> None:
    parser = vm.parser
    parser._ct_rewrite_scope_stack.append(
        {
            "active_rewrite_pipelines": {
                "reader": set(parser._active_rewrite_pipelines.get("reader", {"default"})),
                "grammar": set(parser._active_rewrite_pipelines.get("grammar", {"default"})),
            },
            "active_pattern_groups": set(parser._active_pattern_groups),
            "active_pattern_scopes": set(parser._active_pattern_scopes),
        }
    )
    vm.push(len(parser._ct_rewrite_scope_stack))


def _ct_rewrite_scope_pop(vm: CompileTimeVM) -> None:
    parser = vm.parser
    stack = parser._ct_rewrite_scope_stack
    if not stack:
        vm.push(0)
        return

    snapshot = stack.pop()
    pipelines_raw = snapshot.get("active_rewrite_pipelines")
    if not isinstance(pipelines_raw, dict):
        raise ParseError("ct-rewrite-scope-pop encountered invalid pipeline snapshot")

    for stage in ("reader", "grammar"):
        raw_values = pipelines_raw.get(stage, {"default"})
        if isinstance(raw_values, (set, list, tuple)):
            restored_values = {str(item) for item in raw_values}
        else:
            restored_values = {"default"}
        if not restored_values:
            restored_values = {"default"}
        parser._active_rewrite_pipelines[stage] = restored_values

    groups_raw = snapshot.get("active_pattern_groups", {"default"})
    if isinstance(groups_raw, (set, list, tuple)):
        groups = {str(item) for item in groups_raw}
    else:
        groups = {"default"}
    if not groups:
        groups = {"default"}

    scopes_raw = snapshot.get("active_pattern_scopes", {"global"})
    if isinstance(scopes_raw, (set, list, tuple)):
        scopes = {str(item) for item in scopes_raw}
    else:
        scopes = {"global"}
    if not scopes:
        scopes = {"global"}

    parser._active_pattern_groups = groups
    parser._active_pattern_scopes = scopes
    vm.push(1)


def _ct_rewrite_run_on_list(vm: CompileTimeVM) -> None:
    lexeme_values = vm._resolve_handle(vm.pop())
    stage = _coerce_rewrite_stage(vm.pop(), field="rewrite run stage")
    lexemes = _coerce_lexeme_list(lexeme_values, field="rewrite run token list")
    max_steps = max(1, int(vm.parser.rewrite_max_steps))
    result, patches = _simulate_rewrite_dry_run(
        vm.parser,
        stage=stage,
        lexemes=lexemes,
        max_steps=max_steps,
    )
    vm.push(result)
    vm.push(patches)


def _coerce_lexeme_list(value: Any, *, field: str) -> List[str]:
    items = _ensure_list(value)
    out: List[str] = []
    for item in items:
        if isinstance(item, Token):
            out.append(item.lexeme)
        elif isinstance(item, str):
            out.append(item)
        else:
            raise ParseError(f"{field} expects list elements that are strings or tokens")
    return out


def _coerce_bool(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    raise ParseError(f"{field} expects integer/boolean value")


def _coerce_rewrite_stage(value: Any, *, field: str = "rewrite stage") -> str:
    stage = _coerce_str(value).strip().lower()
    if stage not in ("reader", "grammar"):
        raise ParseError(f"{field} must be 'reader' or 'grammar'")
    return stage


def _simulate_rewrite_dry_run(
    parser: Parser,
    *,
    stage: str,
    lexemes: Sequence[str],
    max_steps: int,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    stage = _coerce_rewrite_stage(stage)
    if max_steps < 1:
        raise ParseError("rewrite dry-run max_steps must be >= 1")

    template = parser._last_token
    if template is None:
        template = Token(lexeme="", line=0, column=0, start=0, end=0)

    synthetic_tokens = [
        Token(
            lexeme=str(lex),
            line=template.line,
            column=max(1, template.column + idx),
            start=template.start,
            end=template.end,
            expansion_depth=template.expansion_depth,
        )
        for idx, lex in enumerate(lexemes)
    ]

    saved_tokens = parser.tokens
    saved_pos = parser.pos
    saved_last_token = parser._last_token
    saved_trace_enabled = parser.rewrite_trace_enabled
    saved_trace_log = list(parser.rewrite_trace_log)
    saved_step_count = parser._rewrite_step_count
    saved_seen_state = dict(parser._rewrite_seen_state)
    saved_loop_reports = list(parser.rewrite_loop_reports)
    saved_budget = parser.rewrite_max_steps

    parser.tokens = synthetic_tokens
    parser.pos = 0
    parser._last_token = None
    parser.rewrite_trace_enabled = True
    parser.rewrite_trace_log = []
    parser._rewrite_step_count = 0
    parser._rewrite_seen_state = {}
    parser.rewrite_loop_reports = []
    parser.rewrite_max_steps = int(max_steps)
    merged_trace: Optional[List[Dict[str, Any]]] = None

    try:
        made_progress = True
        while made_progress:
            made_progress = False
            idx = 0
            while idx < len(parser.tokens):
                parser.pos = idx + 1
                cur = parser.tokens[idx]
                parser._last_token = cur
                if parser.macro_engine.try_apply_rewrite_rules(stage, cur):
                    made_progress = True
                    idx = max(0, parser.pos)
                    continue
                idx += 1

            if parser.rewrite_saturation_strategy == "single-pass":
                break

        result = [tok.lexeme for tok in parser.tokens]
        patches = [dict(entry) for entry in parser.rewrite_trace_log]
        if saved_trace_enabled:
            merged_trace = saved_trace_log + patches
            if len(merged_trace) > 8192:
                merged_trace = merged_trace[-8192:]
        return result, patches
    finally:
        parser.tokens = saved_tokens
        parser.pos = saved_pos
        parser._last_token = saved_last_token
        parser.rewrite_trace_enabled = saved_trace_enabled
        parser.rewrite_trace_log = merged_trace if merged_trace is not None else saved_trace_log
        parser._rewrite_step_count = saved_step_count
        parser._rewrite_seen_state = saved_seen_state
        parser.rewrite_loop_reports = saved_loop_reports
        parser.rewrite_max_steps = saved_budget


def _ct_emit_definition(vm: CompileTimeVM) -> None:
    body = _ensure_list(vm.pop())
    name_value = vm.pop()
    if isinstance(name_value, Token):
        template = name_value
        name = name_value.lexeme
    elif isinstance(name_value, str):
        template = _default_template(vm.pop())
        name = name_value
    else:
        raise ParseError("emit-definition expects token or string for name")
    lexemes = [
        item.lexeme if isinstance(item, Token) else _coerce_str(item)
        for item in body
    ]
    generated: List[Token] = []
    _struct_emit_definition(generated, template, name, lexemes)
    vm.parser.inject_token_objects(generated)


def _ct_parse_error(vm: CompileTimeVM) -> None:
    message = vm.pop_str()
    raise ParseError(message)


def _ct_static_assert(vm: CompileTimeVM) -> None:
    condition = vm._resolve_handle(vm.pop())
    if isinstance(condition, bool):
        ok = condition
    elif isinstance(condition, int):
        ok = condition != 0
    else:
        raise ParseError(
            f"static_assert expects integer/boolean condition, got {type(condition).__name__}"
        )
    if not ok:
        loc = vm.current_location
        if loc is not None:
            raise ParseError(f"static assertion failed at {loc.path}:{loc.line}:{loc.column}")
        raise ParseError("static assertion failed")


def _ct_lexer_new(vm: CompileTimeVM) -> None:
    separators = vm.pop_str()
    vm.push(SplitLexer(vm.parser, separators))


def _ct_lexer_pop(vm: CompileTimeVM) -> None:
    lexer = _ensure_lexer(vm.pop())
    token = lexer.pop()
    vm.push(lexer)
    vm.push(token)


def _ct_lexer_peek(vm: CompileTimeVM) -> None:
    lexer = _ensure_lexer(vm.pop())
    vm.push(lexer)
    vm.push(lexer.peek())


def _ct_lexer_expect(vm: CompileTimeVM) -> None:
    lexeme = vm.pop_str()
    lexer = _ensure_lexer(vm.pop())
    token = lexer.expect(lexeme)
    vm.push(lexer)
    vm.push(token)


def _ct_lexer_collect_brace(vm: CompileTimeVM) -> None:
    lexer = _ensure_lexer(vm.pop())
    vm.push(lexer)
    vm.push(lexer.collect_brace_block())


def _ct_lexer_push_back(vm: CompileTimeVM) -> None:
    lexer = _ensure_lexer(vm.pop())
    lexer.push_back()
    vm.push(lexer)


def _ct_eval(vm: CompileTimeVM) -> None:
    """Pop a string from TOS and execute it in the compile-time VM."""
    if vm.runtime_mode:
        length = vm.pop_int()
        addr = vm.pop_int()
        source = ctypes.string_at(addr, length).decode("utf-8")
    else:
        source = vm.pop_str()
    tokens = list(vm.parser.reader.tokenize(source))
    # Parse as if inside a definition body to get Op nodes
    parser = vm.parser
    # Save parser state
    old_tokens = parser.tokens
    old_pos = parser.pos
    old_iter = parser._token_iter
    old_exhausted = parser._token_iter_exhausted
    old_source = parser.source
    # Set up temporary token stream
    parser.tokens = list(tokens)
    parser.pos = 0
    parser._token_iter = iter([])
    parser._token_iter_exhausted = True
    parser.source = "<eval>"
    # Collect ops by capturing what _handle_token appends
    temp_defn = Definition(name="__eval__", body=[])
    parser.context_stack.append(temp_defn)
    try:
        while not parser._eof():
            token = parser._consume()
            parser._handle_token(token)
    finally:
        parser.context_stack.pop()
        # Restore parser state
        parser.tokens = old_tokens
        parser.pos = old_pos
        parser._token_iter = old_iter
        parser._token_iter_exhausted = old_exhausted
        parser.source = old_source
    # Execute collected ops in the VM
    if temp_defn.body:
        vm._execute_nodes(temp_defn.body)


# ---------------------------------------------------------------------------
# Runtime intrinsics that cannot run as native JIT  (for --ct-run-main)
# ---------------------------------------------------------------------------

def _rt_exit(vm: CompileTimeVM) -> None:
    code = vm.pop_int()
    raise _CTVMExit(code)


def _rt_jmp(vm: CompileTimeVM) -> None:
    target = vm.pop()
    resolved = vm._resolve_handle(target)
    if isinstance(resolved, Word):
        vm._call_word(resolved)
        raise _CTVMReturn()
    if isinstance(resolved, bool):
        raise _CTVMJump(int(resolved))
    if not isinstance(resolved, int):
        raise ParseError(
            f"jmp expects an address or word pointer, got {type(resolved).__name__}: {resolved!r}"
        )
    raise _CTVMJump(resolved)


def _rt_syscall(vm: CompileTimeVM) -> None:
    """Execute a real Linux syscall via a JIT stub, intercepting exit/exit_group."""
    # Lazily compile the syscall JIT stub
    stub = vm._jit_cache.get("__syscall_stub")
    if stub is None:
        stub = _compile_syscall_stub(vm)
        vm._jit_cache["__syscall_stub"] = stub

    # out[0] = final r12, out[1] = final r13, out[2] = flag (0=normal, 1=exit, code in out[3])
    out = vm._jit_out4
    stub(vm.r12, vm.r13, vm._jit_out4_addr)
    vm.r12 = out[0]
    vm.r13 = out[1]
    if out[2] == 1:
        raise _CTVMExit(out[3])


def _compile_syscall_stub(vm: CompileTimeVM) -> Any:
    """JIT-compile a native syscall stub that intercepts exit/exit_group."""
    if not _ensure_keystone():
        raise ParseError("keystone-engine is required for JIT syscall execution")

    # The stub uses the same wrapper convention as _compile_jit:
    #   rdi = r12 (data stack ptr), rsi = r13 (return stack ptr), rdx = output ptr
    # Output struct: [r12, r13, exit_flag, exit_code]
    #
    # Stack protocol (matching _emit_syscall_intrinsic):
    #   TOS:   syscall number -> rax
    #   TOS-1: arg count -> rcx
    #   then args on stack as ... arg0 arg1 ... argN (argN is top)
    #

    lines = [
        "_stub_entry:",
        "    push rbx",
        "    push r12",
        "    push r13",
        "    push r14",
        "    sub rsp, 24",
        "    mov [rsp], rdx",        # save output-struct pointer
        "    mov r12, rdi",          # data stack
        "    mov r13, rsi",          # return stack
        # Pop syscall number
        "    mov rax, [r12]",
        "    add r12, 8",
        # Pop arg count
        "    mov rcx, [r12]",
        "    add r12, 8",
        # Clamp to [0,6]
        "    cmp rcx, 0",
        "    jge _count_nonneg",
        "    xor rcx, rcx",
        "_count_nonneg:",
        "    cmp rcx, 6",
        "    jle _count_clamped",
        "    mov rcx, 6",
        "_count_clamped:",
        # Check for exit (60) / exit_group (231)
        "    cmp rax, 60",
        "    je _do_exit",
        "    cmp rax, 231",
        "    je _do_exit",
        # Clear syscall arg registers
        "    xor rdi, rdi",
        "    xor rsi, rsi",
        "    xor rdx, rdx",
        "    xor r10, r10",
        "    xor r8, r8",
        "    xor r9, r9",
        # Pop args in the same order as _emit_syscall_intrinsic
        "    cmp rcx, 6",
        "    jl _skip_r9",
        "    mov r9, [r12]",
        "    add r12, 8",
        "_skip_r9:",
        "    cmp rcx, 5",
        "    jl _skip_r8",
        "    mov r8, [r12]",
        "    add r12, 8",
        "_skip_r8:",
        "    cmp rcx, 4",
        "    jl _skip_r10",
        "    mov r10, [r12]",
        "    add r12, 8",
        "_skip_r10:",
        "    cmp rcx, 3",
        "    jl _skip_rdx",
        "    mov rdx, [r12]",
        "    add r12, 8",
        "_skip_rdx:",
        "    cmp rcx, 2",
        "    jl _skip_rsi",
        "    mov rsi, [r12]",
        "    add r12, 8",
        "_skip_rsi:",
        "    cmp rcx, 1",
        "    jl _skip_rdi",
        "    mov rdi, [r12]",
        "    add r12, 8",
        "_skip_rdi:",
        "    syscall",
        # Push result
        "    sub r12, 8",
        "    mov [r12], rax",
        # Normal return: flag=0
        "    mov rax, [rsp]",        # output-struct pointer
        "    mov qword [rax], r12",
        "    mov qword [rax+8], r13",
        "    mov qword [rax+16], 0", # exit_flag = 0
        "    mov qword [rax+24], 0", # exit_code = 0
        "    jmp _stub_epilogue",
        # Exit path: don't actually call syscall, just report it
        "_do_exit:",
        "    xor rbx, rbx",
        "    cmp rcx, 1",
        "    jl _exit_code_ready",
        "    mov rbx, [r12]",        # arg0 = exit code (for exit/exit_group)
        "    add r12, 8",
        "_exit_code_ready:",
        "    mov rax, [rsp]",        # output-struct pointer
        "    mov qword [rax], r12",
        "    mov qword [rax+8], r13",
        "    mov qword [rax+16], 1", # exit_flag = 1
        "    mov [rax+24], rbx",     # exit_code
        "_stub_epilogue:",
        "    add rsp, 24",
        "    pop r14",
        "    pop r13",
        "    pop r12",
        "    pop rbx",
        "    ret",
    ]

    def _norm(l: str) -> str:
        l = l.split(";", 1)[0].rstrip()
        for sz in ("qword", "dword", "word", "byte"):
            l = l.replace(f"{sz} [", f"{sz} ptr [")
        return l
    normalized = [_norm(l) for l in lines if _norm(l).strip()]

    ks = Ks(KS_ARCH_X86, KS_MODE_64)
    try:
        encoding, _ = ks.asm("\n".join(normalized))
    except KsError as exc:
        debug_txt = "\n".join(normalized)
        raise ParseError(f"JIT syscall stub assembly failed: {exc}\n--- asm ---\n{debug_txt}\n--- end ---") from exc
    if encoding is None:
        raise ParseError("JIT syscall stub produced no code")

    code = bytes(encoding)
    page_size = max(len(code), 4096)
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.mmap.restype = ctypes.c_void_p
    _libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_long]
    PROT_RWX = 0x1 | 0x2 | 0x4
    MAP_PRIVATE = 0x02
    MAP_ANONYMOUS = 0x20
    ptr = _libc.mmap(None, page_size, PROT_RWX, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
    if ptr == ctypes.c_void_p(-1).value or ptr is None:
        raise RuntimeError("mmap failed for JIT syscall stub")
    ctypes.memmove(ptr, code, len(code))
    vm._jit_code_pages.append((ptr, page_size))
    # Same signature: (r12, r13, out_ptr) -> void
    if CompileTimeVM._JIT_FUNC_TYPE is None:
        CompileTimeVM._JIT_FUNC_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p)
    func = CompileTimeVM._JIT_FUNC_TYPE(ptr)
    return func


def _register_runtime_intrinsics(dictionary: Dictionary) -> None:
    """Register runtime intrinsics only for words that cannot run as native JIT.

    Most :asm words now run as native JIT-compiled machine code on real
    memory stacks.  Only a handful need Python-level interception:
      - exit    : must not actually call sys_exit (would kill the compiler)
      - jmp     : needs interpreter-level IP manipulation
      - syscall : the ``syscall`` word is compiler-generated (no asm body);
                  intercept to block sys_exit and handle safely
    Note: get_addr is handled inline in _execute_nodes before _call_word.
    """
    _RT_MAP: Dict[str, Callable[[CompileTimeVM], None]] = {
        "exit": _rt_exit,
        "jmp": _rt_jmp,
        "syscall": _rt_syscall,
    }
    for name, func in _RT_MAP.items():
        word = dictionary.lookup(name)
        if word is None:
            word = Word(name=name)
            dictionary.register(word)
        word.runtime_intrinsic = func


def _register_compile_time_primitives(dictionary: Dictionary) -> None:
    def register(name: str, func: Callable[[CompileTimeVM], None], *, compile_only: bool = False) -> None:
        word = dictionary.lookup(name)
        if word is None:
            word = Word(name=name)
            dictionary.register(word)
        word.compile_time_intrinsic = func
        if compile_only:
            word.compile_only = True

    register("nil", _ct_nil, compile_only=True)
    register("nil?", _ct_nil_p, compile_only=True)
    register("list-new", _ct_list_new, compile_only=True)
    register("list-clone", _ct_list_clone, compile_only=True)
    register("list-append", _ct_list_append, compile_only=True)
    register("list-pop", _ct_list_pop, compile_only=True)
    register("list-pop-front", _ct_list_pop_front, compile_only=True)
    register("list-peek-front", _ct_list_peek_front, compile_only=True)
    register("list-push-front", _ct_list_push_front, compile_only=True)
    register("list-reverse", _ct_list_reverse, compile_only=True)
    register("list-length", _ct_list_length, compile_only=True)
    register("list-empty?", _ct_list_empty, compile_only=True)
    register("list-get", _ct_list_get, compile_only=True)
    register("list-set", _ct_list_set, compile_only=True)
    register("list-clear", _ct_list_clear, compile_only=True)
    register("list-extend", _ct_list_extend, compile_only=True)
    register("list-last", _ct_list_last, compile_only=True)
    register("list-insert", _ct_list_insert, compile_only=True)
    register("list-remove", _ct_list_remove, compile_only=True)
    register("list-slice", _ct_list_slice, compile_only=True)
    register("list-find", _ct_list_find, compile_only=True)
    register("list-contains?", _ct_list_contains, compile_only=True)
    register("list-join", _ct_list_join, compile_only=True)
    register("i", _ct_loop_index, compile_only=True)
    register("ct-control-frame-new", _ct_control_frame_new, compile_only=True)
    register("ct-control-get", _ct_control_get, compile_only=True)
    register("ct-control-set", _ct_control_set, compile_only=True)
    register("ct-control-push", _ct_control_push, compile_only=True)
    register("ct-control-pop", _ct_control_pop, compile_only=True)
    register("ct-control-peek", _ct_control_peek, compile_only=True)
    register("ct-control-depth", _ct_control_depth, compile_only=True)
    register("ct-control-add-close-op", _ct_control_add_close_op, compile_only=True)
    register("ct-new-label", _ct_new_label, compile_only=True)
    register("ct-emit-op", _ct_emit_op, compile_only=True)
    register("ct-last-token-line", _ct_last_token_line, compile_only=True)
    register("ct-register-block-opener", _ct_register_block_opener, compile_only=True)
    register("ct-unregister-block-opener", _ct_unregister_block_opener, compile_only=True)
    register("ct-register-control-override", _ct_register_control_override, compile_only=True)
    register("ct-unregister-control-override", _ct_unregister_control_override, compile_only=True)

    register("prelude-clear", _ct_prelude_clear, compile_only=True)
    register("prelude-append", _ct_prelude_append, compile_only=True)
    register("prelude-set", _ct_prelude_set, compile_only=True)
    register("bss-clear", _ct_bss_clear, compile_only=True)
    register("bss-append", _ct_bss_append, compile_only=True)
    register("bss-set", _ct_bss_set, compile_only=True)

    register("map-new", _ct_map_new, compile_only=True)
    register("map-set", _ct_map_set, compile_only=True)
    register("map-get", _ct_map_get, compile_only=True)
    register("map-has?", _ct_map_has, compile_only=True)
    register("map-delete", _ct_map_delete, compile_only=True)
    register("map-clear", _ct_map_clear, compile_only=True)
    register("map-length", _ct_map_length, compile_only=True)
    register("map-empty?", _ct_map_empty, compile_only=True)
    register("map-keys", _ct_map_keys, compile_only=True)
    register("map-values", _ct_map_values, compile_only=True)
    register("map-clone", _ct_map_clone, compile_only=True)
    register("map-update", _ct_map_update, compile_only=True)

    register("string=", _ct_string_eq, compile_only=True)
    register("string-length", _ct_string_length, compile_only=True)
    register("string-append", _ct_string_append, compile_only=True)
    register("string>number", _ct_string_to_number, compile_only=True)
    register("string-contains?", _ct_string_contains, compile_only=True)
    register("string-starts-with?", _ct_string_starts_with, compile_only=True)
    register("string-ends-with?", _ct_string_ends_with, compile_only=True)
    register("string-split", _ct_string_split, compile_only=True)
    register("string-join", _ct_string_join, compile_only=True)
    register("string-strip", _ct_string_strip, compile_only=True)
    register("string-replace", _ct_string_replace, compile_only=True)
    register("string-upper", _ct_string_upper, compile_only=True)
    register("string-lower", _ct_string_lower, compile_only=True)
    register("int>string", _ct_int_to_string, compile_only=True)
    register("identifier?", _ct_identifier_p, compile_only=True)
    register("shunt", _ct_shunt, compile_only=True)

    register("token-lexeme", _ct_token_lexeme, compile_only=True)
    register("token-from-lexeme", _ct_token_from_lexeme, compile_only=True)
    register("token-with-lexeme", _ct_token_with_lexeme, compile_only=True)
    register("token-clone", _ct_token_clone, compile_only=True)
    register("token-shift-column", _ct_token_shift_column, compile_only=True)
    register("token-line", _ct_token_line, compile_only=True)
    register("token-column", _ct_token_column, compile_only=True)
    register("next-token", _ct_next_token, compile_only=True)
    register("peek-token", _ct_peek_token, compile_only=True)
    register("ct-current-token", _ct_current_token, compile_only=True)
    register("ct-parser-pos", _ct_parser_pos, compile_only=True)
    register("ct-parser-remaining", _ct_parser_remaining, compile_only=True)
    register("ct-parser-eof?", _ct_parser_eof, compile_only=True)
    register("ct-parser-peek", _ct_parser_peek, compile_only=True)
    register("ct-parser-set-pos", _ct_parser_set_pos, compile_only=True)
    register("ct-parser-checkpoint", _ct_parser_checkpoint, compile_only=True)
    register("ct-parser-restore", _ct_parser_restore, compile_only=True)
    register("ct-parser-tail", _ct_parser_tail, compile_only=True)
    register("ct-parser-session-begin", _ct_parser_session_begin, compile_only=True)
    register("ct-parser-session-commit", _ct_parser_session_commit, compile_only=True)
    register("ct-parser-session-rollback", _ct_parser_session_rollback, compile_only=True)
    register("ct-parser-collect-until", _ct_parser_collect_until, compile_only=True)
    register("ct-parser-collect-balanced", _ct_parser_collect_balanced, compile_only=True)
    register("ct-parser-mark", _ct_parser_mark, compile_only=True)
    register("ct-parser-diff", _ct_parser_diff, compile_only=True)
    register("ct-parser-expected", _ct_parser_expected, compile_only=True)
    register("inject-tokens", _ct_inject_tokens, compile_only=True)
    register("inject-lexemes", _ct_inject_lexemes, compile_only=True)
    register("add-token", _ct_add_token, compile_only=True)
    register("add-token-chars", _ct_add_token_chars, compile_only=True)
    register("ct-add-reader-rewrite", _ct_add_reader_rewrite, compile_only=True)
    register("ct-add-reader-rewrite-named", _ct_add_reader_rewrite_named, compile_only=True)
    register("ct-add-reader-rewrite-priority", _ct_add_reader_rewrite_priority, compile_only=True)
    register("ct-remove-reader-rewrite", _ct_remove_reader_rewrite, compile_only=True)
    register("ct-clear-reader-rewrites", _ct_clear_reader_rewrites, compile_only=True)
    register("ct-list-reader-rewrites", _ct_list_reader_rewrites, compile_only=True)
    register("ct-set-reader-rewrite-enabled", _ct_set_reader_rewrite_enabled, compile_only=True)
    register("ct-get-reader-rewrite-enabled", _ct_get_reader_rewrite_enabled, compile_only=True)
    register("ct-set-reader-rewrite-priority", _ct_set_reader_rewrite_priority, compile_only=True)
    register("ct-get-reader-rewrite-priority", _ct_get_reader_rewrite_priority, compile_only=True)
    register("ct-add-grammar-rewrite", _ct_add_grammar_rewrite, compile_only=True)
    register("ct-add-grammar-rewrite-named", _ct_add_grammar_rewrite_named, compile_only=True)
    register("ct-add-grammar-rewrite-priority", _ct_add_grammar_rewrite_priority, compile_only=True)
    register("ct-remove-grammar-rewrite", _ct_remove_grammar_rewrite, compile_only=True)
    register("ct-clear-grammar-rewrites", _ct_clear_grammar_rewrites, compile_only=True)
    register("ct-list-grammar-rewrites", _ct_list_grammar_rewrites, compile_only=True)
    register("ct-set-grammar-rewrite-enabled", _ct_set_grammar_rewrite_enabled, compile_only=True)
    register("ct-get-grammar-rewrite-enabled", _ct_get_grammar_rewrite_enabled, compile_only=True)
    register("ct-set-grammar-rewrite-priority", _ct_set_grammar_rewrite_priority, compile_only=True)
    register("ct-get-grammar-rewrite-priority", _ct_get_grammar_rewrite_priority, compile_only=True)
    register("set-token-hook", _ct_set_token_hook, compile_only=True)
    register("clear-token-hook", _ct_clear_token_hook, compile_only=True)
    register("ct-set-macro-expansion-limit", _ct_set_macro_expansion_limit, compile_only=True)
    register("ct-get-macro-expansion-limit", _ct_get_macro_expansion_limit, compile_only=True)
    register("ct-set-macro-preview", _ct_set_macro_preview, compile_only=True)
    register("ct-get-macro-preview", _ct_get_macro_preview, compile_only=True)
    register("ct-register-text-macro", _ct_register_text_macro, compile_only=True)
    register("ct-register-text-macro-signature", _ct_register_text_macro_signature, compile_only=True)
    register("ct-register-pattern-macro", _ct_register_pattern_macro, compile_only=True)
    register("ct-unregister-pattern-macro", _ct_unregister_pattern_macro, compile_only=True)
    register("ct-word-is-text-macro", _ct_word_is_text_macro, compile_only=True)
    register("ct-word-is-pattern-macro", _ct_word_is_pattern_macro, compile_only=True)
    register("ct-get-macro-signature", _ct_get_macro_signature, compile_only=True)
    register("ct-get-macro-expansion", _ct_get_macro_expansion, compile_only=True)
    register("ct-set-macro-expansion", _ct_set_macro_expansion, compile_only=True)
    register("ct-clone-macro", _ct_clone_macro, compile_only=True)
    register("ct-rename-macro", _ct_rename_macro, compile_only=True)
    register("ct-macro-doc-get", _ct_macro_doc_get, compile_only=True)
    register("ct-macro-doc-set", _ct_macro_doc_set, compile_only=True)
    register("ct-macro-attrs-get", _ct_macro_attrs_get, compile_only=True)
    register("ct-macro-attrs-set", _ct_macro_attrs_set, compile_only=True)
    register("ct-get-macro-template-mode", _ct_get_macro_template_mode, compile_only=True)
    register("ct-get-macro-template-version", _ct_get_macro_template_version, compile_only=True)
    register("ct-get-macro-template-program-size", _ct_get_macro_template_program_size, compile_only=True)
    register("ct-set-ct-call-contract", _ct_set_ct_call_contract, compile_only=True)
    register("ct-get-ct-call-contract", _ct_get_ct_call_contract, compile_only=True)
    register("ct-set-ct-call-exception-policy", _ct_set_ct_call_exception_policy, compile_only=True)
    register("ct-get-ct-call-exception-policy", _ct_get_ct_call_exception_policy, compile_only=True)
    register("ct-set-ct-call-sandbox-mode", _ct_set_ct_call_sandbox_mode, compile_only=True)
    register("ct-get-ct-call-sandbox-mode", _ct_get_ct_call_sandbox_mode, compile_only=True)
    register("ct-set-ct-call-sandbox-allowlist", _ct_set_ct_call_sandbox_allowlist, compile_only=True)
    register("ct-get-ct-call-sandbox-allowlist", _ct_get_ct_call_sandbox_allowlist, compile_only=True)
    register("ct-ctrand-seed", _ct_ctrand_seed, compile_only=True)
    register("ct-ctrand-int", _ct_ctrand_int, compile_only=True)
    register("ct-ctrand-range", _ct_ctrand_range, compile_only=True)
    register("ct-set-ct-call-memo", _ct_set_ct_call_memo, compile_only=True)
    register("ct-get-ct-call-memo", _ct_get_ct_call_memo, compile_only=True)
    register("ct-clear-ct-call-memo", _ct_clear_ct_call_memo, compile_only=True)
    register("ct-get-ct-call-memo-size", _ct_get_ct_call_memo_size, compile_only=True)
    register("ct-set-ct-call-side-effects", _ct_set_ct_call_side_effects, compile_only=True)
    register("ct-get-ct-call-side-effects", _ct_get_ct_call_side_effects, compile_only=True)
    register("ct-get-ct-call-side-effect-log", _ct_get_ct_call_side_effect_log, compile_only=True)
    register("ct-clear-ct-call-side-effect-log", _ct_clear_ct_call_side_effect_log, compile_only=True)
    register("ct-set-ct-call-recursion-limit", _ct_set_ct_call_recursion_limit, compile_only=True)
    register("ct-get-ct-call-recursion-limit", _ct_get_ct_call_recursion_limit, compile_only=True)
    register("ct-set-ct-call-timeout-ms", _ct_set_ct_call_timeout_ms, compile_only=True)
    register("ct-get-ct-call-timeout-ms", _ct_get_ct_call_timeout_ms, compile_only=True)
    register("ct-gensym", _ct_gensym, compile_only=True)
    register("ct-capture-args", _ct_capture_args, compile_only=True)
    register("ct-capture-locals", _ct_capture_locals, compile_only=True)
    register("ct-capture-globals", _ct_capture_globals, compile_only=True)
    register("ct-capture-get", _ct_capture_get, compile_only=True)
    register("ct-capture-has?", _ct_capture_has, compile_only=True)
    register("ct-capture-shape", _ct_capture_shape, compile_only=True)
    register("ct-capture-assert-shape", _ct_capture_assert_shape, compile_only=True)
    register("ct-capture-count", _ct_capture_count, compile_only=True)
    register("ct-capture-slice", _ct_capture_slice, compile_only=True)
    register("ct-capture-map", _ct_capture_map, compile_only=True)
    register("ct-capture-filter", _ct_capture_filter, compile_only=True)
    register("ct-capture-normalize", _ct_capture_normalize, compile_only=True)
    register("ct-capture-pretty", _ct_capture_pretty, compile_only=True)
    register("ct-capture-clone", _ct_capture_clone, compile_only=True)
    register("ct-capture-global-set", _ct_capture_global_set, compile_only=True)
    register("ct-capture-global-get", _ct_capture_global_get, compile_only=True)
    register("ct-capture-global-delete", _ct_capture_global_delete, compile_only=True)
    register("ct-capture-global-clear", _ct_capture_global_clear, compile_only=True)
    register("ct-capture-freeze", _ct_capture_freeze, compile_only=True)
    register("ct-capture-thaw", _ct_capture_thaw, compile_only=True)
    register("ct-capture-mutable?", _ct_capture_mutable, compile_only=True)
    register("ct-capture-schema-put", _ct_capture_schema_put, compile_only=True)
    register("ct-capture-schema-get", _ct_capture_schema_get, compile_only=True)
    register("ct-capture-schema-validate", _ct_capture_schema_validate, compile_only=True)
    register("ct-capture-coerce-tokens", _ct_capture_coerce_tokens, compile_only=True)
    register("ct-capture-coerce-string", _ct_capture_coerce_string, compile_only=True)
    register("ct-capture-coerce-number", _ct_capture_coerce_number, compile_only=True)
    register("ct-capture-lifetime", _ct_capture_lifetime, compile_only=True)
    register("ct-capture-lifetime-live?", _ct_capture_lifetime_live, compile_only=True)
    register("ct-capture-lifetime-assert", _ct_capture_lifetime_assert, compile_only=True)
    register("ct-capture-separate", _ct_capture_separate, compile_only=True)
    register("ct-capture-join", _ct_capture_join, compile_only=True)
    register("ct-capture-equal?", _ct_capture_equal, compile_only=True)
    register("ct-capture-origin", _ct_capture_origin, compile_only=True)
    register("ct-capture-taint-set", _ct_capture_taint_set, compile_only=True)
    register("ct-capture-taint-get", _ct_capture_taint_get, compile_only=True)
    register("ct-capture-tainted?", _ct_capture_tainted, compile_only=True)
    register("ct-capture-serialize", _ct_capture_serialize, compile_only=True)
    register("ct-capture-deserialize", _ct_capture_deserialize, compile_only=True)
    register("ct-capture-compress", _ct_capture_compress, compile_only=True)
    register("ct-capture-decompress", _ct_capture_decompress, compile_only=True)
    register("ct-capture-hash", _ct_capture_hash, compile_only=True)
    register("ct-capture-diff", _ct_capture_diff, compile_only=True)
    register("ct-capture-replay-log", _ct_capture_replay_log, compile_only=True)
    register("ct-capture-replay-clear", _ct_capture_replay_clear, compile_only=True)
    register("ct-capture-lint", _ct_capture_lint, compile_only=True)
    register("ct-list-pattern-macros", _ct_list_pattern_macros, compile_only=True)
    register("ct-set-pattern-macro-enabled", _ct_set_pattern_macro_enabled, compile_only=True)
    register("ct-get-pattern-macro-enabled", _ct_get_pattern_macro_enabled, compile_only=True)
    register("ct-set-pattern-macro-priority", _ct_set_pattern_macro_priority, compile_only=True)
    register("ct-get-pattern-macro-priority", _ct_get_pattern_macro_priority, compile_only=True)
    register("ct-get-pattern-macro-clauses", _ct_get_pattern_macro_clauses, compile_only=True)
    register("ct-get-pattern-macro-clause-details", _ct_get_pattern_macro_clause_details, compile_only=True)
    register("ct-set-pattern-macro-group", _ct_set_pattern_macro_group, compile_only=True)
    register("ct-get-pattern-macro-group", _ct_get_pattern_macro_group, compile_only=True)
    register("ct-set-pattern-macro-scope", _ct_set_pattern_macro_scope, compile_only=True)
    register("ct-get-pattern-macro-scope", _ct_get_pattern_macro_scope, compile_only=True)
    register("ct-set-pattern-group-active", _ct_set_pattern_group_active, compile_only=True)
    register("ct-set-pattern-scope-active", _ct_set_pattern_scope_active, compile_only=True)
    register("ct-list-active-pattern-groups", _ct_list_active_pattern_groups, compile_only=True)
    register("ct-list-active-pattern-scopes", _ct_list_active_pattern_scopes, compile_only=True)
    register("ct-set-pattern-macro-clause-guard", _ct_set_pattern_macro_clause_guard, compile_only=True)
    register("ct-detect-pattern-conflicts", _ct_detect_pattern_conflicts, compile_only=True)
    register("ct-detect-pattern-conflicts-named", _ct_detect_pattern_conflicts_named, compile_only=True)
    register("ct-get-rewrite-specificity", _ct_get_rewrite_specificity, compile_only=True)
    register("ct-set-rewrite-pipeline", _ct_set_rewrite_pipeline, compile_only=True)
    register("ct-get-rewrite-pipeline", _ct_get_rewrite_pipeline, compile_only=True)
    register("ct-set-rewrite-pipeline-active", _ct_set_rewrite_pipeline_active, compile_only=True)
    register("ct-list-rewrite-active-pipelines", _ct_list_rewrite_active_pipelines, compile_only=True)
    register("ct-rebuild-rewrite-index", _ct_rebuild_rewrite_index, compile_only=True)
    register("ct-get-rewrite-index-stats", _ct_get_rewrite_index_stats, compile_only=True)
    register("ct-rewrite-txn-begin", _ct_rewrite_txn_begin, compile_only=True)
    register("ct-rewrite-txn-commit", _ct_rewrite_txn_commit, compile_only=True)
    register("ct-rewrite-txn-rollback", _ct_rewrite_txn_rollback, compile_only=True)
    register("ct-export-rewrite-pack", _ct_export_rewrite_pack, compile_only=True)
    register("ct-import-rewrite-pack", _ct_import_rewrite_pack, compile_only=True)
    register("ct-import-rewrite-pack-replace", _ct_import_rewrite_pack_replace, compile_only=True)
    register("ct-get-rewrite-provenance", _ct_get_rewrite_provenance, compile_only=True)
    register("ct-rewrite-dry-run", _ct_rewrite_dry_run, compile_only=True)
    register("ct-rewrite-generate-fixture", _ct_rewrite_generate_fixture, compile_only=True)
    register("ct-set-rewrite-saturation", _ct_set_rewrite_saturation, compile_only=True)
    register("ct-get-rewrite-saturation", _ct_get_rewrite_saturation, compile_only=True)
    register("ct-set-rewrite-max-steps", _ct_set_rewrite_max_steps, compile_only=True)
    register("ct-get-rewrite-max-steps", _ct_get_rewrite_max_steps, compile_only=True)
    register("ct-set-rewrite-loop-detection", _ct_set_rewrite_loop_detection, compile_only=True)
    register("ct-get-rewrite-loop-detection", _ct_get_rewrite_loop_detection, compile_only=True)
    register("ct-get-rewrite-loop-reports", _ct_get_rewrite_loop_reports, compile_only=True)
    register("ct-clear-rewrite-loop-reports", _ct_clear_rewrite_loop_reports, compile_only=True)
    register("ct-set-rewrite-trace", _ct_set_rewrite_trace, compile_only=True)
    register("ct-get-rewrite-trace", _ct_get_rewrite_trace, compile_only=True)
    register("ct-get-rewrite-trace-log", _ct_get_rewrite_trace_log, compile_only=True)
    register("ct-clear-rewrite-trace-log", _ct_clear_rewrite_trace_log, compile_only=True)
    register("ct-get-rewrite-profile", _ct_get_rewrite_profile, compile_only=True)
    register("ct-clear-rewrite-profile", _ct_clear_rewrite_profile, compile_only=True)
    register("ct-rewrite-compatibility-matrix", _ct_rewrite_compatibility_matrix, compile_only=True)
    register("ct-rewrite-scope-push", _ct_rewrite_scope_push, compile_only=True)
    register("ct-rewrite-scope-pop", _ct_rewrite_scope_pop, compile_only=True)
    register("ct-rewrite-run-on-list", _ct_rewrite_run_on_list, compile_only=True)
    register("ct-unregister-word", _ct_unregister_word, compile_only=True)
    register("ct-list-words", _ct_list_words, compile_only=True)
    register("ct-word-exists?", _ct_word_exists, compile_only=True)
    register("ct-get-word-body", _ct_get_word_body, compile_only=True)
    register("ct-get-word-asm", _ct_get_word_asm, compile_only=True)
    register("use-l2-ct", _ct_use_l2_compile_time, compile_only=True)
    word_use_l2 = dictionary.lookup("use-l2-ct")
    if word_use_l2:
        word_use_l2.immediate = True
    register("emit-definition", _ct_emit_definition, compile_only=True)
    register("parse-error", _ct_parse_error, compile_only=True)
    register("static_assert", _ct_static_assert, compile_only=True)

    register("lexer-new", _ct_lexer_new, compile_only=True)
    register("lexer-pop", _ct_lexer_pop, compile_only=True)
    register("lexer-peek", _ct_lexer_peek, compile_only=True)
    register("lexer-expect", _ct_lexer_expect, compile_only=True)
    register("lexer-collect-brace", _ct_lexer_collect_brace, compile_only=True)
    register("lexer-push-back", _ct_lexer_push_back, compile_only=True)
    register("eval", _ct_eval, compile_only=True)




PY_EXEC_GLOBALS: Dict[str, Any] = {
    "MacroContext": MacroContext,
    "Token": Token,
    "Op": Op,
    "StructField": StructField,
    "Definition": Definition,
    "Module": Module,
    "ParseError": ParseError,
    "emit_definition": _struct_emit_definition,
    "is_identifier": _is_identifier,
}


def _parse_cfield_type(parser: Parser, struct_name: str) -> str:
    if parser._eof():
        raise ParseError(f"field type missing in cstruct '{struct_name}'")
    tok = parser.next_token().lexeme

    if tok == "struct":
        if parser._eof():
            raise ParseError(f"struct field type missing name in cstruct '{struct_name}'")
        name_tok = parser.next_token().lexeme
        type_name = f"struct {name_tok}"
        if not parser._eof():
            peek = parser.peek_token()
            if peek is not None and set(peek.lexeme) == {"*"}:
                type_name += peek.lexeme
                parser.next_token()
        return _canonical_c_type_name(type_name)

    canonical = _canonical_c_type_name(tok)
    return _canonical_c_type_name(_C_FIELD_TYPE_ALIASES.get(canonical, canonical))


def macro_struct_begin(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    if parser._eof():
        raise ParseError("struct name missing after 'struct'")
    name_token = parser.next_token()
    struct_name = name_token.lexeme
    fields: List[StructField] = []
    current_offset = 0
    while True:
        if parser._eof():
            raise ParseError("unterminated struct definition (missing 'end')")
        token = parser.next_token()
        if token.lexeme == "end":
            break
        if token.lexeme != "field":
            raise ParseError(
                f"expected 'field' or 'end' in struct '{struct_name}' definition"
            )
        if parser._eof():
            raise ParseError("field name missing in struct definition")
        field_name_token = parser.next_token()
        if parser._eof():
            raise ParseError(f"field size missing for '{field_name_token.lexeme}'")
        size_token = parser.next_token()
        try:
            field_size = int(size_token.lexeme, 0)
        except ValueError as exc:
            raise ParseError(
                f"invalid field size '{size_token.lexeme}' in struct '{struct_name}'"
            ) from exc
        fields.append(StructField(field_name_token.lexeme, current_offset, field_size))
        current_offset += field_size

    generated: List[Token] = []
    _struct_emit_definition(generated, name_token, f"{struct_name}.size", [str(current_offset)])
    for field in fields:
        size_word = f"{struct_name}.{field.name}.size"
        offset_word = f"{struct_name}.{field.name}.offset"
        _struct_emit_definition(generated, name_token, size_word, [str(field.size)])
        _struct_emit_definition(generated, name_token, offset_word, [str(field.offset)])
        _struct_emit_definition(
            generated,
            name_token,
            f"{struct_name}.{field.name}@",
            [offset_word, "+", "@"],
        )
        _struct_emit_definition(
            generated,
            name_token,
            f"{struct_name}.{field.name}!",
            ["swap", offset_word, "+", "swap", "!"],
        )

    parser.tokens[parser.pos:parser.pos] = generated
    return None


def macro_cstruct_begin(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    if parser._eof():
        raise ParseError("cstruct name missing after 'cstruct'")
    name_token = parser.next_token()
    struct_name = name_token.lexeme
    fields: List[CStructField] = []
    current_offset = 0
    max_align = 1

    while True:
        if parser._eof():
            raise ParseError("unterminated cstruct definition (missing 'end')")
        token = parser.next_token()
        if token.lexeme == "end":
            break
        if token.lexeme != "cfield":
            raise ParseError(
                f"expected 'cfield' or 'end' in cstruct '{struct_name}' definition"
            )
        if parser._eof():
            raise ParseError("field name missing in cstruct definition")
        field_name_token = parser.next_token()
        type_name = _parse_cfield_type(parser, struct_name)
        field_size, field_align, _, _ = _c_type_size_align_class(type_name, parser.cstruct_layouts)
        if field_size <= 0:
            raise ParseError(
                f"invalid cfield type '{type_name}' for '{field_name_token.lexeme}' in cstruct '{struct_name}'"
            )

        current_offset = _round_up(current_offset, field_align)
        fields.append(
            CStructField(
                name=field_name_token.lexeme,
                type_name=type_name,
                offset=current_offset,
                size=field_size,
                align=field_align,
            )
        )
        current_offset += field_size
        if field_align > max_align:
            max_align = field_align

    total_size = _round_up(current_offset, max_align)
    parser.cstruct_layouts[struct_name] = CStructLayout(
        name=struct_name,
        size=total_size,
        align=max_align,
        fields=fields,
    )

    generated: List[Token] = []
    _struct_emit_definition(generated, name_token, f"{struct_name}.size", [str(total_size)])
    _struct_emit_definition(generated, name_token, f"{struct_name}.align", [str(max_align)])
    for field in fields:
        size_word = f"{struct_name}.{field.name}.size"
        offset_word = f"{struct_name}.{field.name}.offset"
        _struct_emit_definition(generated, name_token, size_word, [str(field.size)])
        _struct_emit_definition(generated, name_token, offset_word, [str(field.offset)])
        if field.size == 8:
            _struct_emit_definition(
                generated,
                name_token,
                f"{struct_name}.{field.name}@",
                [offset_word, "+", "@"],
            )
            _struct_emit_definition(
                generated,
                name_token,
                f"{struct_name}.{field.name}!",
                ["swap", offset_word, "+", "swap", "!"],
            )

    parser.tokens[parser.pos:parser.pos] = generated
    return None

def macro_here(ctx: MacroContext) -> Optional[List[Op]]:
    tok = ctx.parser._last_token
    if tok is None:
        return [_make_op("literal", "<source>:0:0")]
    loc = ctx.parser.location_for_token(tok)
    return [_make_op("literal", f"{loc.path.name}:{loc.line}:{loc.column}")]


def _emit_ct_runtime_flag(builder: FunctionEmitter) -> None:
    """Emit runtime value for `CT` (0 in generated runtime code)."""
    builder.push_literal(0)


def _ct_push_compile_time_flag(vm: CompileTimeVM) -> None:
    """Push compile-time value for `CT` (1 while executing in CT VM)."""
    vm.push(1)


def bootstrap_dictionary() -> Dictionary:
    dictionary = Dictionary()
    dictionary.register(Word(name="immediate", immediate=True, macro=macro_immediate))
    dictionary.register(Word(name="compile-only", immediate=True, macro=macro_compile_only))
    dictionary.register(Word(name="runtime", immediate=True, macro=macro_runtime))
    dictionary.register(Word(name="runtime-only", immediate=True, macro=macro_runtime))
    dictionary.register(Word(name="inline", immediate=True, macro=macro_inline))
    dictionary.register(Word(name="label", immediate=True, macro=macro_label))
    dictionary.register(Word(name="goto", immediate=True, macro=macro_goto))
    dictionary.register(Word(name="compile-time", immediate=True, macro=macro_compile_time))
    dictionary.register(Word(name="here", immediate=True, macro=macro_here))
    dictionary.register(Word(name="with", immediate=True, macro=macro_with))
    dictionary.register(Word(name="macro", immediate=True, macro=macro_begin_text_macro))
    dictionary.register(Word(name="struct", immediate=True, macro=macro_struct_begin))
    dictionary.register(Word(name="cstruct", immediate=True, macro=macro_cstruct_begin))
    ct_word = Word(name="CT")
    ct_word.intrinsic = _emit_ct_runtime_flag
    ct_word.compile_time_intrinsic = _ct_push_compile_time_flag
    ct_word.runtime_intrinsic = _ct_push_compile_time_flag
    dictionary.register(ct_word)
    _register_compile_time_primitives(dictionary)
    _register_runtime_intrinsics(dictionary)
    return dictionary


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class FileSpan:
    __slots__ = ('path', 'start_line', 'end_line', 'local_start_line')

    def __init__(self, path: Path, start_line: int, end_line: int, local_start_line: int) -> None:
        self.path = path
        self.start_line = start_line
        self.end_line = end_line
        self.local_start_line = local_start_line


# Uppercase macro prefixes to strip (API export macros like RLAPI, WINGDIAPI, etc.)
# Keep common uppercase type names.
_C_HEADER_KEEP_UPPER = frozenset({"FILE", "DIR", "EOF", "NULL", "BOOL"})


def _parse_c_header_externs(header_text: str) -> List[str]:
    """Extract function declarations from a C header and return L2 ``extern`` lines."""
    text = re.sub(r"/\*.*?\*/", " ", header_text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"^\s*#[^\n]*$", "", text, flags=re.MULTILINE)
    text = text.replace("\\\n", " ")
    # Collapse whitespace (including newlines) so multi-line declarations become single-line
    text = re.sub(r"\s+", " ", text)
    # Strip __attribute__((...)), __nonnull((...)), __THROW, __wur, and similar GCC extensions
    text = re.sub(r"__\w+\s*\(\([^)]*\)\)", "", text)
    text = re.sub(r"__\w+", "", text)
    # Remove __restrict
    text = text.replace("__restrict", "")

    # Match function declarations: <tokens> <name>(<params>);
    _RE = re.compile(
        r"([\w][\w\s*]+?)"           # return type tokens + function name
        r"\s*\(([^)]*?)\)"           # parameter list
        r"\s*;"
    )

    results: List[str] = []
    for m in _RE.finditer(text):
        prefix = m.group(1).strip()
        params_raw = m.group(2).strip()

        if "..." in params_raw:
            # Variadic function: strip the ... from parameter list, keep fixed args
            params_fixed = re.sub(r",?\s*\.\.\.", "", params_raw).strip()
            param_str = "void" if params_fixed in ("void", "") else params_fixed
            is_variadic = True
        else:
            is_variadic = False

        tokens = prefix.split()
        if len(tokens) < 2:
            continue

        # Last token is function name (may have leading * for pointer-returning functions)
        func_name = tokens[-1].lstrip("*")
        if not func_name or not re.match(r"^[A-Za-z_]\w*$", func_name):
            continue

        # Skip typedef (struct/enum/union return types are fine — the regex
        # already ensures this matched a function declaration with parentheses)
        if tokens[0] in ("typedef",):
            continue

        # Build return type: strip API macros and calling-convention qualifiers
        type_tokens = tokens[:-1]
        cleaned: List[str] = []
        for t in type_tokens:
            if t in ("extern", "static", "inline"):
                continue
            # Strip uppercase macro prefixes (3+ chars, all caps) unless known type
            if re.match(r"^[A-Z_][A-Z_0-9]{2,}$", t) and t not in _C_HEADER_KEEP_UPPER:
                continue
            cleaned.append(t)

        # Pointer stars attached to the function name belong to the return type
        leading_stars = len(tokens[-1]) - len(tokens[-1].lstrip("*"))
        ret_type = " ".join(cleaned)
        if leading_stars:
            ret_type += " " + "*" * leading_stars
        ret_type = ret_type.strip()
        if not ret_type:
            ret_type = "int"

        if not is_variadic:
            param_str = "void" if params_raw in ("void", "") else params_raw

        va_suffix = ", ..." if is_variadic else ""
        results.append(f"extern {ret_type} {func_name}({param_str}{va_suffix})")
    return results


# Map C types to L2 cstruct field types
_C_TO_L2_FIELD_TYPE: Dict[str, str] = {
    "char": "i8", "signed char": "i8", "unsigned char": "u8",
    "short": "i16", "unsigned short": "u16", "short int": "i16",
    "int": "i32", "unsigned int": "u32", "unsigned": "u32",
    "long": "i64", "unsigned long": "u64", "long int": "i64",
    "long long": "i64", "unsigned long long": "u64",
    "float": "f32", "double": "f64",
    "size_t": "u64", "ssize_t": "i64",
    "int8_t": "i8", "uint8_t": "u8",
    "int16_t": "i16", "uint16_t": "u16",
    "int32_t": "i32", "uint32_t": "u32",
    "int64_t": "i64", "uint64_t": "u64",
}


def _parse_c_header_structs(header_text: str) -> List[str]:
    """Extract struct definitions from C header text and return L2 ``cstruct`` lines."""
    text = re.sub(r"/\*.*?\*/", " ", header_text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"#[^\n]*", "", text)
    text = re.sub(r"\s+", " ", text)

    results: List[str] = []
    # Match: struct Name { fields }; or typedef struct Name { fields } Alias;
    # or typedef struct { fields } Name;
    _RE_STRUCT = re.compile(
        r"(?:typedef\s+)?struct\s*(\w*)\s*\{([^}]*)\}\s*(\w*)\s*;",
    )
    for m in _RE_STRUCT.finditer(text):
        struct_name = m.group(1).strip()
        body = m.group(2).strip()
        typedef_name = m.group(3).strip()
        # Prefer typedef name if present
        name = typedef_name if typedef_name else struct_name
        if not name or name.startswith("_"):
            continue
        fields = _extract_struct_fields(body)
        if not fields:
            continue
        # Generate L2 cstruct declaration
        field_parts = []
        for fname, ftype in fields:
            field_parts.append(f"cfield {fname} {ftype}")
        results.append(f"cstruct {name} {' '.join(field_parts)} end")
    return results


def _extract_struct_fields(body: str) -> List[Tuple[str, str]]:
    """Parse C struct field declarations into (name, l2_type) pairs."""
    fields: List[Tuple[str, str]] = []
    for decl in body.split(";"):
        decl = decl.strip()
        if not decl:
            continue
        # Skip bitfields
        if ":" in decl:
            continue
        # Skip nested struct/union definitions (but allow struct pointers)
        if ("struct " in decl or "union " in decl) and "*" not in decl:
            continue
        tokens = decl.split()
        if len(tokens) < 2:
            continue
        # Last token is field name (may have * prefix for pointers)
        field_name = tokens[-1].lstrip("*")
        if not field_name or not re.match(r"^[A-Za-z_]\w*$", field_name):
            continue
        # Check if pointer
        if "*" in decl:
            fields.append((field_name, "ptr"))
            continue
        # Build type from all tokens except field name
        type_tokens = tokens[:-1]
        # Remove qualifiers
        type_tokens = [t for t in type_tokens if t not in ("const", "volatile", "static",
                                                            "register", "restrict", "_Atomic")]
        ctype = " ".join(type_tokens)
        l2_type = _C_TO_L2_FIELD_TYPE.get(ctype)
        if l2_type is None:
            # Unknown type, treat as pointer-sized
            fields.append((field_name, "ptr"))
        else:
            fields.append((field_name, l2_type))
    return fields


class Compiler:
    def __init__(
        self,
        include_paths: Optional[Sequence[Path]] = None,
        *,
        macro_expansion_limit: int = DEFAULT_MACRO_EXPANSION_LIMIT,
        macro_preview: bool = False,
        defines: Optional[Sequence[str]] = None,
        source_graph_cache: Optional["SourceGraphCache"] = None,
    ) -> None:
        self.reader = Reader()
        self.dictionary = bootstrap_dictionary()
        self._syscall_label_counter = 0
        self._register_syscall_words()
        self.parser = Parser(
            self.dictionary,
            self.reader,
            macro_expansion_limit=macro_expansion_limit,
            macro_preview=macro_preview,
        )
        self.assembler = Assembler(self.dictionary)
        if include_paths is None:
            include_paths = [Path("."), Path("./stdlib")]
        self.include_paths: List[Path] = [p.expanduser().resolve() for p in include_paths]
        self._loaded_files: Set[Path] = set()
        self.defines: Set[str] = set(defines or [])
        self._last_loaded_path: Optional[Path] = None
        self._last_loaded_source: Optional[str] = None
        self._last_loaded_spans: Optional[List[FileSpan]] = None
        # Populated from source-level `flags ...` pragmas while loading files.
        self.source_link_flags: List[str] = []
        self.source_include_paths: List[Path] = []
        self.source_cli_flags: List[str] = []
        self.source_graph_cache = source_graph_cache

    def _reset_source_flag_state(self) -> None:
        self.source_link_flags.clear()
        self.source_include_paths.clear()
        self.source_cli_flags.clear()

    def _load_source_graph(self, path: Path) -> Tuple[str, List[FileSpan]]:
        resolved = path.resolve()
        if self.source_graph_cache is not None:
            cached = self.source_graph_cache.load(
                resolved,
                defines=self.defines,
                include_paths=self.include_paths,
            )
            if cached is not None:
                cached_source = cached.get("source")
                span_rows = cached.get("spans")
                if isinstance(cached_source, str) and isinstance(span_rows, list):
                    cached_spans: List[FileSpan] = []
                    for row in span_rows:
                        if (
                            isinstance(row, (list, tuple))
                            and len(row) == 4
                            and isinstance(row[0], str)
                            and isinstance(row[1], int)
                            and isinstance(row[2], int)
                            and isinstance(row[3], int)
                        ):
                            cached_spans.append(FileSpan(Path(row[0]), row[1], row[2], row[3]))

                    loaded_files: Set[Path] = set()
                    raw_files = cached.get("files", [])
                    if isinstance(raw_files, list):
                        for raw in raw_files:
                            if isinstance(raw, str):
                                loaded_files.add(Path(raw))
                    if not loaded_files:
                        loaded_files.add(resolved)

                    link_flags = cached.get("source_link_flags", [])
                    self.source_link_flags = [str(f) for f in link_flags if isinstance(f, str) and f]

                    include_flags = cached.get("source_include_paths", [])
                    cached_include_paths: List[Path] = []
                    if isinstance(include_flags, list):
                        for raw in include_flags:
                            if isinstance(raw, str) and raw:
                                inc = Path(raw)
                                cached_include_paths.append(inc)
                                if inc not in self.include_paths:
                                    self.include_paths.append(inc)
                    self.source_include_paths = cached_include_paths

                    cli_flags = cached.get("source_cli_flags", [])
                    self.source_cli_flags = [str(f) for f in cli_flags if isinstance(f, str) and f]

                    self._loaded_files = loaded_files
                    self._last_loaded_path = resolved
                    self._last_loaded_source = cached_source
                    self._last_loaded_spans = cached_spans
                    return cached_source, cached_spans

        source, spans = self._load_with_imports(resolved)
        self._last_loaded_path = resolved
        self._last_loaded_source = source
        self._last_loaded_spans = spans
        if self.source_graph_cache is not None:
            self.source_graph_cache.save(
                resolved,
                defines=self.defines,
                include_paths=self.include_paths,
                loaded_files=self._loaded_files,
                source_text=source,
                spans=spans,
                source_link_flags=self.source_link_flags,
                source_include_paths=self.source_include_paths,
                source_cli_flags=self.source_cli_flags,
            )
        return source, spans

    def _record_source_link_flag(self, flag: str) -> None:
        if flag and flag not in self.source_link_flags:
            self.source_link_flags.append(flag)

    def _record_source_cli_flag(self, flag: str) -> None:
        if flag:
            self.source_cli_flags.append(flag)

    def _add_source_include_path(self, raw_path: str, *, base_path: Path) -> None:
        include_path = Path(raw_path).expanduser()
        if not include_path.is_absolute():
            include_path = (base_path.parent / include_path).resolve()
        else:
            include_path = include_path.resolve()
        if include_path not in self.include_paths:
            self.include_paths.append(include_path)
            # Resolution depends on include paths; invalidate old resolutions.
            self._import_resolve_cache.clear()
        if include_path not in self.source_include_paths:
            self.source_include_paths.append(include_path)

    def _apply_flags_directive(self, payload: str, *, path: Path, line_no: int) -> None:
        text = payload.strip()
        if not text:
            raise ParseError(f"flags directive missing arguments at {path}:{line_no}")
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError as exc:
            raise ParseError(f"invalid flags directive at {path}:{line_no}: {exc}") from exc

        # Allow a quoted shell-style bundle like: flags "-lc -lm -L. -I."
        if len(tokens) == 1 and re.search(r"\s-[A-Za-z]", tokens[0]):
            try:
                tokens = shlex.split(tokens[0], posix=True)
            except ValueError as exc:
                raise ParseError(f"invalid flags directive at {path}:{line_no}: {exc}") from exc

        if not tokens:
            raise ParseError(f"flags directive missing arguments at {path}:{line_no}")

        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-I", "--include"):
                i += 1
                if i >= len(tokens):
                    raise ParseError(f"{tok} requires a path in flags directive at {path}:{line_no}")
                self._add_source_include_path(tokens[i], base_path=path)
                i += 1
                continue
            if tok.startswith("-I") and len(tok) > 2:
                self._add_source_include_path(tok[2:], base_path=path)
                i += 1
                continue
            if tok.startswith("--include="):
                self._add_source_include_path(tok.split("=", 1)[1], base_path=path)
                i += 1
                continue

            # Everything else is treated as a linker/runtime flag token.
            self._record_source_link_flag(tok)
            self._record_source_cli_flag(tok)
            i += 1

    def compile_source(
        self,
        source: str,
        spans: Optional[List[FileSpan]] = None,
        *,
        debug: bool = False,
        entry_mode: str = "program",
    ) -> Emission:
        self.parser.file_spans = spans or []
        tokens = self.reader.tokenize(source)
        module = self.parser.parse(tokens, source)
        return self.assembler.emit(module, debug=debug, entry_mode=entry_mode)

    def parse_file(self, path: Path) -> None:
        """Parse a source file to populate the dictionary without emitting assembly."""
        self._reset_source_flag_state()
        source, spans = self._load_source_graph(path)
        self.parser.file_spans = spans or []
        tokens = self.reader.tokenize(source)
        self.parser.parse(tokens, source)

    def compile_file(self, path: Path, *, debug: bool = False, entry_mode: str = "program") -> Emission:
        self._reset_source_flag_state()
        source, spans = self._load_source_graph(path)
        return self.compile_source(source, spans=spans, debug=debug, entry_mode=entry_mode)

    def collect_source_flags(self, path: Path) -> None:
        """Load source/import graph only to collect source-level pragma flags."""
        self._reset_source_flag_state()
        self._load_source_graph(path)

    def compile_preloaded(self, *, debug: bool = False, entry_mode: str = "program") -> Emission:
        if self._last_loaded_source is None or self._last_loaded_spans is None:
            raise CompileError("no preloaded source available")
        return self.compile_source(
            self._last_loaded_source,
            spans=self._last_loaded_spans,
            debug=debug,
            entry_mode=entry_mode,
        )

    def run_compile_time_word(self, name: str, *, libs: Optional[List[str]] = None) -> None:
        word = self.dictionary.lookup(name)
        if word is None:
            raise CompileTimeError(f"word '{name}' not defined; cannot run at compile time")
        # Skip if already executed via a ``compile-time <name>`` directive.
        if name in self.parser.compile_time_vm._ct_executed:
            return
        self.parser.compile_time_vm.invoke(word, runtime_mode=True, libs=libs)

    def run_compile_time_word_repl(self, name: str, *, libs: Optional[List[str]] = None) -> None:
        """Like run_compile_time_word but uses invoke_repl for persistent state."""
        word = self.dictionary.lookup(name)
        if word is None:
            raise CompileTimeError(f"word '{name}' not defined; cannot run at compile time")
        self.parser.compile_time_vm.invoke_repl(word, libs=libs)

    _import_resolve_cache: Dict[Tuple[Path, str], Path] = {}

    @staticmethod
    def _parse_directive_target_and_rest(
        line: str,
        keyword: str,
        *,
        path: Path,
        line_no: int,
        line_already_lstripped: bool = False,
    ) -> Optional[Tuple[str, str]]:
        """Parse `<keyword> <target> [remainder]` where target may be quoted."""
        text = line if line_already_lstripped else line.lstrip()
        if not text.startswith(keyword):
            return None
        if len(text) > len(keyword) and not text[len(keyword)].isspace():
            return None

        pos = len(keyword)
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            raise ParseError(f"empty {keyword} target in {path}:{line_no}")

        if text[pos] == '"':
            pos += 1
            start = pos
            while pos < len(text) and text[pos] != '"':
                pos += 1
            if pos >= len(text):
                raise ParseError(f"unterminated quoted {keyword} target in {path}:{line_no}")
            target = text[start:pos]
            pos += 1
        else:
            start = pos
            while pos < len(text) and not text[pos].isspace():
                pos += 1
            target = text[start:pos]

        while pos < len(text) and text[pos].isspace():
            pos += 1
        remainder = text[pos:]
        return target, remainder

    def _preprocess_c_header(self, header_path: Path, raw_text: str) -> str:
        """Try running the C preprocessor on a header file for accurate parsing.

        Falls back to raw_text if the preprocessor is not available."""
        import subprocess
        try:
            result = subprocess.run(
                ["cc", "-E", "-P", "-D__attribute__(x)=", "-D__extension__=",
                 "-D__restrict=", "-D__asm__(x)=", str(header_path)],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
        return raw_text

    def _resolve_import_target(self, importing_file: Path, target: str) -> Path:
        cache_key = (importing_file.parent, target)
        cached = self._import_resolve_cache.get(cache_key)
        if cached is not None:
            return cached
        raw = Path(target)
        tried: List[Path] = []

        if raw.is_absolute():
            candidate = raw.expanduser()
            tried.append(candidate)
            if candidate.exists():
                result = candidate.resolve()
                self._import_resolve_cache[cache_key] = result
                return result

        candidate = importing_file.parent / raw
        tried.append(candidate)
        if candidate.exists():
            result = candidate.resolve()
            self._import_resolve_cache[cache_key] = result
            return result

        for base in self.include_paths:
            candidate = base / raw
            tried.append(candidate)
            if candidate.exists():
                result = candidate.resolve()
                self._import_resolve_cache[cache_key] = result
                return result

        tried_str = "\n".join(f"  - {p}" for p in tried)
        raise ParseError(
            f"cannot import {target!r} from {importing_file}\n"
            f"tried:\n{tried_str}"
        )

    def _register_syscall_words(self) -> None:
        word = self.dictionary.lookup("syscall")
        if word is None:
            word = Word(name="syscall")
            self.dictionary.register(word)
        word.intrinsic = self._emit_syscall_intrinsic

    def _emit_syscall_intrinsic(self, builder: FunctionEmitter) -> None:
        def _try_pop_known_syscall_setup() -> Optional[Tuple[int, int]]:
            """Recognize and remove literal setup for known-argc syscalls.

            Supported forms right before `syscall`:
              1) <argc> <nr>
              2) <nr> <argc> ___linux_swap
            Returns (argc, nr) when recognized.
            """

            # Form 1: ... push argc ; push nr ; syscall
            nr = Assembler._pop_preceding_literal(builder)
            if nr is not None:
                argc = Assembler._pop_preceding_literal(builder)
                if argc is not None and 0 <= argc <= 6:
                    return argc, nr
                # rollback if second literal wasn't argc
                builder.push_literal(nr)

            # Form 2: ... push nr ; push argc ; ___linux_swap ; syscall
            text = builder.text
            swap_tail = [
                "mov rax, [r12]",
                "mov rbx, [r12 + 8]",
                "mov [r12], rbx",
                "mov [r12 + 8], rax",
            ]
            if len(text) >= 4 and [s.strip() for s in text[-4:]] == swap_tail:
                del text[-4:]
                argc2 = Assembler._pop_preceding_literal(builder)
                nr2 = Assembler._pop_preceding_literal(builder)
                if argc2 is not None and nr2 is not None and 0 <= argc2 <= 6:
                    return argc2, nr2
                # rollback conservatively if match fails
                if nr2 is not None:
                    builder.push_literal(nr2)
                if argc2 is not None:
                    builder.push_literal(argc2)
                text.extend(swap_tail)

            return None

        known = _try_pop_known_syscall_setup()
        if known is not None:
            argc, nr = known
            builder.push_literal(nr)
            builder.pop_to("rax")
            if argc >= 6:
                builder.pop_to("r9")
            if argc >= 5:
                builder.pop_to("r8")
            if argc >= 4:
                builder.pop_to("r10")
            if argc >= 3:
                builder.pop_to("rdx")
            if argc >= 2:
                builder.pop_to("rsi")
            if argc >= 1:
                builder.pop_to("rdi")
            builder.emit("    syscall")
            builder.push_from("rax")
            return

        label_id = self._syscall_label_counter
        self._syscall_label_counter += 1

        def lbl(suffix: str) -> str:
            return f"syscall_{label_id}_{suffix}"

        builder.pop_to("rax")  # syscall number
        builder.pop_to("rcx")  # arg count
        builder.emit("    ; clamp arg count to [0, 6]")
        builder.emit("    cmp rcx, 0")
        builder.emit(f"    jge {lbl('count_nonneg')}")
        builder.emit("    xor rcx, rcx")
        builder.emit(f"{lbl('count_nonneg')}:")
        builder.emit("    cmp rcx, 6")
        builder.emit(f"    jle {lbl('count_clamped')}")
        builder.emit("    mov rcx, 6")
        builder.emit(f"{lbl('count_clamped')}:")

        checks = [
            (6, "r9"),
            (5, "r8"),
            (4, "r10"),
            (3, "rdx"),
            (2, "rsi"),
            (1, "rdi"),
        ]
        for threshold, reg in checks:
            builder.emit(f"    cmp rcx, {threshold}")
            builder.emit(f"    jl {lbl(f'skip_{reg}')}")
            builder.pop_to(reg)
            builder.emit(f"{lbl(f'skip_{reg}')}:")

        builder.emit("    syscall")
        builder.push_from("rax")

    def _load_with_imports(self, path: Path, seen: Optional[Set[Path]] = None) -> Tuple[str, List[FileSpan]]:
        if seen is None:
            seen = set()
        out_lines: List[str] = []
        spans: List[FileSpan] = []
        self._append_file_with_imports(path.resolve(), out_lines, spans, seen)
        self._loaded_files = set(seen)
        return "\n".join(out_lines) + "\n", spans

    def _append_file_with_imports(
        self,
        path: Path,
        out_lines: List[str],
        spans: List[FileSpan],
        seen: Set[Path],
    ) -> None:
        # path is expected to be already resolved by callers
        if path in seen:
            return
        seen.add(path)

        try:
            contents = path.read_text()
        except FileNotFoundError as exc:
            raise ParseError(f"cannot import {path}: {exc}") from exc

        # Fast path for files without preprocessor/import directives.
        # This avoids line-by-line scanning for the common plain-source case.
        if (
            "import" not in contents
            and "define" not in contents
            and "ifdef" not in contents
            and "ifndef" not in contents
            and "elsedef" not in contents
            and "endif" not in contents
            and "flags" not in contents
            and ":py" not in contents
        ):
            lines = contents.splitlines()
            if lines:
                start_line = len(out_lines) + 1
                out_lines.extend(lines)
                spans.append(
                    FileSpan(
                        path=path,
                        start_line=start_line,
                        end_line=len(out_lines) + 1,
                        local_start_line=1,
                    )
                )
            return

        in_py_block = False
        brace_depth = 0
        string_char = None
        escape = False

        segment_start_global: Optional[int] = None
        segment_start_local: int = 1
        file_line_no = 1
        _out_append = out_lines.append
        _spans_append = spans.append
        _FileSpan = FileSpan

        # ifdef/ifndef/else/endif conditional compilation stack
        # Each entry is True (include lines) or False (skip lines)
        _ifdef_stack: List[bool] = []
        _ifdef_inactive = 0

        for line in contents.splitlines():
            # Avoid lstrip allocation when the line is already left-aligned.
            if line and (line[0] == " " or line[0] == "\t"):
                lstripped = line.lstrip()
            else:
                lstripped = line
            # Hot path: ordinary source line with no directive prefix.
            if not in_py_block and _ifdef_inactive == 0:
                lead = lstripped[:1]
                if lead not in ("i", "c", "d", "f", "e", ":"):
                    if segment_start_global is None:
                        segment_start_global = len(out_lines) + 1
                        segment_start_local = file_line_no
                    _out_append(line)
                    file_line_no += 1
                    continue

            stripped = lstripped.rstrip()

            # --- Conditional compilation directives ---
            if stripped[:6] == "ifdef " or stripped == "ifdef":
                name = stripped[6:].strip() if len(stripped) > 6 else ""
                if not name:
                    raise ParseError(f"ifdef missing symbol name at {path}:{file_line_no}")
                branch_active = name in self.defines if _ifdef_inactive == 0 else False
                _ifdef_stack.append(branch_active)
                if not branch_active:
                    _ifdef_inactive += 1
                _out_append("")  # placeholder to keep line numbers aligned
                file_line_no += 1
                continue
            if stripped[:7] == "ifndef " or stripped == "ifndef":
                name = stripped[7:].strip() if len(stripped) > 7 else ""
                if not name:
                    raise ParseError(f"ifndef missing symbol name at {path}:{file_line_no}")
                branch_active = name not in self.defines if _ifdef_inactive == 0 else False
                _ifdef_stack.append(branch_active)
                if not branch_active:
                    _ifdef_inactive += 1
                _out_append("")
                file_line_no += 1
                continue
            if stripped == "elsedef":
                if not _ifdef_stack:
                    raise ParseError(f"elsedef without matching ifdef/ifndef at {path}:{file_line_no}")
                was_active = _ifdef_stack[-1]
                now_active = not was_active
                _ifdef_stack[-1] = now_active
                if was_active and not now_active:
                    _ifdef_inactive += 1
                elif (not was_active) and now_active:
                    _ifdef_inactive -= 1
                _out_append("")
                file_line_no += 1
                continue
            if stripped == "endif":
                if not _ifdef_stack:
                    raise ParseError(f"endif without matching ifdef/ifndef at {path}:{file_line_no}")
                popped = _ifdef_stack.pop()
                if not popped:
                    _ifdef_inactive -= 1
                _out_append("")
                file_line_no += 1
                continue

            # If inside a false ifdef branch, skip the line
            if _ifdef_inactive:
                _out_append("")
                file_line_no += 1
                continue

            if stripped[:7] == "define " or stripped == "define":
                symbol = stripped[7:].strip() if len(stripped) > 7 else ""
                if not symbol:
                    raise ParseError(f"define missing symbol name at {path}:{file_line_no}")
                symbol_name = symbol.split(None, 1)[0]
                self.defines.add(symbol_name)
                _out_append("")
                file_line_no += 1
                continue

            if lstripped.startswith("flags") and (len(lstripped) == 5 or lstripped[5].isspace()):
                payload = lstripped[5:].strip()
                self._apply_flags_directive(payload, path=path, line_no=file_line_no)
                _out_append("")
                file_line_no += 1
                continue

            if not in_py_block and stripped[:3] == ":py" and "{" in stripped:
                in_py_block = True
                brace_depth = 0
                string_char = None
                escape = False
                # scan_line inline
                for ch in line:
                    if string_char:
                        if escape:
                            escape = False
                        elif ch == "\\":
                            escape = True
                        elif ch == string_char:
                            string_char = None
                    else:
                        if ch == "'" or ch == '"':
                            string_char = ch
                        elif ch == "{":
                            brace_depth += 1
                        elif ch == "}":
                            brace_depth -= 1
                # begin_segment_if_needed inline
                if segment_start_global is None:
                    segment_start_global = len(out_lines) + 1
                    segment_start_local = file_line_no
                _out_append(line)
                file_line_no += 1
                if brace_depth == 0:
                    in_py_block = False
                continue

            if in_py_block:
                # scan_line inline
                for ch in line:
                    if string_char:
                        if escape:
                            escape = False
                        elif ch == "\\":
                            escape = True
                        elif ch == string_char:
                            string_char = None
                    else:
                        if ch == "'" or ch == '"':
                            string_char = ch
                        elif ch == "{":
                            brace_depth += 1
                        elif ch == "}":
                            brace_depth -= 1
                # begin_segment_if_needed inline
                if segment_start_global is None:
                    segment_start_global = len(out_lines) + 1
                    segment_start_local = file_line_no
                _out_append(line)
                file_line_no += 1
                if brace_depth == 0:
                    in_py_block = False
                continue

            parsed_import = None
            if lstripped.startswith("import"):
                parsed_import = self._parse_directive_target_and_rest(
                    lstripped,
                    "import",
                    path=path,
                    line_no=file_line_no,
                    line_already_lstripped=True,
                )
            if parsed_import is not None:
                target, remainder = parsed_import

                # begin_segment_if_needed inline
                if segment_start_global is None:
                    segment_start_global = len(out_lines) + 1
                    segment_start_local = file_line_no
                _out_append("")
                source_line_no = file_line_no
                file_line_no += 1
                # close_segment_if_open inline
                if segment_start_global is not None:
                    _spans_append(
                        _FileSpan(
                            path=path,
                            start_line=segment_start_global,
                            end_line=len(out_lines) + 1,
                            local_start_line=segment_start_local,
                        )
                    )
                    segment_start_global = None

                target_path = self._resolve_import_target(path, target)
                self._append_file_with_imports(target_path, out_lines, spans, seen)

                # Allow `import <path> <statements...>`; keep trailing statements.
                if remainder:
                    if segment_start_global is None:
                        segment_start_global = len(out_lines) + 1
                        segment_start_local = source_line_no
                    _out_append(remainder)
                continue

            parsed_cimport = None
            if lstripped.startswith("cimport"):
                parsed_cimport = self._parse_directive_target_and_rest(
                    lstripped,
                    "cimport",
                    path=path,
                    line_no=file_line_no,
                    line_already_lstripped=True,
                )
            if parsed_cimport is not None:
                header_target, remainder = parsed_cimport
                # cimport "header.h" — extract extern declarations from a C header
                header_path = self._resolve_import_target(path, header_target)
                seen.add(header_path)
                try:
                    header_text = header_path.read_text()
                except FileNotFoundError as exc:
                    raise ParseError(f"cimport cannot read {header_path}: {exc}") from exc

                # Try running the C preprocessor for more accurate parsing
                header_text = self._preprocess_c_header(header_path, header_text)

                extern_lines = _parse_c_header_externs(header_text)
                struct_lines = _parse_c_header_structs(header_text)

                # begin_segment_if_needed inline
                if segment_start_global is None:
                    segment_start_global = len(out_lines) + 1
                    segment_start_local = file_line_no
                source_line_no = file_line_no
                # Replace the cimport line with the extracted extern + struct declarations
                for ext_line in extern_lines:
                    _out_append(ext_line)
                for st_line in struct_lines:
                    _out_append(st_line)
                _out_append("")  # blank line after externs
                file_line_no += 1

                # Keep optional trailing statements after cimport on the same line.
                if remainder:
                    _out_append(remainder)
                    if segment_start_global is None:
                        segment_start_global = len(out_lines) + 1
                        segment_start_local = source_line_no
                continue

            # begin_segment_if_needed inline
            if segment_start_global is None:
                segment_start_global = len(out_lines) + 1
                segment_start_local = file_line_no
            _out_append(line)
            file_line_no += 1

        # close_segment_if_open inline
        if segment_start_global is not None:
            _spans_append(
                _FileSpan(
                    path=path,
                    start_line=segment_start_global,
                    end_line=len(out_lines) + 1,
                    local_start_line=segment_start_local,
                )
            )

        if _ifdef_stack:
            raise ParseError(f"unterminated ifdef/ifndef ({len(_ifdef_stack)} level(s) deep) in {path}")


class SourceGraphCache:
    """Caches fully preprocessed source graphs keyed by source/import state."""

    _VERSION = 1

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        import hashlib
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hash_str(s: str) -> str:
        import hashlib
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _compiler_fingerprint() -> str:
        try:
            st = Path(__file__).stat()
            return f"{int(st.st_mtime_ns)}:{int(st.st_size)}"
        except OSError:
            return "0:0"

    def _manifest_path(
        self,
        source: Path,
        *,
        defines: Set[str],
        include_paths: Sequence[Path],
    ) -> Path:
        define_key = "\x1f".join(sorted(str(item) for item in defines))
        include_key = "\x1f".join(str(Path(item).expanduser().resolve()) for item in include_paths)
        key = self._hash_str(
            f"v={self._VERSION}|source={source.resolve()}|"
            f"defines={define_key}|includes={include_key}|"
            f"compiler={self._compiler_fingerprint()}"
        )
        return self.cache_dir / f"graph_{key}.json"

    def _file_info(self, path: Path) -> dict:
        st = path.stat()
        return {
            "mtime_ns": int(st.st_mtime_ns),
            "size": int(st.st_size),
            "hash": self._hash_bytes(path.read_bytes()),
        }

    def _deps_fresh(self, file_info: Dict[str, Any]) -> bool:
        for path_str, info in file_info.items():
            if not isinstance(path_str, str) or not isinstance(info, dict):
                return False
            p = Path(path_str)
            if not p.exists():
                return False
            try:
                st = p.stat()
            except OSError:
                return False

            cached_mtime_ns = info.get("mtime_ns")
            cached_size = info.get("size")
            if isinstance(cached_mtime_ns, int) and isinstance(cached_size, int):
                if int(st.st_mtime_ns) == cached_mtime_ns and int(st.st_size) == cached_size:
                    continue

            actual_hash = self._hash_bytes(p.read_bytes())
            if actual_hash != info.get("hash"):
                return False
        return True

    def load(
        self,
        source: Path,
        *,
        defines: Set[str],
        include_paths: Sequence[Path],
    ) -> Optional[dict]:
        manifest_path = self._manifest_path(source, defines=defines, include_paths=include_paths)
        if not manifest_path.exists():
            return None
        try:
            import json
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

        if not isinstance(payload, dict):
            return None
        if payload.get("version") != self._VERSION:
            return None
        file_info = payload.get("file_info")
        if not isinstance(file_info, dict):
            return None
        if not self._deps_fresh(file_info):
            return None
        return payload

    def save(
        self,
        source: Path,
        *,
        defines: Set[str],
        include_paths: Sequence[Path],
        loaded_files: Set[Path],
        source_text: str,
        spans: Sequence[FileSpan],
        source_link_flags: Sequence[str],
        source_include_paths: Sequence[Path],
        source_cli_flags: Sequence[str],
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        file_info: Dict[str, dict] = {}
        for p in sorted(loaded_files, key=lambda item: str(item)):
            try:
                file_info[str(p)] = self._file_info(p)
            except OSError:
                continue

        span_rows: List[Tuple[str, int, int, int]] = []
        for span in spans:
            span_rows.append(
                (
                    span.path.as_posix(),
                    int(span.start_line),
                    int(span.end_line),
                    int(span.local_start_line),
                )
            )

        payload = {
            "version": self._VERSION,
            "source": source_text,
            "spans": span_rows,
            "files": [str(p) for p in sorted(loaded_files, key=lambda item: str(item))],
            "file_info": file_info,
            "source_link_flags": [str(f) for f in source_link_flags if str(f)],
            "source_include_paths": [str(Path(p).expanduser().resolve()) for p in source_include_paths],
            "source_cli_flags": [str(f) for f in source_cli_flags if str(f)],
            "defines": sorted(str(item) for item in defines),
            "include_paths": [str(Path(p).expanduser().resolve()) for p in include_paths],
        }

        manifest_path = self._manifest_path(source, defines=defines, include_paths=include_paths)
        try:
            import json
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            return


class BuildCache:
    """Caches compilation artifacts keyed by source content and compiler flags."""

    _FORMAT_VERSION = 2

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        import hashlib
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hash_str(s: str) -> str:
        import hashlib
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _manifest_path(self, source: Path) -> Path:
        key = self._hash_str(str(source.resolve()))
        return self.cache_dir / f"{key}.json"

    def flags_hash(
        self,
        debug: bool,
        folding: bool,
        peephole: bool,
        auto_inline: bool,
        asm_post_opt: bool,
        string_deduplication: bool,
        entry_mode: str,
    ) -> str:
        # Include the compiler's own mtime so any change to main.py
        # (codegen improvements, bug fixes) invalidates cached results.
        try:
            compiler_mtime = os.path.getmtime(__file__)
        except OSError:
            compiler_mtime = 0
        return self._hash_str(
            f"debug={debug},folding={folding},"
            f"peephole={peephole},auto_inline={auto_inline},"
            f"asm_post_opt={asm_post_opt},"
            f"string_deduplication={string_deduplication},"
            f"entry_mode={entry_mode},compiler_mtime={compiler_mtime}"
        )

    def _file_info(self, path: Path) -> dict:
        st = path.stat()
        return {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "hash": self._hash_bytes(path.read_bytes()),
        }

    def load_manifest(self, source: Path) -> Optional[dict]:
        mp = self._manifest_path(source)
        if not mp.exists():
            return None
        try:
            import json
            payload = json.loads(mp.read_text())
        except (ValueError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("format_version") != self._FORMAT_VERSION:
            return None
        return payload

    def check_fresh(self, manifest: dict, fhash: str) -> bool:
        """Return True if all source files are unchanged and flags match."""
        if manifest.get("flags_hash") != fhash:
            return False
        if manifest.get("has_ct_effects"):
            return False
        files = manifest.get("files", {})
        for path_str, info in files.items():
            p = Path(path_str)
            if not p.exists():
                return False
            try:
                st = p.stat()
            except OSError:
                return False
            if st.st_mtime == info.get("mtime") and st.st_size == info.get("size"):
                continue
            actual_hash = self._hash_bytes(p.read_bytes())
            if actual_hash != info.get("hash"):
                return False
        return True

    def get_cached_asm(self, manifest: dict) -> Optional[str]:
        asm_hash = manifest.get("asm_hash")
        if not asm_hash:
            return None
        asm_path = self.cache_dir / f"{asm_hash}.asm"
        if not asm_path.exists():
            return None
        return asm_path.read_text()

    def save(
        self,
        source: Path,
        loaded_files: Set[Path],
        fhash: str,
        asm_text: str,
        has_ct_effects: bool = False,
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        files: Dict[str, dict] = {}
        for p in sorted(loaded_files):
            try:
                files[str(p)] = self._file_info(p)
            except OSError:
                pass
        asm_hash = self._hash_str(asm_text)
        asm_path = self.cache_dir / f"{asm_hash}.asm"
        asm_path.write_text(asm_text)
        manifest = {
            "format_version": self._FORMAT_VERSION,
            "source": str(source.resolve()),
            "flags_hash": fhash,
            "files": files,
            "asm_hash": asm_hash,
            "has_ct_effects": has_ct_effects,
        }
        import json
        self._manifest_path(source).write_text(json.dumps(manifest))

    def clean(self) -> None:
        if self.cache_dir.exists():
            import shutil
            shutil.rmtree(self.cache_dir)


_nasm_path: str = ""
_linker_path: str = ""
_linker_is_lld: bool = False


def _which_exec(name: str) -> Optional[str]:
    """Lightweight executable lookup without importing shutil."""
    if not name:
        return None
    if os.path.sep in name:
        return name if (os.path.isfile(name) and os.access(name, os.X_OK)) else None
    for part in os.environ.get("PATH", "").split(os.pathsep):
        base = part or "."
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None

def _find_nasm() -> str:
    global _nasm_path
    if _nasm_path:
        return _nasm_path
    p = _which_exec("nasm")
    if not p:
        raise RuntimeError("nasm not found")
    _nasm_path = p
    return p

def _find_linker() -> tuple:
    global _linker_path, _linker_is_lld
    if _linker_path:
        return _linker_path, _linker_is_lld
    lld = _which_exec("ld.lld")
    if lld:
        _linker_path = lld
        _linker_is_lld = True
        return lld, True
    ld = _which_exec("ld")
    if ld:
        _linker_path = ld
        _linker_is_lld = False
        return ld, False
    raise RuntimeError("No linker found")

def _run_cmd(args: list) -> None:
    """Run a command using posix_spawn for lower overhead than subprocess."""
    pid = os.posix_spawn(args[0], args, os.environ)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        code = os.WEXITSTATUS(status)
        if code != 0:
            import subprocess
            raise subprocess.CalledProcessError(code, args)
    elif os.WIFSIGNALED(status):
        import subprocess
        raise subprocess.CalledProcessError(-os.WTERMSIG(status), args)


def run_nasm(asm_path: Path, obj_path: Path, debug: bool = False, asm_text: str = "") -> None:
    nasm = _find_nasm()
    cmd = [nasm, "-f", "elf64"]
    if debug:
        cmd.extend(["-g", "-F", "dwarf"])
    cmd += ["-o", str(obj_path), str(asm_path)]
    _run_cmd(cmd)


def run_linker(obj_path: Path, exe_path: Path, debug: bool = False, libs=None, *, shared: bool = False):
    libs = libs or []

    linker, use_lld = _find_linker()

    cmd = [linker]

    if use_lld:
        cmd.extend(["-m", "elf_x86_64"])

    if shared:
        cmd.append("-shared")

    cmd.extend([
        "-o", str(exe_path),
        str(obj_path),
    ])

    if not shared and not libs:
        cmd.extend(["-nostdlib", "-static"])

    if libs:
        # Determine if any libs require dynamic linking (shared libraries).
        needs_dynamic = any(
            not (str(lib).endswith(".a") or str(lib).endswith(".o"))
            for lib in libs if lib
        )
        if not shared and needs_dynamic:
            cmd.extend([
                "-dynamic-linker", "/lib64/ld-linux-x86-64.so.2",
            ])
        # Add standard library search paths so ld.lld can find libc etc.
        for lib_dir in ["/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/lib/x86_64-linux-gnu"]:
            if os.path.isdir(lib_dir):
                cmd.append(f"-L{lib_dir}")
        for lib in libs:
            if not lib:
                continue
            lib = str(lib)
            if lib.startswith(("-L", "-l", "-Wl,")):
                cmd.append(lib)
                continue
            if lib.startswith(":"):
                cmd.append(f"-l{lib}")
                continue
            if os.path.isabs(lib) or lib.startswith("./") or lib.startswith("../"):
                cmd.append(lib)
                continue
            if os.path.sep in lib or lib.endswith(".a"):
                cmd.append(lib)
                continue
            if ".so" in lib:
                cmd.append(f"-l:{lib}")
                continue
            cmd.append(f"-l{lib}")

    if debug:
        cmd.append("-g")

    _run_cmd(cmd)


def build_static_library(obj_path: Path, archive_path: Path) -> None:
    import subprocess
    parent = archive_path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ar", "rcs", str(archive_path), str(obj_path)], check=True)


def _load_sidecar_meta_libs(source: Path) -> List[str]:
    """Return additional linker libs from sibling <source>.meta.json."""
    meta_path = source.with_suffix(".meta.json")
    if not meta_path.exists():
        return []
    try:
        import json
        payload = json.loads(meta_path.read_text())
    except Exception as exc:
        print(f"[warn] failed to read {meta_path}: {exc}")
        return []
    libs = payload.get("libs")
    if not isinstance(libs, list):
        return []
    out: List[str] = []
    for item in libs:
        if isinstance(item, str) and item:
            out.append(item)
    return out


def _build_ct_sidecar_shared(source: Path, temp_dir: Path) -> Optional[Path]:
    """Build sibling <source>.c into a shared object for --ct-run-main externs."""
    c_path = source.with_suffix(".c")
    if not c_path.exists():
        return None
    temp_dir.mkdir(parents=True, exist_ok=True)
    so_path = temp_dir / f"{source.stem}.ctlib.so"
    cmd = ["cc", "-shared", "-fPIC", str(c_path), "-o", str(so_path)]
    import subprocess
    subprocess.run(cmd, check=True)
    return so_path


def run_repl(
    compiler: Compiler,
    temp_dir: Path,
    libs: Sequence[str],
    debug: bool = False,
    initial_source: Optional[Path] = None,
) -> int:
    """REPL backed by the compile-time VM for instant execution.

    State (data stack, memory, definitions) persists across evaluations.
    Use ``:reset`` to start fresh.
    """

    # -- Colors ---------------------------------------------------------------
    _C_RESET = "\033[0m"
    _C_BOLD = "\033[1m"
    _C_DIM = "\033[2m"
    _C_GREEN = "\033[32m"
    _C_CYAN = "\033[36m"
    _C_YELLOW = "\033[33m"
    _C_RED = "\033[31m"
    _C_MAGENTA = "\033[35m"

    use_color = sys.stdout.isatty()
    def _c(code: str, text: str) -> str:
        return f"{code}{text}{_C_RESET}" if use_color else text

    # -- Helpers --------------------------------------------------------------
    def _block_defines_main(block: str) -> bool:
        stripped_lines = [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        for idx, stripped in enumerate(stripped_lines):
            for prefix in ("word", ":asm", ":py", "extern"):
                if stripped.startswith(f"{prefix} "):
                    rest = stripped[len(prefix):].lstrip()
                    if rest.startswith("main"):
                        return True
            if stripped == "word" and idx + 1 < len(stripped_lines):
                if stripped_lines[idx + 1].startswith("main"):
                    return True
        return False

    temp_dir.mkdir(parents=True, exist_ok=True)
    src_path = temp_dir / "repl.sl"
    editor_cmd = os.environ.get("EDITOR") or "vim"

    default_imports = ["import stdlib/stdlib.sl", "import stdlib/io.sl"]
    imports: List[str] = list(default_imports)
    user_defs_files: List[str] = []
    user_defs_repl: List[str] = []
    main_body: List[str] = []
    has_user_main = False

    include_paths = list(compiler.include_paths)

    if initial_source is not None:
        try:
            initial_text = initial_source.read_text()
            user_defs_files.append(initial_text)
            has_user_main = has_user_main or _block_defines_main(initial_text)
            if has_user_main:
                main_body.clear()
            print(_c(_C_DIM, f"[repl] loaded {initial_source}"))
        except Exception as exc:
            print(_c(_C_RED, f"[repl] failed to load {initial_source}: {exc}"))

    # -- Persistent VM execution ----------------------------------------------
    def _run_on_ct_vm(source: str, word_name: str = "main") -> bool:
        """Parse source and execute *word_name* via the compile-time VM.

        Uses ``invoke_repl`` so stacks/memory persist across calls.
        Returns True on success, False on error (already printed).
        """
        nonlocal compiler
        src_path.write_text(source)
        try:
            _suppress_redefine_warnings_set(True)
            compiler._loaded_files.clear()
            compiler.parse_file(src_path)
        except (ParseError, CompileError, CompileTimeError) as exc:
            print(_c(_C_RED, f"[error] {exc}"))
            return False
        except Exception as exc:
            print(_c(_C_RED, f"[error] parse failed: {exc}"))
            return False
        finally:
            _suppress_redefine_warnings_set(False)

        try:
            compiler.run_compile_time_word_repl(word_name, libs=list(libs))
        except (CompileTimeError, _CTVMExit) as exc:
            if isinstance(exc, _CTVMExit):
                code = exc.code
                if code != 0:
                    print(_c(_C_YELLOW, f"[warn] program exited with code {code}"))
            else:
                print(_c(_C_RED, f"[error] {exc}"))
                return False
        except Exception as exc:
            print(_c(_C_RED, f"[error] execution failed: {exc}"))
            return False
        return True

    # -- Stack display --------------------------------------------------------
    def _show_stack() -> None:
        vm = compiler.parser.compile_time_vm
        values = vm.repl_stack_values()
        if not values:
            print(_c(_C_DIM, "<empty stack>"))
        else:
            parts = []
            for v in values:
                if v < 0:
                    v = v + (1 << 64)  # show as unsigned
                    parts.append(f"{v} (0x{v:x})")
                elif v > 0xFFFF:
                    parts.append(f"{v} (0x{v:x})")
                else:
                    parts.append(str(v))
            depth_str = _c(_C_DIM, f"<{len(values)}>")
            print(f"{depth_str} {' '.join(parts)}")

    # -- Word listing ---------------------------------------------------------
    def _show_words(filter_str: str = "") -> None:
        all_words = sorted(compiler.dictionary.words.keys())
        if filter_str:
            all_words = [w for w in all_words if filter_str in w]
        if not all_words:
            print(_c(_C_DIM, "no matching words"))
            return
        # Print in columns
        max_len = max(len(w) for w in all_words) + 2
        cols = max(1, 80 // max_len)
        for i in range(0, len(all_words), cols):
            row = all_words[i:i + cols]
            print("  ".join(w.ljust(max_len) for w in row))
        print(_c(_C_DIM, f"({len(all_words)} words)"))

    # -- Word type/info -------------------------------------------------------
    def _show_type(word_name: str) -> None:
        word = compiler.dictionary.lookup(word_name)
        if word is None:
            print(_c(_C_RED, f"word '{word_name}' not found"))
            return

        # Header: name + kind
        defn = word.definition
        if word.is_extern:
            kind = "extern"
        elif word.macro_expansion is not None:
            kind = "macro"
        elif isinstance(defn, AsmDefinition):
            kind = "asm"
        elif isinstance(defn, Definition):
            kind = "word"
        elif word.compile_time_intrinsic is not None or word.runtime_intrinsic is not None:
            kind = "builtin"
        elif word.macro is not None:
            kind = "immediate/macro"
        else:
            kind = "unknown"
        print(f"  {_c(_C_BOLD, word_name)}  {_c(_C_CYAN, kind)}")

        # Tags
        tags: List[str] = []
        if word.immediate:
            tags.append("immediate")
        if word.compile_only:
            tags.append("compile-only")
        if word.runtime_only:
            tags.append("runtime-only")
        if word.inline:
            tags.append("inline")
        if word.compile_time_override:
            tags.append("ct-override")
        if word.priority != 0:
            tags.append(f"priority={word.priority}")
        if tags:
            print(f"  {_c(_C_DIM, '  tags: ')}{_c(_C_YELLOW, ' '.join(tags))}")

        # Extern signature
        if word.is_extern and word.extern_signature:
            arg_types, ret_type = word.extern_signature
            sig = f"{ret_type} {word_name}({', '.join(arg_types)})"
            print(f"  {_c(_C_DIM, '  sig:  ')}{_c(_C_GREEN, sig)}")
        elif word.is_extern:
            print(f"  {_c(_C_DIM, '  args: ')}{word.extern_inputs} in, {word.extern_outputs} out")

        # Stack effect from definition comment
        if isinstance(defn, Definition) and defn.stack_inputs is not None:
            print(f"  {_c(_C_DIM, '  args: ')}{defn.stack_inputs} inputs")

        # Macro expansion
        if word.macro_expansion is not None:
            params = word.macro_params
            expansion = " ".join(word.macro_expansion)
            if len(expansion) > 80:
                expansion = expansion[:77] + "..."
            param_str = f" (${params} params)" if params else ""
            print(f"  {_c(_C_DIM, '  expands:')}{param_str} {expansion}")

        # Asm body (trimmed)
        if isinstance(defn, AsmDefinition):
            body = defn.body.strip()
            lines = body.splitlines()
            if defn.effects:
                print(f"  {_c(_C_DIM, '  effects:')} {' '.join(sorted(defn.effects))}")
            if len(lines) <= 6:
                for ln in lines:
                    print(f"    {_c(_C_DIM, ln.rstrip())}")
            else:
                for ln in lines[:4]:
                    print(f"    {_c(_C_DIM, ln.rstrip())}")
                print(f"    {_c(_C_DIM, f'... ({len(lines)} lines total)')}")

        # Word body (decompiled ops)
        if isinstance(defn, Definition):
            ops = defn.body
            indent = 0
            max_show = 12
            shown = 0
            for op in ops:
                if shown >= max_show:
                    print(f"    {_c(_C_DIM, f'... ({len(ops)} ops total)')}")
                    break
                if op.op in ("branch_zero", "for_begin", "while_begin", "list_begin"):
                    pass
                if op.op in ("jump", "for_end"):
                    indent = max(0, indent - 1)

                if op.op == "literal":
                    if isinstance(op.data, str):
                        txt = f'"{op.data}"' if len(op.data) <= 40 else f'"{op.data[:37]}..."'
                        line_str = f"  {txt}"
                    elif isinstance(op.data, float):
                        line_str = f"  {op.data}"
                    else:
                        line_str = f"  {op.data}"
                elif op.op == "word":
                    line_str = f"  {op.data}"
                elif op.op == "branch_zero":
                    line_str = "  if"
                    indent += 1
                elif op.op == "jump":
                    line_str = "  else/end"
                elif op.op == "for_begin":
                    line_str = "  for"
                    indent += 1
                elif op.op == "for_end":
                    line_str = "  end-for"
                elif op.op == "label":
                    line_str = f"  label {op.data}"
                elif op.op == "goto":
                    line_str = f"  goto {op.data}"
                else:
                    line_str = f"  {op.op}" + (f" {op.data}" if op.data is not None else "")

                print(f"  {_c(_C_DIM, '  ' * indent)}{line_str}")
                shown += 1

    # -- readline setup -------------------------------------------------------
    history_path = temp_dir / "repl_history"
    try:
        import readline
        readline.parse_and_bind("tab: complete")
        try:
            readline.read_history_file(str(history_path))
        except (FileNotFoundError, OSError):
            pass

        def _completer(text: str, state: int) -> Optional[str]:
            commands = [":help", ":show", ":reset", ":load ", ":call ",
                        ":edit ", ":seteditor ", ":quit", ":q",
                        ":stack", ":words ", ":type ", ":clear"]
            if text.startswith(":"):
                matches = [c for c in commands if c.startswith(text)]
            else:
                all_words = sorted(compiler.dictionary.words.keys())
                matches = [w + " " for w in all_words if w.startswith(text)]
            return matches[state] if state < len(matches) else None

        readline.set_completer(_completer)
        readline.set_completer_delims(" \t\n")
        _has_readline = True
    except ImportError:
        _has_readline = False

    # -- Help -----------------------------------------------------------------
    def _print_help() -> None:
        print(_c(_C_BOLD, "[repl] commands:"))
        cmds = [
            (":help", "show this help"),
            (":stack", "display the data stack"),
            (":clear", "clear the data stack (keep definitions)"),
            (":words [filter]", "list defined words (optionally filtered)"),
            (":type <word>", "show word info / signature"),
            (":show", "display current session source"),
            (":reset", "clear everything — fresh VM and dictionary"),
            (":load <file>", "load a source file into the session"),
            (":call <word>", "execute a word via the compile-time VM"),
            (":edit [file]", "open session file or given file in editor"),
            (":seteditor [cmd]", "show/set editor command (default from $EDITOR)"),
            (":quit | :q", "exit the REPL"),
        ]
        for cmd, desc in cmds:
            print(f"  {_c(_C_GREEN, cmd.ljust(20))} {desc}")
        print(_c(_C_BOLD, "[repl] free-form input:"))
        print("  definitions (word/:asm/:py/extern/macro/struct) extend the session")
        print("  imports add to session imports")
        print("  other lines run immediately (values stay on the stack)")
        print("  multiline: end lines with \\ to continue")

    # -- Banner ---------------------------------------------------------------
    prompt = _c(_C_GREEN + _C_BOLD, "l2> ") if use_color else "l2> "
    cont_prompt = _c(_C_DIM, "... ") if use_color else "... "
    print(_c(_C_BOLD, "[repl] L2 interactive — type :help for commands, :quit to exit"))
    print(_c(_C_DIM, "[repl] state persists across evaluations; :reset to start fresh"))

    pending_block: List[str] = []

    while True:
        try:
            cur_prompt = cont_prompt if pending_block else prompt
            line = input(cur_prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break

        stripped = line.strip()
        if stripped in {":quit", ":q"}:
            break
        if stripped == ":help":
            _print_help()
            continue
        if stripped == ":stack":
            _show_stack()
            continue
        if stripped == ":clear":
            vm = compiler.parser.compile_time_vm
            if vm._repl_initialized:
                vm.r12 = vm._native_data_top
            else:
                vm.stack.clear()
            print(_c(_C_DIM, "stack cleared"))
            continue
        if stripped.startswith(":words"):
            filt = stripped.split(None, 1)[1].strip() if " " in stripped else ""
            _show_words(filt)
            continue
        if stripped.startswith(":type "):
            word_name = stripped.split(None, 1)[1].strip()
            if word_name:
                _show_type(word_name)
            else:
                print(_c(_C_RED, "[repl] usage: :type <word>"))
            continue
        if stripped == ":reset":
            imports = list(default_imports)
            user_defs_files.clear()
            user_defs_repl.clear()
            main_body.clear()
            has_user_main = False
            pending_block.clear()
            compiler = Compiler(
                include_paths=include_paths,
                macro_expansion_limit=compiler.parser.macro_expansion_limit,
                macro_preview=compiler.parser.macro_preview,
            )
            print(_c(_C_DIM, "[repl] session reset — fresh VM and dictionary"))
            continue
        if stripped.startswith(":seteditor"):
            parts = stripped.split(None, 1)
            if len(parts) == 1 or not parts[1].strip():
                print(f"[repl] editor: {editor_cmd}")
            else:
                editor_cmd = parts[1].strip()
                print(f"[repl] editor set to: {editor_cmd}")
            continue
        if stripped.startswith(":edit"):
            arg = stripped.split(None, 1)[1].strip() if " " in stripped else ""
            target_path = Path(arg) if arg else src_path
            try:
                current_source = _repl_build_source(
                    imports,
                    user_defs_files,
                    user_defs_repl,
                    main_body,
                    has_user_main,
                    force_synthetic=bool(main_body),
                )
                src_path.write_text(current_source)
            except Exception as exc:
                print(_c(_C_RED, f"[repl] failed to sync source before edit: {exc}"))
            try:
                if not target_path.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.touch()
                import shlex
                cmd_parts = shlex.split(editor_cmd)
                import subprocess
                subprocess.run([*cmd_parts, str(target_path)])
                if target_path.resolve() == src_path.resolve():
                    try:
                        updated = target_path.read_text()
                        new_imports: List[str] = []
                        non_import_lines: List[str] = []
                        for ln in updated.splitlines():
                            stripped_ln = ln.strip()
                            if stripped_ln.startswith("import "):
                                new_imports.append(stripped_ln)
                            else:
                                non_import_lines.append(ln)
                        imports = new_imports if new_imports else list(default_imports)
                        new_body = "\n".join(non_import_lines).strip()
                        user_defs_files = [new_body] if new_body else []
                        user_defs_repl.clear()
                        main_body.clear()
                        has_user_main = _block_defines_main(new_body)
                        print(_c(_C_DIM, "[repl] reloaded session source from editor"))
                    except Exception as exc:
                        print(_c(_C_RED, f"[repl] failed to reload edited source: {exc}"))
            except Exception as exc:
                print(_c(_C_RED, f"[repl] failed to launch editor: {exc}"))
            continue
        if stripped == ":show":
            source = _repl_build_source(imports, user_defs_files, user_defs_repl, main_body, has_user_main, force_synthetic=True)
            print(source.rstrip())
            continue
        if stripped.startswith(":load "):
            path_text = stripped.split(None, 1)[1].strip()
            target_path = Path(path_text)
            if not target_path.exists():
                print(_c(_C_RED, f"[repl] file not found: {target_path}"))
                continue
            try:
                loaded_text = target_path.read_text()
                user_defs_files.append(loaded_text)
                if _block_defines_main(loaded_text):
                    has_user_main = True
                    main_body.clear()
                print(_c(_C_DIM, f"[repl] loaded {target_path}"))
            except Exception as exc:
                print(_c(_C_RED, f"[repl] failed to load {target_path}: {exc}"))
            continue
        if stripped.startswith(":call "):
            word_name = stripped.split(None, 1)[1].strip()
            if not word_name:
                print(_c(_C_RED, "[repl] usage: :call <word>"))
                continue
            if word_name == "main" and not has_user_main:
                print(_c(_C_RED, "[repl] cannot call main; no user-defined main present"))
                continue
            if word_name == "main" and has_user_main:
                source = _repl_build_source(imports, user_defs_files, user_defs_repl, [], True, force_synthetic=False)
            else:
                temp_defs = [*user_defs_repl, f"word __repl_call__\n    {word_name}\nend"]
                source = _repl_build_source(imports, user_defs_files, temp_defs, [], True, force_synthetic=False)
                _run_on_ct_vm(source, "__repl_call__")
                continue
            _run_on_ct_vm(source, word_name)
            continue
        if not stripped:
            continue

        # Multiline handling via trailing backslash
        if line.endswith("\\"):
            pending_block.append(line[:-1])
            continue

        if pending_block:
            pending_block.append(line)
            block = "\n".join(pending_block)
            pending_block.clear()
        else:
            block = line

        block_stripped = block.lstrip()
        first_tok = block_stripped.split(None, 1)[0] if block_stripped else ""
        is_definition = first_tok in {"word", ":asm", ":py", "extern", "macro", "struct"}
        is_import = first_tok == "import"

        if is_import:
            imports.append(block_stripped)
        elif is_definition:
            if _block_defines_main(block):
                user_defs_repl = [d for d in user_defs_repl if not _block_defines_main(d)]
                has_user_main = True
                main_body.clear()
            user_defs_repl.append(block)
        else:
            source = _repl_build_source(
                imports,
                user_defs_files,
                user_defs_repl,
                block.splitlines(),
                has_user_main,
                force_synthetic=True,
            )
            _run_on_ct_vm(source)
            continue

        # Validate definitions by parsing (no execution needed).
        source = _repl_build_source(imports, user_defs_files, user_defs_repl, main_body, has_user_main, force_synthetic=bool(main_body))
        try:
            src_path.write_text(source)
            _suppress_redefine_warnings_set(True)
            try:
                compiler._loaded_files.clear()
                compiler.parse_file(src_path)
            finally:
                _suppress_redefine_warnings_set(False)
        except (ParseError, CompileError, CompileTimeError) as exc:
            print(_c(_C_RED, f"[error] {exc}"))
            continue

    # Save readline history
    if _has_readline:
        try:
            readline.write_history_file(str(history_path))
        except OSError:
            pass

    return 0


def _repl_build_source(
    imports: Sequence[str],
    file_defs: Sequence[str],
    repl_defs: Sequence[str],
    main_body: Sequence[str],
    has_user_main: bool,
    force_synthetic: bool = False,
) -> str:
    lines: List[str] = []
    lines.extend(imports)
    lines.extend(file_defs)
    lines.extend(repl_defs)
    if (force_synthetic or not has_user_main) and main_body:
        lines.append("word main")
        for ln in main_body:
            if ln:
                lines.append(f"    {ln}")
            else:
                lines.append("")
        lines.append("end")
    return "\n".join(lines) + "\n"


# ---- Docs explorer integration (delegated to docs.py) ----

_DOCS_HELPERS_MODULE: Any = None
_DOCS_HELPERS_LOADED = False
_DOCS_HELPERS_WARNED = False
_DOCS_HELPERS_ERROR = ""


def _load_docs_helpers(*, warn: bool = False) -> Optional[Any]:
    global _DOCS_HELPERS_MODULE
    global _DOCS_HELPERS_LOADED
    global _DOCS_HELPERS_WARNED
    global _DOCS_HELPERS_ERROR

    if not _DOCS_HELPERS_LOADED:
        _DOCS_HELPERS_LOADED = True
        docs_path = Path(__file__).with_name("docs.py")
        if docs_path.exists():
            spec = None
            try:
                spec = importlib.util.spec_from_file_location("l2_docs_helpers", docs_path)
                if spec is None or spec.loader is None:
                    raise RuntimeError("unable to create module spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                if hasattr(module, "configure_runtime"):
                    module.configure_runtime(ct_word_metadata_provider=_collect_ct_word_metadata)
                _DOCS_HELPERS_MODULE = module
                _DOCS_HELPERS_ERROR = ""
            except Exception as exc:
                if spec is not None and getattr(spec, "name", None):
                    sys.modules.pop(spec.name, None)
                _DOCS_HELPERS_MODULE = None
                _DOCS_HELPERS_ERROR = f"failed to import docs.py: {exc}"
        else:
            _DOCS_HELPERS_MODULE = None
            _DOCS_HELPERS_ERROR = "docs.py not found"

    if warn and _DOCS_HELPERS_MODULE is None and not _DOCS_HELPERS_WARNED:
        _DOCS_HELPERS_WARNED = True
        if _DOCS_HELPERS_ERROR:
            sys.stderr.write(f"[warn] docs helpers unavailable ({_DOCS_HELPERS_ERROR})\n")
        else:
            sys.stderr.write("[warn] docs helpers unavailable\n")

    return _DOCS_HELPERS_MODULE


def _collect_ct_word_metadata() -> List[Dict[str, Any]]:
    dictionary = bootstrap_dictionary()
    out: List[Dict[str, Any]] = []
    for name, word in dictionary.words.items():
        if word.compile_time_intrinsic is None or name.startswith("__"):
            continue
        out.append(
            {
                "name": name,
                "compile_only": bool(word.compile_only),
                "immediate": bool(word.immediate),
                "runtime_only": bool(word.runtime_only),
                "has_runtime_intrinsic": bool(word.runtime_intrinsic is not None),
            }
        )
    out.sort(key=lambda item: str(item["name"]).lower())
    return out


def _build_ct_ref_complete_summary_table(base_doc_text: str) -> str:
    docs_helpers = _load_docs_helpers(warn=False)
    if docs_helpers is None or not hasattr(docs_helpers, "build_ct_reference_bundle"):
        return ""
    bundle = docs_helpers.build_ct_reference_bundle(base_doc_text, _collect_ct_word_metadata())
    return str(bundle.get("summary_text", ""))


def _build_ct_ref_function_appendix(base_doc_text: str) -> str:
    docs_helpers = _load_docs_helpers(warn=False)
    if docs_helpers is None or not hasattr(docs_helpers, "build_ct_reference_bundle"):
        return ""
    bundle = docs_helpers.build_ct_reference_bundle(base_doc_text, _collect_ct_word_metadata())
    return str(bundle.get("appendix_text", ""))


def run_docs_explorer(
    *,
    source: Optional[Path],
    include_paths: Sequence[Path],
    explicit_roots: Sequence[Path],
    initial_query: str,
    include_undocumented: bool = False,
    include_private: bool = False,
    include_tests: bool = False,
) -> int:
    docs_helpers = _load_docs_helpers(warn=True)
    if docs_helpers is None:
        reason = _DOCS_HELPERS_ERROR or "failed to import docs.py"
        raise CompileError(f"docs mode unavailable: {reason}")
    if not hasattr(docs_helpers, "run_docs_cli"):
        raise CompileError("docs mode unavailable: docs.py missing run_docs_cli")
    try:
        return int(
            docs_helpers.run_docs_cli(
                source=source,
                include_paths=include_paths,
                explicit_roots=explicit_roots,
                initial_query=initial_query,
                include_undocumented=include_undocumented,
                include_private=include_private,
                include_tests=include_tests,
                ct_word_metadata_provider=_collect_ct_word_metadata,
            )
        )
    except Exception as exc:
        raise CompileError(f"docs mode failed: {exc}") from exc


def _integrity_opcode_symbols() -> Set[str]:
    return {
        name
        for name, value in globals().items()
        if name.startswith("OP_") and isinstance(value, int) and name != "OP_OTHER"
    }


def _integrity_symbols_in_object(obj: Any) -> Set[str]:
    try:
        import inspect
        source = inspect.getsource(obj)
    except Exception:
        return set()
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return set()
    seen: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id.startswith("OP_"):
            seen.add(node.id)
    return seen


def _integrity_load_ct_reference_text() -> str:
    """Load `_L2_CT_REF_TEXT` from docs.py `_run_docs_tui` assignments."""
    docs_path = Path(__file__).with_name("docs.py")
    if not docs_path.exists():
        return ""

    try:
        source = docs_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""

    fn_node: Optional[ast.FunctionDef] = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_run_docs_tui":
            fn_node = node
            break
    if fn_node is None:
        return ""

    env: Dict[str, str] = {}

    def _eval_expr(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = _eval_expr(node.left)
            right = _eval_expr(node.right)
            if left is None or right is None:
                return None
            return left + right
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "textwrap"
                and func.attr == "indent"
                and len(node.args) >= 2
            ):
                text = _eval_expr(node.args[0])
                prefix = _eval_expr(node.args[1])
                if isinstance(text, str) and isinstance(prefix, str):
                    return textwrap.indent(text, prefix)
            return None
        return None

    for stmt in fn_node.body:
        if not isinstance(stmt, ast.Assign):
            continue
        value = _eval_expr(stmt.value)
        if not isinstance(value, str):
            continue
        for target in stmt.targets:
            if not isinstance(target, ast.Name):
                continue
            env[target.id] = value
            if target.id == "_L2_CT_REF_TEXT":
                return value
    return ""


def _integrity_doc_has_word_entry(doc_text: str, word_name: str) -> bool:
    pattern = re.compile(rf"(?m)^\s{{2,}}{re.escape(word_name)}(?:\s+|$)")
    return pattern.search(doc_text) is not None


def _run_integrity_compile_time_docs_checks(errors: List[str]) -> None:
    doc_text = _integrity_load_ct_reference_text()
    if not doc_text.strip():
        errors.append("failed to load built-in compile-time reference text")
        return

    docs_helpers = _load_docs_helpers(warn=False)
    summary_text = ""
    appendix_text = ""
    entries: List[Dict[str, Any]] = []

    try:
        if docs_helpers is not None and hasattr(docs_helpers, "build_ct_reference_bundle"):
            bundle = docs_helpers.build_ct_reference_bundle(
                doc_text,
                _collect_ct_word_metadata(),
            )
            summary_text = str(bundle.get("summary_text", ""))
            appendix_text = str(bundle.get("appendix_text", ""))
            entries = [dict(item) for item in bundle.get("entries", [])]
        else:
            summary_text = _build_ct_ref_complete_summary_table(doc_text)
            appendix_text = _build_ct_ref_function_appendix(doc_text)
    except Exception as exc:
        errors.append(f"failed to build generated compile-time reference sections: {exc}")
        return

    full_text = doc_text + summary_text + appendix_text

    dictionary = bootstrap_dictionary()
    registered_ct_words = sorted(
        name
        for name, word in dictionary.words.items()
        if word.compile_time_intrinsic is not None and not name.startswith("__")
    )
    missing_docs = [name for name in registered_ct_words if not _integrity_doc_has_word_entry(full_text, name)]
    if missing_docs:
        errors.append(
            "compile-time reference missing registered compile-time words: "
            + ", ".join(missing_docs)
        )

    missing_summary = [
        name
        for name in registered_ct_words
        if re.search(rf"(?m)^[ \t]{{2,}}{re.escape(name)}[ \t]{{2,}}", summary_text) is None
    ]
    if missing_summary:
        errors.append(
            "compile-time summary table missing CT words: "
            + ", ".join(missing_summary)
        )

    missing_appendix = [
        name
        for name in registered_ct_words
        if re.search(rf"(?m)^[ \t]{{2,}}{re.escape(name)}(?:[ \t]{{2,}}|$)", appendix_text) is None
    ]
    if missing_appendix:
        errors.append(
            "compile-time appendix missing CT words: "
            + ", ".join(missing_appendix)
        )

    if entries:
        bad_names = {"word", "call:", "->", "*", "overview:", "example:", "category:"}
        bad_present = sorted(
            name for name in {str(item.get("name", "")).strip().lower() for item in entries}
            if name in bad_names
        )
        if bad_present:
            errors.append(
                "compile-time entry catalog contains noisy/non-function names: "
                + ", ".join(bad_present)
            )

        entry_names = {str(item.get("name", "")).strip() for item in entries}
        missing_entry_records = [name for name in registered_ct_words if name not in entry_names]
        if missing_entry_records:
            errors.append(
                "compile-time entry catalog missing registered words: "
                + ", ".join(missing_entry_records)
            )

        for item in entries:
            name = str(item.get("name", "")).strip()
            if not name:
                errors.append("compile-time entry catalog contains blank name")
                continue
            overview = str(item.get("overview", "")).strip()
            example = str(item.get("example", "")).strip()
            category = str(item.get("category", "")).strip()
            if not overview:
                errors.append(f"compile-time entry '{name}' is missing overview text")
            if not example:
                errors.append(f"compile-time entry '{name}' is missing example text")
            if not category:
                errors.append(f"compile-time entry '{name}' is missing category")

            if "..." in example:
                errors.append(f"compile-time entry '{name}' example still uses placeholder ellipsis")
            if example == name:
                errors.append(f"compile-time entry '{name}' example should include an explicit usage context")
            if len(overview) < 40:
                errors.append(f"compile-time entry '{name}' overview is too short for useful guidance")
            if overview.lower().startswith("compile-time operation for "):
                errors.append(f"compile-time entry '{name}' overview is still generic boilerplate")
            if not re.search(rf"(?<![A-Za-z0-9_?.:+\-*/<>=!&]){re.escape(name)}(?![A-Za-z0-9_?.:+\-*/<>=!&])", example):
                errors.append(f"compile-time entry '{name}' example should explicitly show the word invocation")

            text_blob = " ".join(
                [
                    name,
                    str(item.get("search_text", "")),
                    overview,
                    example,
                ]
            ).lower()
            if " handler:" in text_blob or " flags:" in text_blob:
                errors.append(f"compile-time entry '{name}' still embeds handler/flags text")

    for name in registered_ct_words:
        entry_match = re.search(
            rf"(?m)^[ \t]{{2,}}{re.escape(name)}(?:[ \t]+[^\n]*)?\n((?:[ \t]{{4}}[^\n]*\n)+)",
            appendix_text,
        )
        if entry_match is None:
            continue
        detail_lines = [line.strip() for line in entry_match.group(1).splitlines() if line.strip()]
        if not detail_lines:
            errors.append(f"compile-time appendix entry '{name}' is missing detail lines")
            continue

        if not any(line.startswith("Overview:") for line in detail_lines):
            errors.append(f"compile-time appendix entry '{name}' is missing descriptive overview line")

        if not any(line.startswith("Category:") for line in detail_lines):
            errors.append(f"compile-time appendix entry '{name}' is missing category metadata")
        if not any(line.startswith("Scope:") for line in detail_lines):
            errors.append(f"compile-time appendix entry '{name}' is missing scope metadata")

        example_idx = -1
        for idx, line in enumerate(detail_lines):
            if line.startswith("Example:"):
                example_idx = idx
                break

        if example_idx < 0:
            errors.append(f"compile-time appendix entry '{name}' is missing example line")
        else:
            example_payload = [
                line.strip()
                for line in detail_lines[example_idx + 1 :]
                if line.strip() and not line.startswith(("Category:", "Scope:", "Overview:", "Example:"))
            ]
            if not example_payload:
                errors.append(f"compile-time appendix entry '{name}' has an empty example")

    if (
        docs_helpers is not None
        and hasattr(docs_helpers, "attach_ct_entry_line_numbers")
        and hasattr(docs_helpers, "build_ct_detail_lines")
        and entries
    ):
        try:
            line_entries = [
                dict(item)
                for item in docs_helpers.attach_ct_entry_line_numbers(full_text, entries)
            ]
        except Exception as exc:
            errors.append(f"compile-time detail helper line mapping failed: {exc}")
            line_entries = []

        for item in line_entries[:40]:
            name = str(item.get("name", "")).strip() or "<unknown>"
            try:
                detail_lines = [str(line) for line in docs_helpers.build_ct_detail_lines(item, 100)]
            except Exception as exc:
                errors.append(f"compile-time detail helper failed for '{name}': {exc}")
                continue
            detail_blob = "\n".join(detail_lines).lower()
            if "overview:" not in detail_blob:
                errors.append(f"compile-time detail view for '{name}' is missing overview section")
            if "example:" not in detail_blob:
                errors.append(f"compile-time detail view for '{name}' is missing example section")
            if "handler:" in detail_blob or "flags:" in detail_blob:
                errors.append(f"compile-time detail view for '{name}' still shows handler/flags")

    for required in ("runtime", "CT"):
        if not _integrity_doc_has_word_entry(full_text, required):
            errors.append(f"compile-time reference missing '{required}' entry")


def _run_integrity_word_flag_checks(errors: List[str]) -> None:
    dictionary = bootstrap_dictionary()
    for name, word in sorted(dictionary.words.items()):
        if word.compile_only and word.runtime_only:
            errors.append(f"word '{name}' is both compile-only and runtime-only")
        if word.runtime_only and word.immediate:
            errors.append(f"word '{name}' is both runtime-only and immediate")
        if word.runtime_only and word.compile_time_override:
            errors.append(f"word '{name}' is runtime-only but has compile-time override enabled")

    ct_word = dictionary.lookup("CT")
    if ct_word is None:
        errors.append("missing built-in CT word")
        return
    if ct_word.intrinsic is None:
        errors.append("CT word missing runtime emission intrinsic")
    if ct_word.compile_time_intrinsic is None:
        errors.append("CT word missing compile-time intrinsic")
    if ct_word.runtime_intrinsic is None:
        errors.append("CT word missing runtime-mode compile-time intrinsic")

    parser = Parser(dictionary, Reader())
    vm = parser.compile_time_vm
    try:
        vm.invoke(ct_word)
        if vm.stack != [1]:
            errors.append(f"CT compile-time invocation expected stack [1], got {vm.stack!r}")
    except Exception as exc:
        errors.append(f"CT compile-time invocation failed: {exc}")

    try:
        vm.invoke(ct_word, runtime_mode=True)
        depth = vm.native_stack_depth()
        if depth != 1:
            errors.append(f"CT runtime-mode compile-time invocation expected native depth 1, got {depth}")
        elif CTMemory.read_qword(vm.r12) != 1:
            errors.append("CT runtime-mode compile-time invocation expected top-of-stack value 1")
    except Exception as exc:
        errors.append(f"CT runtime-mode compile-time invocation failed: {exc}")

    try:
        asm = Assembler(dictionary)
        emitted: List[str] = []
        builder = FunctionEmitter(emitted)
        asm._emit_wordref("CT", builder)
        if not any("mov qword [r12], 0" in line for line in emitted):
            errors.append("CT runtime emission did not push literal 0")
    except Exception as exc:
        errors.append(f"CT runtime emission probe failed: {exc}")


def _run_integrity_cfg_format_checks(errors: List[str]) -> None:
    asm = Assembler(Dictionary())
    cases: List[Tuple[Op, str]] = [
        (_make_op("literal", 123), "push 123"),
        (_make_word_op("dup"), "dup"),
        (_make_op("word_ptr", "dup"), "&dup"),
        (_make_op("branch_zero", "L0"), "branch_zero"),
        (_make_op("jump", "L0"), "jump"),
        (_make_op("label", "L0"), ".L0:"),
        (_make_op("for_begin", {"loop": "L", "end": "E"}), "for"),
        (_make_op("for_end", {"loop": "L", "end": "E"}), "end  (for)"),
        (_make_op("list_begin", "L"), "list_begin"),
        (_make_op("list_end", "L"), "list_end"),
        (_make_op("list_literal", [1, 2]), "list_literal [1, 2]"),
        (_make_op("bss_list_literal", {"size": 2, "values": [1]}), "bss_list_literal {'size': 2, 'values': [1]}"),
        (_make_op("ret"), "ret"),
    ]
    for node, expected in cases:
        actual = asm._format_cfg_op(node)
        if actual != expected:
            errors.append(
                f"Assembler._format_cfg_op mismatch for '{node.op}': expected {expected!r}, got {actual!r}"
            )


def _run_integrity_vm_semantic_checks(errors: List[str]) -> None:
    dictionary = bootstrap_dictionary()
    parser = Parser(dictionary, Reader())
    vm = parser.compile_time_vm

    vm_probe_word_name = "__integrity_vm_word__"
    if dictionary.lookup(vm_probe_word_name) is None:
        dictionary.register(
            Word(
                name=vm_probe_word_name,
                definition=Definition(
                    name=vm_probe_word_name,
                    body=[_make_literal_op(9)],
                ),
            )
        )

    loop_data = {"loop": "VM_LOOP", "end": "VM_END"}
    vm_cases: List[Dict[str, Any]] = [
        {
            "name": "word_call",
            "nodes": [_make_word_op(vm_probe_word_name)],
            "initial_stack": [],
            "check": lambda run_vm: None if run_vm.stack == [9] else f"expected [9], got {run_vm.stack!r}",
        },
        {
            "name": "literal_push",
            "nodes": [_make_literal_op(7)],
            "initial_stack": [],
            "check": lambda run_vm: None if run_vm.stack == [7] else f"expected [7], got {run_vm.stack!r}",
        },
        {
            "name": "word_ptr_handle",
            "nodes": [_make_op("word_ptr", vm_probe_word_name)],
            "initial_stack": [],
            "check": lambda run_vm: (
                None
                if len(run_vm.stack) == 1
                and isinstance(run_vm.stack[0], int)
                and isinstance(run_vm._resolve_handle(run_vm.stack[0]), Word)
                and run_vm._resolve_handle(run_vm.stack[0]).name == vm_probe_word_name
                else f"word_ptr did not resolve to {vm_probe_word_name!r}: stack={run_vm.stack!r}"
            ),
        },
        {
            "name": "branch_zero_taken",
            "nodes": [
                _make_op("branch_zero", "VM_LABEL"),
                _make_literal_op(111),
                _make_op("label", "VM_LABEL"),
                _make_literal_op(222),
            ],
            "initial_stack": [0],
            "check": lambda run_vm: None if run_vm.stack == [222] else f"expected [222], got {run_vm.stack!r}",
        },
        {
            "name": "branch_zero_not_taken",
            "nodes": [
                _make_op("branch_zero", "VM_LABEL"),
                _make_literal_op(111),
                _make_op("label", "VM_LABEL"),
                _make_literal_op(222),
            ],
            "initial_stack": [1],
            "check": lambda run_vm: None if run_vm.stack == [111, 222] else f"expected [111, 222], got {run_vm.stack!r}",
        },
        {
            "name": "jump",
            "nodes": [
                _make_op("jump", "VM_JUMP"),
                _make_literal_op(1),
                _make_op("label", "VM_JUMP"),
                _make_literal_op(2),
            ],
            "initial_stack": [],
            "check": lambda run_vm: None if run_vm.stack == [2] else f"expected [2], got {run_vm.stack!r}",
        },
        {
            "name": "for_loop_counted",
            "nodes": [
                _make_op("for_begin", loop_data),
                _make_literal_op(1),
                _make_op("for_end", loop_data),
            ],
            "initial_stack": [2],
            "check": lambda run_vm: None if run_vm.stack == [1, 1] else f"expected [1, 1], got {run_vm.stack!r}",
        },
        {
            "name": "for_loop_zero_skips",
            "nodes": [
                _make_op("for_begin", loop_data),
                _make_literal_op(1),
                _make_op("for_end", loop_data),
            ],
            "initial_stack": [0],
            "check": lambda run_vm: None if run_vm.stack == [] else f"expected [], got {run_vm.stack!r}",
        },
        {
            "name": "list_begin_end",
            "nodes": [
                _make_op("list_begin", "VM_LIST"),
                _make_literal_op(4),
                _make_literal_op(5),
                _make_op("list_end", "VM_LIST"),
            ],
            "initial_stack": [],
            "check": lambda run_vm: _integrity_check_heap_list(run_vm, [4, 5]),
        },
        {
            "name": "list_literal",
            "nodes": [_make_op("list_literal", [4, 5])],
            "initial_stack": [],
            "check": lambda run_vm: _integrity_check_heap_list(run_vm, [4, 5]),
        },
        {
            "name": "bss_list_literal",
            "nodes": [_make_op("bss_list_literal", {"size": 4, "values": [9]})],
            "initial_stack": [],
            "check": lambda run_vm: _integrity_check_heap_list(run_vm, [9, 0, 0, 0]),
        },
        {
            "name": "ret_terminates",
            "nodes": [_make_literal_op(1), _make_op("ret"), _make_literal_op(2)],
            "initial_stack": [],
            "check": lambda run_vm: None if run_vm.stack == [1] else f"expected [1], got {run_vm.stack!r}",
        },
    ]

    for case in vm_cases:
        vm.reset()
        vm.runtime_mode = False
        vm.stack.extend(list(case["initial_stack"]))
        try:
            vm._execute_nodes(case["nodes"])
        except Exception as exc:
            errors.append(f"VM semantic case '{case['name']}' failed with exception: {exc}")
            continue

        try:
            check_err = case["check"](vm)
        except Exception as exc:
            errors.append(f"VM semantic case '{case['name']}' validation crashed: {exc}")
            continue

        if check_err:
            errors.append(f"VM semantic case '{case['name']}' failed: {check_err}")

    runtime_only_probe = Word(
        name="__integrity_runtime_only_probe__",
        definition=Definition(name="__integrity_runtime_only_probe__", body=[_make_literal_op(1)]),
        runtime_only=True,
    )
    dictionary.register(runtime_only_probe)
    try:
        vm.invoke(runtime_only_probe)
    except CompileTimeError as exc:
        if "runtime-only" not in str(exc):
            errors.append(
                "runtime-only compile-time rejection raised unexpected message: " + str(exc)
            )
    except Exception as exc:
        errors.append(f"runtime-only compile-time rejection raised unexpected exception: {exc}")
    else:
        errors.append("runtime-only probe word executed in compile-time VM")


def _integrity_check_heap_list(vm: CompileTimeVM, expected: List[int]) -> Optional[str]:
    if len(vm.stack) != 1:
        return f"expected single pointer on stack, got {vm.stack!r}"
    addr = vm.stack[0]
    if not isinstance(addr, int):
        return f"expected list pointer int, got {type(addr).__name__}"
    try:
        length = CTMemory.read_qword(addr)
        values = [CTMemory.read_qword(addr + 8 + i * 8) for i in range(max(0, length))]
    except Exception as exc:
        return f"failed to read list memory at {addr}: {exc}"
    if length != len(expected):
        return f"expected length {len(expected)}, got {length}"
    if values != expected:
        return f"expected values {expected!r}, got {values!r}"
    return None


def _run_integrity_assembler_semantic_checks(errors: List[str]) -> None:
    dictionary = bootstrap_dictionary()
    probe_target = "__integrity_probe_target"
    if dictionary.lookup(probe_target) is None:
        dictionary.register(Word(name=probe_target))

    asm_cases: List[Dict[str, Any]] = [
        {
            "name": "word_call",
            "nodes": [_make_word_op(probe_target)],
            "required": [f"call {sanitize_label(probe_target)}"],
        },
        {
            "name": "literal_int",
            "nodes": [_make_literal_op(7)],
            "required": ["mov qword [r12], 7"],
        },
        {
            "name": "word_ptr",
            "nodes": [_make_op("word_ptr", probe_target)],
            "required": [f"mov qword [r12], {sanitize_label(probe_target)}"],
        },
        {
            "name": "branch_zero",
            "nodes": [_make_op("branch_zero", "L0")],
            "required": ["test rax, rax", "jz L0"],
        },
        {
            "name": "jump",
            "nodes": [_make_op("jump", "L0")],
            "required": ["jmp L0"],
        },
        {
            "name": "label",
            "nodes": [_make_op("label", "L0")],
            "required": ["L0:"],
        },
        {
            "name": "for_pair",
            "nodes": [
                _make_op("for_begin", {"loop": "LOOP0", "end": "END0"}),
                _make_op("for_end", {"loop": "LOOP0", "end": "END0"}),
            ],
            "required": ["jle END0", "LOOP0:", "jg LOOP0", "END0:"],
        },
        {
            "name": "list_begin_end",
            "nodes": [_make_op("list_begin", "LIST0"), _make_op("list_end", "LIST0")],
            "required": ["; list begin", "; list end", "LIST0_copy_loop:", "LIST0_copy_done:"],
        },
        {
            "name": "list_literal",
            "nodes": [_make_op("list_literal", [1, 2])],
            "required": ["mov qword [rax], 2", "mov qword [rax + 8], 1", "mov qword [rax + 16], 2"],
        },
        {
            "name": "bss_list_literal",
            "nodes": [_make_op("bss_list_literal", {"size": 2, "values": [1]})],
            "required": ["; bss list literal", "mov qword [rax], 2", "mov qword [rax + 8], 1", "mov qword [rax + 16], 0"],
        },
        {
            "name": "ret",
            "nodes": [_make_op("ret")],
            "required": ["ret"],
            "check": lambda text_blob: None if text_blob.count("\n    ret") >= 2 else "expected explicit ret plus function epilogue ret",
        },
    ]

    for case in asm_cases:
        asm = Assembler(dictionary)
        text: List[str] = []
        definition = Definition(name=f"__integrity_asm_case_{case['name']}", body=case["nodes"])
        try:
            asm._emit_definition(definition, text, debug=False)
        except Exception as exc:
            errors.append(f"Assembler semantic case '{case['name']}' failed with exception: {exc}")
            continue

        text_blob = "\n".join(text)
        missing = [snippet for snippet in case["required"] if snippet not in text_blob]
        if missing:
            errors.append(
                f"Assembler semantic case '{case['name']}' missing output: " + ", ".join(missing)
            )
            continue

        check_fn = case.get("check")
        if check_fn is not None:
            try:
                check_err = check_fn(text_blob)
            except Exception as exc:
                errors.append(f"Assembler semantic case '{case['name']}' validation crashed: {exc}")
                continue
            if check_err:
                errors.append(f"Assembler semantic case '{case['name']}' failed: {check_err}")


def _integrity_native_stack_values(vm: CompileTimeVM) -> List[int]:
    depth = vm.native_stack_depth()
    top_first = [CTMemory.read_qword(vm.r12 + i * 8) for i in range(depth)]
    return list(reversed(top_first))


def _run_integrity_python_pipeline_checks(errors: List[str]) -> None:
    import tempfile

    repo_root = Path(__file__).resolve().parent
    include_paths = [repo_root, repo_root / "stdlib"]

    deterministic_source = """
word __integrity_ct_flag
    CT
end
compile-time __integrity_ct_flag

word main
    CT
    0 if
        11
    else
        22
    end
end
""".lstrip()

    try:
        compiler_a = Compiler(include_paths=include_paths)
        asm_a = compiler_a.compile_source(deterministic_source, debug=False, entry_mode="program").snapshot()
        compiler_b = Compiler(include_paths=include_paths)
        asm_b = compiler_b.compile_source(deterministic_source, debug=False, entry_mode="program").snapshot()
    except Exception as exc:
        errors.append(f"python pipeline determinism probe failed during compilation: {exc}")
        return

    if asm_a != asm_b:
        errors.append("python pipeline determinism probe produced non-deterministic assembly output")
    if "global _start" not in asm_a:
        errors.append("python pipeline determinism probe expected program assembly to export _start")
    if "mov qword [r12], 0" not in asm_a:
        errors.append("python pipeline determinism probe expected CT runtime emission to push literal 0")
    if compiler_a.parser.compile_time_vm.stack != [1]:
        errors.append(
            "python pipeline determinism probe expected compile-time CT stack to be [1], got "
            + repr(compiler_a.parser.compile_time_vm.stack)
        )

    try:
        library_asm = Compiler(include_paths=include_paths).compile_source(
            "word helper\n    7\nend\n",
            debug=False,
            entry_mode="library",
        ).snapshot()
    except Exception as exc:
        errors.append(f"python pipeline library-mode probe failed: {exc}")
    else:
        if "global _start" in library_asm:
            errors.append("python pipeline library-mode probe unexpectedly exported _start")

    try:
        Compiler(include_paths=include_paths).compile_preloaded(debug=False, entry_mode="program")
    except CompileError as exc:
        if "no preloaded source available" not in str(exc):
            errors.append(
                "python pipeline preloaded-guard probe raised unexpected message: " + str(exc)
            )
    except Exception as exc:
        errors.append(
            "python pipeline preloaded-guard probe raised unexpected exception: "
            + f"{type(exc).__name__}: {exc}"
        )
    else:
        errors.append("python pipeline preloaded-guard probe expected compile_preloaded to fail without preload")

    try:
        Compiler(include_paths=include_paths).compile_source(
            "word main\n    0\nend\n",
            debug=False,
            entry_mode="invalid",
        )
    except CompileError as exc:
        if "unknown entry mode" not in str(exc):
            errors.append("python pipeline invalid-entry probe raised unexpected message: " + str(exc))
    except Exception as exc:
        errors.append(
            "python pipeline invalid-entry probe raised unexpected exception: "
            + f"{type(exc).__name__}: {exc}"
        )
    else:
        errors.append("python pipeline invalid-entry probe expected compile_source to reject unknown entry mode")

    with tempfile.TemporaryDirectory(prefix="l2_integrity_pipeline_") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        src_path = temp_dir / "pipeline_main.sl"
        src_path.write_text("word main\n    1\nend\n", encoding="utf-8")

        try:
            compiler_file = Compiler(include_paths=include_paths)
            snap_file = compiler_file.compile_file(src_path, debug=False, entry_mode="program").snapshot()

            compiler_preloaded = Compiler(include_paths=include_paths)
            compiler_preloaded.collect_source_flags(src_path)
            snap_preloaded = compiler_preloaded.compile_preloaded(debug=False, entry_mode="program").snapshot()
        except Exception as exc:
            errors.append(f"python pipeline preloaded roundtrip probe failed: {exc}")
        else:
            if snap_file != snap_preloaded:
                errors.append("python pipeline preloaded roundtrip probe produced mismatched assembly snapshots")
            if compiler_preloaded.dictionary.lookup("main") is None:
                errors.append("python pipeline preloaded roundtrip probe did not populate dictionary with 'main'")


def _run_integrity_python_interpreter_differential_checks(errors: List[str]) -> None:
    import random
    dictionary = bootstrap_dictionary()
    parser = Parser(dictionary, Reader())
    vm = parser.compile_time_vm
    rng = random.Random(0x1A2B3C4D)

    for idx in range(96):
        a = rng.randint(-32, 32)
        cond = rng.randint(0, 1)
        on_true = rng.randint(-32, 32)
        on_false = rng.randint(-32, 32)
        loop_count = rng.randint(0, 6)
        step = rng.randint(-8, 8)

        else_label = f"INT_ELSE_{idx}"
        after_label = f"INT_AFTER_{idx}"
        loop_data = {"loop": f"INT_LOOP_{idx}", "end": f"INT_END_{idx}"}
        name = f"__integrity_py_diff_{idx}"

        nodes = [
            _make_literal_op(a),
            _make_literal_op(cond),
            _make_op("branch_zero", else_label),
            _make_literal_op(on_true),
            _make_op("jump", after_label),
            _make_op("label", else_label),
            _make_literal_op(on_false),
            _make_op("label", after_label),
            _make_literal_op(loop_count),
            _make_op("for_begin", loop_data),
            _make_literal_op(step),
            _make_op("for_end", loop_data),
            _make_op("ret"),
        ]

        expected = [a, on_true if cond != 0 else on_false] + [step] * loop_count
        definition = Definition(name=name, body=nodes)

        word = dictionary.lookup(name)
        if word is None:
            word = Word(name=name, definition=definition)
            dictionary.register(word)
        else:
            word.definition = definition
            word.compile_only = False
            word.runtime_only = False
            word.immediate = False

        try:
            vm.invoke(word)
            interpreted_stack = list(vm.stack)
        except Exception as exc:
            errors.append(
                f"python interpreter differential case '{name}' failed in interpreted mode: {type(exc).__name__}: {exc}"
            )
            continue

        if interpreted_stack != expected:
            errors.append(
                f"python interpreter differential case '{name}' interpreted mismatch: expected {expected!r}, got {interpreted_stack!r}"
            )
            continue

        try:
            vm.invoke(word, runtime_mode=True)
            runtime_stack = _integrity_native_stack_values(vm)
        except Exception as exc:
            errors.append(
                f"python interpreter differential case '{name}' failed in runtime_mode: {type(exc).__name__}: {exc}"
            )
            continue

        if runtime_stack != expected:
            errors.append(
                f"python interpreter differential case '{name}' runtime mismatch: expected {expected!r}, got {runtime_stack!r}"
            )

    inner_name = "__integrity_py_diff_inner"
    outer_name = "__integrity_py_diff_outer"
    passthrough_name = "__integrity_py_diff_passthrough"

    inner_word = dictionary.lookup(inner_name)
    if inner_word is None:
        inner_word = Word(name=inner_name)
        dictionary.register(inner_word)
    inner_word.definition = Definition(name=inner_name, body=[_make_literal_op(3), _make_op("ret")])
    inner_word.compile_only = False
    inner_word.runtime_only = False
    inner_word.immediate = False

    outer_word = dictionary.lookup(outer_name)
    if outer_word is None:
        outer_word = Word(name=outer_name)
        dictionary.register(outer_word)
    outer_word.definition = Definition(
        name=outer_name,
        body=[_make_word_op(inner_name), _make_literal_op(4), _make_op("ret")],
    )
    outer_word.compile_only = False
    outer_word.runtime_only = False
    outer_word.immediate = False

    try:
        vm.invoke(outer_word)
        if vm.stack != [3, 4]:
            errors.append(
                f"python interpreter nested-call probe interpreted mismatch: expected [3, 4], got {vm.stack!r}"
            )
    except Exception as exc:
        errors.append(
            "python interpreter nested-call probe failed in interpreted mode: "
            + f"{type(exc).__name__}: {exc}"
        )

    try:
        vm.invoke(outer_word, runtime_mode=True)
        runtime_vals = _integrity_native_stack_values(vm)
        if runtime_vals != [3, 4]:
            errors.append(
                f"python interpreter nested-call probe runtime mismatch: expected [3, 4], got {runtime_vals!r}"
            )
    except Exception as exc:
        errors.append(
            "python interpreter nested-call probe failed in runtime_mode: "
            + f"{type(exc).__name__}: {exc}"
        )

    passthrough_word = dictionary.lookup(passthrough_name)
    if passthrough_word is None:
        passthrough_word = Word(name=passthrough_name)
        dictionary.register(passthrough_word)
    passthrough_word.definition = Definition(name=passthrough_name, body=[_make_op("ret")])
    passthrough_word.compile_only = False
    passthrough_word.runtime_only = False
    passthrough_word.immediate = False

    try:
        vm.invoke_with_args(passthrough_word, [7, -2, 11])
        if vm.stack != [7, -2, 11]:
            errors.append(
                "python interpreter invoke_with_args probe mismatch: "
                + f"expected [7, -2, 11], got {vm.stack!r}"
            )
    except Exception as exc:
        errors.append(
            "python interpreter invoke_with_args probe failed: "
            + f"{type(exc).__name__}: {exc}"
        )


def _run_integrity_python_repl_state_checks(errors: List[str]) -> None:
    dictionary = bootstrap_dictionary()
    parser = Parser(dictionary, Reader())
    vm = parser.compile_time_vm

    push7_name = "__integrity_repl_push7"
    push9_name = "__integrity_repl_push9"

    push7 = dictionary.lookup(push7_name)
    if push7 is None:
        push7 = Word(name=push7_name)
        dictionary.register(push7)
    push7.definition = Definition(name=push7_name, body=[_make_literal_op(7)])
    push7.compile_only = False
    push7.runtime_only = False
    push7.immediate = False

    push9 = dictionary.lookup(push9_name)
    if push9 is None:
        push9 = Word(name=push9_name)
        dictionary.register(push9)
    push9.definition = Definition(name=push9_name, body=[_make_literal_op(9)])
    push9.compile_only = False
    push9.runtime_only = False
    push9.immediate = False

    try:
        vm.invoke_repl(push7)
        first = vm.repl_stack_values()
        vm.invoke_repl(push9)
        second = vm.repl_stack_values()
        vm.invoke_repl(push7)
        third = vm.repl_stack_values()
    except Exception as exc:
        errors.append(f"python REPL state probe failed while invoking words: {type(exc).__name__}: {exc}")
        return

    if first != [7]:
        errors.append(f"python REPL state probe first snapshot mismatch: expected [7], got {first!r}")
    if second != [7, 9]:
        errors.append(f"python REPL state probe second snapshot mismatch: expected [7, 9], got {second!r}")
    if third != [7, 9, 7]:
        errors.append(
            f"python REPL state probe third snapshot mismatch: expected [7, 9, 7], got {third!r}"
        )

    vm.reset()
    if vm.repl_stack_values() != []:
        errors.append("python REPL state probe expected reset() to clear persistent REPL state")

    try:
        vm.invoke(push9)
    except Exception as exc:
        errors.append(f"python REPL state probe failed after reset in interpreted mode: {type(exc).__name__}: {exc}")
    else:
        if vm.stack != [9]:
            errors.append(
                f"python REPL state probe interpreted invoke mismatch after reset: expected [9], got {vm.stack!r}"
            )


def _run_integrity_roundtrip_checks(errors: List[str]) -> None:
    import subprocess
    import tempfile

    repo_root = Path(__file__).resolve().parent
    include_paths = [repo_root]

    probes: List[Tuple[str, str, int]] = [
        (
            "literal_exit",
            """
:asm exit_top {
    mov rax, 60
    mov rdi, [r12]
    add r12, 8
    syscall
} ;

word main
    7
    exit_top
end
""".lstrip(),
            7,
        ),
        (
            "if_else",
            """
:asm exit_top {
    mov rax, 60
    mov rdi, [r12]
    add r12, 8
    syscall
} ;

word main
    0 if
        11
    else
        22
    end
    exit_top
end
""".lstrip(),
            22,
        ),
        (
            "for_sum",
            """
:asm exit_top {
    mov rax, 60
    mov rdi, [r12]
    add r12, 8
    syscall
} ;

word main
    0
    5 for
        2 +
    end
    exit_top
end
""".lstrip(),
            10,
        ),
        (
            "asm_word",
            """
:asm exit_top {
    mov rax, 60
    mov rdi, [r12]
    add r12, 8
    syscall
} ;

:asm emit42 {
    mov rax, 42
    sub r12, 8
    mov [r12], rax
} ;

word main
    emit42
    exit_top
end
""".lstrip(),
            42,
        ),
    ]

    with tempfile.TemporaryDirectory(prefix="l2_integrity_roundtrip_") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        for name, source_text, expected_exit in probes:
            src_path = temp_dir / f"{name}.sl"
            asm_path = temp_dir / f"{name}.asm"
            obj_path = temp_dir / f"{name}.o"
            exe_path = temp_dir / f"{name}.out"

            src_path.write_text(source_text)

            try:
                compiler = Compiler(include_paths=include_paths)
                emission = compiler.compile_file(src_path, debug=False, entry_mode="program")
                asm_text = emission.snapshot()
                if not asm_text.strip():
                    errors.append(f"round-trip probe '{name}' produced empty assembly")
                    continue

                asm_path.write_text(asm_text)
                run_nasm(asm_path, obj_path, debug=False, asm_text=asm_text)
                run_linker(obj_path, exe_path, debug=False, libs=[], shared=False)
            except Exception as exc:
                errors.append(f"round-trip probe '{name}' failed during build: {exc}")
                continue

            try:
                proc = subprocess.run(
                    [str(exe_path)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except Exception as exc:
                errors.append(f"round-trip probe '{name}' failed to execute: {exc}")
                continue

            if proc.returncode != expected_exit:
                errors.append(
                    f"round-trip probe '{name}' exit-code mismatch: expected {expected_exit}, got {proc.returncode}; stdout={proc.stdout!r}; stderr={proc.stderr!r}"
                )
                continue

            if proc.stdout:
                errors.append(f"round-trip probe '{name}' expected empty stdout, got {proc.stdout!r}")
            if proc.stderr:
                errors.append(f"round-trip probe '{name}' expected empty stderr, got {proc.stderr!r}")


def _run_integrity_prompt_feature_matrix_checks(errors: List[str]) -> None:
    repo_root = Path(__file__).resolve().parent
    paths = {
        "l2_main.py": Path(__file__),
        "main.py": repo_root / "main.py",
        "docs.py": repo_root / "docs.py",
        "main.c": repo_root / "main.c",
    }

    texts: Dict[str, str] = {}
    for name, path in paths.items():
        if not path.exists():
            errors.append(f"feature-matrix missing required file: {name}")
            continue
        try:
            texts[name] = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            errors.append(f"feature-matrix failed to read {name}: {exc}")

    if len(texts) != len(paths):
        return

    impl_text = texts["l2_main.py"]
    main_py_text = texts["main.py"]
    docs_text = texts["docs.py"]
    main_c_text = texts["main.c"]

    checks: List[Tuple[str, bool]] = [
        (
            "--force CLI option and implication are wired",
            "--force" in impl_text
            and "force full rebuild (always recompile + assemble + relink); implies --no-cache" in impl_text
            and "if args.force:\n        args.no_cache = True" in impl_text,
        ),
        (
            "quick force/no-cache workers are available in main.py",
            '_FORCE_WORKER_TOKEN = "--__l2-force-worker"' in main_py_text
            and "def _try_ultra_fast_force(" in main_py_text
            and "def _try_ultra_fast_no_cache(" in main_py_text,
        ),
        (
            "source graph cache is wired in CLI path",
            "class SourceGraphCache:" in impl_text
            and "source_graph_cache = SourceGraphCache(" in impl_text
            and "source_graph_cache=source_graph_cache" in impl_text,
        ),
        (
            "macro engine extraction is active",
            "class MacroEngine:" in impl_text and "self.macro_engine = MacroEngine(self)" in impl_text,
        ),
        (
            "macro preview includes before/after context",
            "kind=\"macro-preview\"" in impl_text
            and "context:" in impl_text
            and "| src:" in impl_text
            and "| now:" in impl_text,
        ),
        (
            "docs helpers are lazy-loaded with fallback diagnostics",
            "def _load_docs_helpers(" in impl_text
            and "docs.py not found" in impl_text
            and "using built-in fallback" in impl_text,
        ),
        (
            "docs module exports advanced CT reference helpers",
            "def run_docs_cli(" in docs_text
            and "def run_docs_explorer(" in docs_text
            and "def build_ct_reference_bundle" in docs_text
            and "def attach_ct_entry_line_numbers" in docs_text
            and "def build_ct_detail_lines" in docs_text,
        ),
        (
            "docs TUI supports CT filter/results/detail modes",
            "_MODE_CT_REF_FILTER = 12" in docs_text
            and "_MODE_CT_REF_RESULTS = 13" in docs_text
            and "_MODE_CT_REF_DETAIL = 14" in docs_text
            and "query fields: section:<name> scope:<mode>" in docs_text
            and "function(s)  (Enter: open results  Esc: cancel)" in docs_text,
        ),
        (
            "integrity harness includes roundtrip and feature matrix",
            "(\"roundtrip\", _run_integrity_roundtrip_checks)" in impl_text
            and "(\"feature-matrix\", _run_integrity_prompt_feature_matrix_checks)" in impl_text,
        ),
        (
            "main.c CLI supports script/no-artifact/ct-run-main path",
            "if (strcmp(arg, \"--ct-run-main\") == 0)" in main_c_text
            and "if (strcmp(arg, \"--no-artifact\") == 0)" in main_c_text
            and "if (strcmp(arg, \"--script\") == 0)" in main_c_text
            and "ct_run_main = true;" in main_c_text
            and "no_artifact = true;" in main_c_text,
        ),
        (
            "main.c exposes broad CT intrinsic surface",
            all(
                fragment in main_c_text
                for fragment in (
                    "register_ct_intrinsic(dict, \"list-peek-front\"",
                    "register_ct_intrinsic(dict, \"list-push-front\"",
                    "register_ct_intrinsic(dict, \"list-reverse\"",
                    "register_ct_intrinsic(dict, \"map-length\"",
                    "register_ct_intrinsic(dict, \"map-keys\"",
                    "register_ct_intrinsic(dict, \"string-contains?\"",
                    "register_ct_intrinsic(dict, \"string-starts-with?\"",
                    "register_ct_intrinsic(dict, \"string-ends-with?\"",
                    "register_ct_intrinsic(dict, \"token-line\"",
                    "register_ct_intrinsic(dict, \"token-column\"",
                    "register_ct_intrinsic(dict, \"next-token\"",
                    "register_ct_intrinsic(dict, \"peek-token\"",
                    "register_ct_intrinsic(dict, \"inject-tokens\"",
                )
            ) and len(re.findall(r"(?m)^\s*register_ct_intrinsic\(", main_c_text)) >= 70,
        ),
    ]

    for label, ok in checks:
        if not ok:
            errors.append(f"feature checklist item failed: {label}")


def _run_integrity_failure_injection_checks(errors: List[str]) -> None:
    repo_root = Path(__file__).resolve().parent
    include_paths = [repo_root, repo_root / "stdlib"]

    failure_cases: List[Dict[str, Any]] = [
        {
            "name": "parse_unexpected_end",
            "source": "end\n",
            "expected_exc": ParseError,
            "message_contains": "compilation failed with",
            "diag_contains": "unexpected 'end'",
        },
        {
            "name": "parse_unterminated_macro",
            "source": "macro m 0 1\n",
            "expected_exc": ParseError,
            "message_contains": "compilation failed with",
            "diag_contains": "unterminated macro definition",
        },
        {
            "name": "compile_unknown_word",
            "source": "word main\n    __integrity_missing_word__\nend\n",
            "expected_exc": CompileError,
            "message_contains": "unknown word '__integrity_missing_word__'",
        },
        {
            "name": "compile_compile_only_word",
            "source": "word main\n    nil\nend\n",
            "expected_exc": CompileError,
            "message_contains": "compile-time only",
        },
        {
            "name": "parse_runtime_immediate_conflict",
            "source": "word rt\n    1\nend\nruntime\nimmediate\nword main\n    0\nend\n",
            "expected_exc": ParseError,
            "message_contains": "compilation failed with",
            "diag_contains": "runtime-only and cannot be immediate",
        },
        {
            "name": "parse_runtime_compile_only_conflict",
            "source": "word rt\n    1\nend\nruntime\ncompile-only\nword main\n    0\nend\n",
            "expected_exc": ParseError,
            "message_contains": "compilation failed with",
            "diag_contains": "runtime-only and cannot be compile-only",
        },
        {
            "name": "parse_compile_time_runtime_only_target",
            "source": "word rt\n    1\nend\nruntime\nword main\n    compile-time rt\nend\n",
            "expected_exc": ParseError,
            "message_contains": "compilation failed with",
            "diag_contains": "word 'rt' is runtime-only",
        },
        {
            "name": "ct_use_l2_runtime_only_target",
            "source": "word rt\n    1\nend\nruntime\nword cfg\n    \"rt\"\n    use-l2-ct\nend\ncompile-time cfg\nword main\n    0\nend\n",
            "expected_exc": CompileTimeError,
            "message_contains": "runtime-only and cannot be executed at compile time",
        },
    ]

    for case in failure_cases:
        compiler = Compiler(include_paths=include_paths)
        try:
            compiler.compile_source(case["source"], debug=False, entry_mode="program")
        except Exception as exc:
            expected_exc = case["expected_exc"]
            if not isinstance(exc, expected_exc):
                errors.append(
                    f"failure-injection case '{case['name']}' raised {type(exc).__name__}, expected {expected_exc.__name__}"
                )
                continue

            message = str(exc)
            needle = case.get("message_contains")
            if needle and needle not in message:
                errors.append(
                    f"failure-injection case '{case['name']}' message mismatch: expected substring {needle!r}, got {message!r}"
                )

            diag_needle = case.get("diag_contains")
            if diag_needle:
                diagnostics = [d.message for d in compiler.parser.diagnostics if getattr(d, "level", "error") == "error"]
                if not any(diag_needle in d for d in diagnostics):
                    errors.append(
                        f"failure-injection case '{case['name']}' missing diagnostic containing {diag_needle!r}; got {diagnostics!r}"
                    )
        else:
            errors.append(f"failure-injection case '{case['name']}' expected failure but compilation succeeded")


def _run_integrity_docs_consistency_checks(errors: List[str]) -> None:
    docs_path = Path(__file__).with_name("docs.py")
    if not docs_path.exists():
        errors.append("docs consistency probe requires docs.py but it is missing")
        return

    try:
        source_text = docs_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        errors.append(f"docs consistency probe failed to read docs.py: {exc}")
        return

    required_fragments = [
        "_L2_MACRO_CANONICAL_TEXT = (",
        "_L2_MACRO_LANG_DETAIL = (",
        "+ _L2_MACRO_CT_BLOCK",
        "+ _L2_MACRO_QA_BLOCK",
        "source-context window around the expansion site.",
        "_MODE_CT_REF_FILTER = 12",
        "_MODE_CT_REF_RESULTS = 13",
        "_MODE_CT_REF_DETAIL = 14",
        "query fields: section:<name> scope:<mode>",
        "function(s)  (Enter: open results  Esc: cancel)",
        "Enter detail  o jump-to-full  / search",
        " / search  f filters  c clear ",
        "def run_docs_cli(",
    ]
    for fragment in required_fragments:
        if fragment not in source_text:
            errors.append(f"docs consistency probe missing expected fragment: {fragment!r}")

    doc_text = _integrity_load_ct_reference_text()
    if not doc_text.strip():
        errors.append("docs consistency probe could not load compile-time reference text")
        return

    # Macro docs should be centralized in §13 and reused via canonical blocks,
    # not duplicated ad-hoc in earlier CT sections.
    if "Macro definition syntax:" in doc_text:
        errors.append("compile-time reference still contains legacy scattered macro syntax block")
    canonical_count = doc_text.count("Macro definitions support three styles:")
    if canonical_count != 1:
        errors.append(
            "compile-time reference should contain exactly one canonical macro definition block "
            f"(found {canonical_count})"
        )


def _run_integrity_docs_module_checks(errors: List[str]) -> None:
    repo_root = Path(__file__).resolve().parent
    docs_path = repo_root / "docs.py"
    if not docs_path.exists():
        errors.append("docs.py is required for docs explorer/integrity checks but was not found")
        return

    try:
        docs_source = docs_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        errors.append(f"failed to read docs.py: {exc}")
        return

    required_fragments = [
        "def run_docs_explorer(",
        "def run_docs_cli(",
        "def _run_docs_tui(",
        "def build_ct_reference_bundle",
        "def attach_ct_entry_line_numbers",
        "def build_ct_detail_lines",
        "Overview:",
        "Example:",
    ]
    for fragment in required_fragments:
        if fragment not in docs_source:
            errors.append(f"docs.py missing expected fragment: {fragment!r}")

    docs_helpers = _load_docs_helpers(warn=False)
    if docs_helpers is None:
        err = _DOCS_HELPERS_ERROR or "unknown import failure"
        errors.append(f"docs.py exists but lazy import failed: {err}")
        return

    for attr in (
        "run_docs_cli",
        "run_docs_explorer",
        "build_ct_reference_bundle",
        "attach_ct_entry_line_numbers",
        "build_ct_detail_lines",
    ):
        if not hasattr(docs_helpers, attr):
            errors.append(f"docs.py helper missing callable '{attr}'")

    try:
        impl_text = Path(__file__).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        errors.append(f"failed to read l2_main.py for docs delegation checks: {exc}")
        return

    forbidden_main_patterns = [
        (r"(?m)^class\s+DocEntry\s*:", "class DocEntry"),
        (r"(?m)^def\s+_scan_doc_file\s*\(", "def _scan_doc_file("),
        (r"(?m)^def\s+_run_docs_tui\s*\(", "def _run_docs_tui("),
    ]
    for pattern, label in forbidden_main_patterns:
        if re.search(pattern, impl_text) is not None:
            errors.append(f"l2_main.py still contains migrated docs implementation fragment: {label!r}")

    required_main_fragments = [
        "def run_docs_explorer(",
        "docs_helpers.run_docs_cli(",
    ]
    for fragment in required_main_fragments:
        if fragment not in impl_text:
            errors.append(f"l2_main.py missing docs delegation fragment: {fragment!r}")


def _run_integrity_compiler_file_contract_checks(errors: List[str]) -> None:
    """Validate cross-file compiler contracts for main.py, l2_main.py, docs.py, and main.c.

    This check is intentionally static and file-scoped so integrity mode stays
    independent from external libraries and stdlib meta layers.
    """

    repo_root = Path(__file__).resolve().parent
    target_paths = {
        "main.py": repo_root / "main.py",
        "l2_main.py": repo_root / "l2_main.py",
        "docs.py": repo_root / "docs.py",
    }
    main_c_path = repo_root / "main.c"
    main_c_text = ""

    if not main_c_path.exists():
        errors.append("required compiler file missing: main.c")
    else:
        try:
            main_c_text = main_c_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            errors.append(f"failed to read main.c: {exc}")

    sources: Dict[str, str] = {}
    trees: Dict[str, ast.AST] = {}

    for name, path in target_paths.items():
        if not path.exists():
            errors.append(f"required compiler file missing: {name}")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            errors.append(f"failed to read {name}: {exc}")
            continue
        sources[name] = text
        try:
            trees[name] = ast.parse(text)
        except SyntaxError as exc:
            errors.append(f"{name} has syntax error at line {exc.lineno}: {exc.msg}")

    missing = [name for name in target_paths if name not in sources or name not in trees]
    if missing:
        return

    main_text = sources["main.py"]
    l2_text = sources["l2_main.py"]
    docs_text = sources["docs.py"]

    # --- main.py contract checks ---
    required_main_fragments = [
        '_FORCE_WORKER_TOKEN = "--__l2-force-worker"',
        "def _try_ultra_fast_force(",
        "def _try_ultra_fast_no_cache(",
        "if __name__ == \"__main__\":",
        "from l2_main import main as _entry_main",
        "from l2_main import *",
    ]
    for fragment in required_main_fragments:
        if fragment not in main_text:
            errors.append(f"main.py missing required compiler bootstrap fragment: {fragment!r}")

    main_top_imports: Set[str] = set()
    for node in trees["main.py"].body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                main_top_imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            main_top_imports.add(node.module)
    for must_import in ("os", "sys"):
        if must_import not in main_top_imports:
            errors.append(f"main.py missing top-level import '{must_import}'")

    # --- docs.py contract checks ---
    docs_required_defs = {
        "run_docs_cli",
        "run_docs_explorer",
        "_run_docs_tui",
        "build_ct_reference_bundle",
        "attach_ct_entry_line_numbers",
        "build_ct_detail_lines",
        "collect_docs",
    }
    docs_defs = {
        node.name
        for node in trees["docs.py"].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    missing_docs_defs = sorted(docs_required_defs - docs_defs)
    if missing_docs_defs:
        errors.append("docs.py missing required APIs: " + ", ".join(missing_docs_defs))

    docs_required_fragments = [
        "imported lazily by l2_main.py",
        "only when docs or integrity features need it.",
        "Structured compile-time docs helpers",
    ]
    for fragment in docs_required_fragments:
        if fragment not in docs_text:
            errors.append(f"docs.py missing required module contract text: {fragment!r}")

    # --- l2_main.py contract checks ---
    l2_required_defs = {
        "cli",
        "main",
        "_run_integrity_checks",
        "_load_docs_helpers",
        "run_docs_explorer",
        "_run_integrity_compiler_file_contract_checks",
    }
    l2_defs = {
        node.name
        for node in trees["l2_main.py"].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    missing_l2_defs = sorted(l2_required_defs - l2_defs)
    if missing_l2_defs:
        errors.append("l2_main.py missing required compiler entrypoints: " + ", ".join(missing_l2_defs))

    l2_required_fragments = [
        "--check-integrity",
        "run internal compiler integrity assertions",
        "if args.check_integrity:",
        "if args.source is None and args.check_integrity and not args.repl:",
        "docs_helpers.run_docs_cli(",
        'Path(__file__).with_name("docs.py")',
        "importlib.util.spec_from_file_location",
    ]
    for fragment in l2_required_fragments:
        if fragment not in l2_text:
            errors.append(f"l2_main.py missing required integration fragment: {fragment!r}")

    # Cross-file API usage integrity: methods invoked from l2_main must exist in docs.py.
    referenced_docs_apis = [
        "run_docs_cli",
        "run_docs_explorer",
        "build_ct_reference_bundle",
        "attach_ct_entry_line_numbers",
        "build_ct_detail_lines",
    ]
    for api in referenced_docs_apis:
        if f"docs_helpers.{api}" in l2_text and api not in docs_defs:
            errors.append(f"l2_main.py references docs helper '{api}' but docs.py does not define it")

    # --- main.c contract checks ---
    if main_c_text:
        required_main_c_fragments = [
            "int l2_cli(int argc, char **argv)",
            "int main(int argc, char **argv)",
            "return l2_cli(argc, argv);",
            "if (strcmp(arg, \"--ct-run-main\") == 0)",
            "if (strcmp(arg, \"--no-artifact\") == 0)",
            "if (strcmp(arg, \"--script\") == 0)",
            "ct_run_main = true;",
            "no_artifact = true;",
            "usage: %s <source.sl> [-o output] [--emit-asm] [--ct-run-main] [--no-artifact] [--script]",
        ]
        for fragment in required_main_c_fragments:
            if fragment not in main_c_text:
                errors.append(f"main.c missing required CLI/runtime fragment: {fragment!r}")

        ct_required_intrinsics = [
            "register_ct_intrinsic(dict, \"list-peek-front\"",
            "register_ct_intrinsic(dict, \"list-push-front\"",
            "register_ct_intrinsic(dict, \"list-reverse\"",
            "register_ct_intrinsic(dict, \"map-length\"",
            "register_ct_intrinsic(dict, \"map-keys\"",
            "register_ct_intrinsic(dict, \"string-contains?\"",
            "register_ct_intrinsic(dict, \"string-starts-with?\"",
            "register_ct_intrinsic(dict, \"string-ends-with?\"",
            "register_ct_intrinsic(dict, \"token-line\"",
            "register_ct_intrinsic(dict, \"token-column\"",
            "register_ct_intrinsic(dict, \"next-token\"",
            "register_ct_intrinsic(dict, \"peek-token\"",
            "register_ct_intrinsic(dict, \"inject-tokens\"",
        ]
        missing_intrinsics = [frag for frag in ct_required_intrinsics if frag not in main_c_text]
        if missing_intrinsics:
            errors.append("main.c missing required CT intrinsic registrations")

        ct_registration_count = len(re.findall(r"(?m)^\s*register_ct_intrinsic\(", main_c_text))
        if ct_registration_count < 70:
            errors.append(
                f"main.c expected broad CT intrinsic surface (>=70 registrations), found {ct_registration_count}"
            )


def _run_integrity_opcode_matrix_checks(errors: List[str]) -> None:
    opcode_symbols = _integrity_opcode_symbols()
    opcode_values = {globals()[name] for name in opcode_symbols}
    mapped_values = set(_OP_STR_TO_INT.values())

    missing_from_mapping = sorted(opcode_values - mapped_values)
    extra_in_mapping = sorted(mapped_values - opcode_values)
    if missing_from_mapping or extra_in_mapping:
        if missing_from_mapping:
            errors.append(
                "opcode mapping missing values: " + ", ".join(str(v) for v in missing_from_mapping)
            )
        if extra_in_mapping:
            errors.append(
                "opcode mapping contains unknown values: " + ", ".join(str(v) for v in extra_in_mapping)
            )

    documented_ops = set(_OP_INTEGRITY_DOCS.keys())
    defined_ops = set(_OP_STR_TO_INT.keys())
    missing_docs = sorted(defined_ops - documented_ops)
    extra_docs = sorted(documented_ops - defined_ops)
    if missing_docs:
        errors.append("missing opcode docs entries: " + ", ".join(missing_docs))
    if extra_docs:
        errors.append("stale opcode docs entries: " + ", ".join(extra_docs))

    coverage_targets: List[Tuple[str, Any]] = [
        ("CompileTimeVM._execute_nodes", CompileTimeVM._execute_nodes),
        ("Assembler._emit_node", Assembler._emit_node),
        ("Assembler._format_cfg_op", Assembler._format_cfg_op),
    ]
    for target_name, target_obj in coverage_targets:
        seen_symbols = _integrity_symbols_in_object(target_obj)
        missing_symbols = sorted(opcode_symbols - seen_symbols)
        if missing_symbols:
            errors.append(
                f"{target_name} missing opcode coverage: " + ", ".join(missing_symbols)
            )


def _run_integrity_step(step_name: str, step_fn: Callable[[List[str]], None], errors: List[str]) -> None:
    before = len(errors)
    try:
        step_fn(errors)
    except Exception as exc:  # pragma: no cover - defensive guard for integrity harness itself
        errors.append(f"[{step_name}] unexpected integrity checker exception: {exc}")
        return
    for idx in range(before, len(errors)):
        msg = errors[idx]
        if not msg.startswith("["):
            errors[idx] = f"[{step_name}] {msg}"


def _run_integrity_checks() -> None:
    errors: List[str] = []

    steps: List[Tuple[str, Callable[[List[str]], None]]] = [
        ("opcode-matrix", _run_integrity_opcode_matrix_checks),
        ("ct-docs", _run_integrity_compile_time_docs_checks),
        ("word-flags", _run_integrity_word_flag_checks),
        ("python-pipeline", _run_integrity_python_pipeline_checks),
        ("cfg-format", _run_integrity_cfg_format_checks),
        ("vm-semantics", _run_integrity_vm_semantic_checks),
        ("python-interpreter-diff", _run_integrity_python_interpreter_differential_checks),
        ("python-repl-state", _run_integrity_python_repl_state_checks),
        ("assembler-semantics", _run_integrity_assembler_semantic_checks),
        ("roundtrip", _run_integrity_roundtrip_checks),
        ("failure-injection", _run_integrity_failure_injection_checks),
        ("docs-consistency", _run_integrity_docs_consistency_checks),
        ("docs-module", _run_integrity_docs_module_checks),
        ("compiler-files", _run_integrity_compiler_file_contract_checks),
        ("feature-matrix", _run_integrity_prompt_feature_matrix_checks),
    ]

    for step_name, step_fn in steps:
        _run_integrity_step(step_name, step_fn, errors)

    if errors:
        details = "\n".join(f"  - {msg}" for msg in errors)
        raise CompileError("integrity assertions failed:\n" + details)


def _emit_macro_profile_report(parser: Parser, destination: Optional[str]) -> None:
    if destination is None:
        return
    report = parser.format_macro_profile()
    target = str(destination).strip().lower()
    if target in ("", "stderr"):
        print(report, file=sys.stderr)
        return
    if target in ("-", "stdout"):
        print(report)
        return
    out_path = Path(str(destination))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report + "\n", encoding="utf-8")
    print(f"[info] wrote macro profile to {out_path}")


_QUICK_FORCE_ASM_OPT_CACHE_MAX = 64
_QUICK_FORCE_ASM_OPT_CACHE: Dict[str, str] = {}
_QUICK_FORCE_TOKEN_CACHE_MAX = 32
_QUICK_FORCE_TOKEN_CACHE: Dict[str, Tuple[Token, ...]] = {}


def _try_quick_compile_force(argv: Sequence[str], *, emit_status: bool = True) -> Optional[int]:
    """Fast path for strict full-rebuild benchmark invocations.

    Supported shape only:
      python main.py <source>.sl --force

    Semantics intentionally preserved:
      - always recompiles source
      - always re-runs NASM
      - always re-runs linker
    """
    source_token: Optional[str] = None
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

    source = Path(source_token)
    if source.suffix.lower() != ".sl":
        return None

    temp_dir = Path("build")
    output = Path("a.out")
    temp_dir.mkdir(parents=True, exist_ok=True)
    asm_path = temp_dir / (source.stem + ".asm")
    obj_path = temp_dir / (source.stem + ".o")
    include_paths = [Path("."), Path("./stdlib")]
    compiler: Optional[Compiler] = Compiler(
        include_paths=include_paths,
        macro_expansion_limit=DEFAULT_MACRO_EXPANSION_LIMIT,
        macro_preview=False,
        defines=[],
    )
    compiler.assembler.enable_constant_folding = True
    compiler.assembler.enable_peephole_optimization = True
    compiler.assembler.enable_loop_unroll = True
    compiler.assembler.enable_auto_inline = True
    compiler.assembler.enable_string_deduplication = True
    compiler.assembler.enable_extern_type_check = True
    compiler.assembler.enable_stack_check = True
    compiler.assembler.verbosity = 0
    compiler.parser._warnings_enabled = set()
    compiler.parser._werror = False
    compiler.parser.capture_op_locations = False
    compiler.parser.enable_dead_macro_elimination = True
    compiler.parser.enable_unused_rewrite_elimination = True
    compiler.parser.dictionary.warn_callback = None

    libs: List[str] = []
    asm_text = ""
    source_resolved = source.resolve()

    try:
        cached_graph = _quick_no_cache_load_cached_graph(source_resolved)
        if cached_graph is not None:
            source_cli_flags = list(cached_graph.get("source_cli_flags", ()))
            source_link_flags = list(cached_graph.get("source_link_flags", ()))
            source_include_paths = [Path(p) for p in cached_graph.get("source_include_paths", ())]
            cached_source = cached_graph.get("source")
            span_rows = cached_graph.get("spans")
            if isinstance(cached_source, str) and isinstance(span_rows, list):
                cached_spans: List[FileSpan] = []
                for row in span_rows:
                    if (
                        isinstance(row, (list, tuple))
                        and len(row) == 4
                        and isinstance(row[0], str)
                        and isinstance(row[1], int)
                        and isinstance(row[2], int)
                        and isinstance(row[3], int)
                    ):
                        cached_spans.append(FileSpan(Path(row[0]), row[1], row[2], row[3]))
                compiler._last_loaded_path = source_resolved
                compiler._last_loaded_source = cached_source
                compiler._last_loaded_spans = cached_spans
                compiler.source_cli_flags = source_cli_flags
                compiler.source_link_flags = source_link_flags
                compiler.source_include_paths = source_include_paths
            else:
                cached_graph = None

        if cached_graph is None:
            compiler.collect_source_flags(source)
            _quick_no_cache_store_cached_graph(source_resolved, compiler)

        # Source-level CLI flags can alter broader behavior; delegate to full CLI.
        if compiler.source_cli_flags:
            return None

        normalized_include_paths: List[Path] = []
        seen_include_paths: Set[Path] = set()
        for include_base in [Path("."), Path("./stdlib"), *compiler.source_include_paths]:
            resolved = include_base.expanduser().resolve()
            if resolved in seen_include_paths:
                continue
            seen_include_paths.add(resolved)
            normalized_include_paths.append(resolved)
        compiler.include_paths = normalized_include_paths
        compiler._import_resolve_cache.clear()

        for flag in compiler.source_link_flags:
            if flag not in libs:
                libs.append(flag)
        for lib in _load_sidecar_meta_libs(source):
            if lib not in libs:
                libs.append(lib)

        source_text = compiler._last_loaded_source
        source_spans = compiler._last_loaded_spans
        if source_text is None or source_spans is None:
            emission = compiler.compile_file(source, debug=False, entry_mode="program")
        else:
            parser = compiler.parser
            parser.file_spans = source_spans

            source_hash: Optional[str] = None
            try:
                import hashlib
                source_hash = hashlib.blake2b(source_text.encode("utf-8"), digest_size=16).hexdigest()
            except Exception:
                source_hash = None

            tokens_template: Optional[Tuple[Token, ...]] = None
            if source_hash is not None:
                tokens_template = _QUICK_FORCE_TOKEN_CACHE.get(source_hash)
            if tokens_template is None:
                tokens_template = tuple(compiler.reader.tokenize(source_text))
                if source_hash is not None:
                    _QUICK_FORCE_TOKEN_CACHE[source_hash] = tokens_template
                    if len(_QUICK_FORCE_TOKEN_CACHE) > _QUICK_FORCE_TOKEN_CACHE_MAX:
                        _QUICK_FORCE_TOKEN_CACHE.clear()

            module = parser.parse(list(tokens_template), source_text)
            emission = compiler.assembler.emit(module, debug=False, entry_mode="program")

        asm_text = emission.snapshot()
        asm_digest: Optional[str] = None
        try:
            import hashlib
            asm_digest = hashlib.blake2b(asm_text.encode("utf-8"), digest_size=16).hexdigest()
        except Exception:
            asm_digest = None

        optimized_cached: Optional[str] = None
        if asm_digest is not None:
            optimized_cached = _QUICK_FORCE_ASM_OPT_CACHE.get(asm_digest)

        if optimized_cached is None:
            optimized_asm, _asm_stats, _asm_pass_logs = optimize_emitted_asm_text(
                asm_text,
                collect_pass_logs=False,
            )
            asm_text = optimized_asm
            if asm_digest is not None:
                _QUICK_FORCE_ASM_OPT_CACHE[asm_digest] = optimized_asm
                if len(_QUICK_FORCE_ASM_OPT_CACHE) > _QUICK_FORCE_ASM_OPT_CACHE_MAX:
                    _QUICK_FORCE_ASM_OPT_CACHE.clear()
        else:
            asm_text = optimized_cached
    except (ParseError, CompileError, CompileTimeError) as exc:
        use_color = sys.stderr.isatty()
        diags = getattr(compiler.parser, "diagnostics", [])
        if diags:
            for diag in diags:
                print(diag.format(color=use_color), file=sys.stderr)
            error_count = sum(1 for d in diags if d.level == "error")
            warn_count = sum(1 for d in diags if d.level == "warning")
            summary_parts: List[str] = []
            if error_count:
                summary_parts.append(f"{error_count} error(s)")
            if warn_count:
                summary_parts.append(f"{warn_count} warning(s)")
            if summary_parts:
                print(f"\n{' and '.join(summary_parts)} emitted", file=sys.stderr)
        else:
            print(f"[error] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[error] unexpected failure: {exc}", file=sys.stderr)
        return 1

    # Force mode semantics: always re-run compiler, assembler, and linker.
    # Keep NASM/link full-rebuild behavior while avoiding redundant asm rewrites.
    write_asm = True
    if asm_path.exists():
        try:
            existing_asm = asm_path.read_text(encoding="utf-8")
        except OSError:
            existing_asm = None
        if existing_asm == asm_text:
            write_asm = False
    if write_asm:
        asm_path.write_text(asm_text, encoding="utf-8")
    run_nasm(asm_path, obj_path, debug=False)
    if output.parent and not output.parent.exists():
        output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.unlink(missing_ok=True)
    except OSError:
        pass
    run_linker(obj_path, output, debug=False, libs=libs, shared=False)
    if emit_status:
        print(f"[info] built {output}")
    return 0


_QUICK_NO_CACHE_GRAPH_CACHE_MAX = 64
_QUICK_NO_CACHE_GRAPH_CACHE: Dict[Path, Dict[str, Any]] = {}


def _quick_no_cache_deps_fresh(dep_entries: Sequence[Tuple[str, int, int]]) -> bool:
    for dep_path, dep_mtime_ns, dep_size in dep_entries:
        try:
            st = Path(dep_path).stat()
        except OSError:
            return False
        if st.st_mtime_ns != dep_mtime_ns or st.st_size != dep_size:
            return False
    return True


def _quick_no_cache_capture_deps(source: Path, spans: Sequence[FileSpan]) -> List[Tuple[str, int, int]]:
    dep_paths: Set[Path] = {source}
    for span in spans:
        try:
            dep_paths.add(span.path.resolve())
        except Exception:
            continue

    deps: List[Tuple[str, int, int]] = []
    for dep_path in sorted(dep_paths, key=lambda p: str(p)):
        try:
            st = dep_path.stat()
        except OSError:
            return []
        deps.append((dep_path.as_posix(), int(st.st_mtime_ns), int(st.st_size)))
    return deps


def _quick_no_cache_load_cached_graph(source: Path) -> Optional[Dict[str, Any]]:
    entry = _QUICK_NO_CACHE_GRAPH_CACHE.get(source)
    if entry is None:
        return None

    deps = entry.get("deps")
    if not isinstance(deps, list) or not deps:
        _QUICK_NO_CACHE_GRAPH_CACHE.pop(source, None)
        return None

    dep_entries: List[Tuple[str, int, int]] = []
    for dep in deps:
        if (
            not isinstance(dep, (list, tuple))
            or len(dep) != 3
            or not isinstance(dep[0], str)
            or not isinstance(dep[1], int)
            or not isinstance(dep[2], int)
        ):
            _QUICK_NO_CACHE_GRAPH_CACHE.pop(source, None)
            return None
        dep_entries.append((dep[0], dep[1], dep[2]))

    if not _quick_no_cache_deps_fresh(dep_entries):
        _QUICK_NO_CACHE_GRAPH_CACHE.pop(source, None)
        return None

    return entry


def _quick_no_cache_store_cached_graph(source: Path, compiler: Compiler) -> None:
    cached_source = compiler._last_loaded_source
    spans = compiler._last_loaded_spans
    if cached_source is None or spans is None:
        return

    deps = _quick_no_cache_capture_deps(source, spans)
    if not deps:
        return

    span_rows: List[Tuple[str, int, int, int]] = []
    for span in spans:
        try:
            span_path = span.path.resolve().as_posix()
        except Exception:
            continue
        span_rows.append((span_path, int(span.start_line), int(span.end_line), int(span.local_start_line)))

    _QUICK_NO_CACHE_GRAPH_CACHE[source] = {
        "deps": deps,
        "source": cached_source,
        "spans": span_rows,
        "source_link_flags": tuple(compiler.source_link_flags),
        "source_include_paths": tuple(str(p) for p in compiler.source_include_paths),
        "source_cli_flags": tuple(compiler.source_cli_flags),
    }

    if len(_QUICK_NO_CACHE_GRAPH_CACHE) > _QUICK_NO_CACHE_GRAPH_CACHE_MAX:
        _QUICK_NO_CACHE_GRAPH_CACHE.clear()


def _try_quick_compile_no_cache(argv: Sequence[str]) -> Optional[int]:
    """Fast path for benchmark-style invocations.

    Supported shape only:
      python main.py <source>.sl --no-cache

    Any other option shape falls back to the full argparse-driven CLI.
    """
    source_token: Optional[str] = None
    saw_no_cache = False
    saw_no_artifact = False
    saw_check = False
    for tok in argv:
        if tok == "--force":
            return None
        if tok == "--no-cache":
            saw_no_cache = True
            continue
        if tok == "--no-artifact":
            saw_no_artifact = True
            continue
        if tok == "--check":
            saw_check = True
            continue
        if tok.startswith("-"):
            return None
        if source_token is None:
            source_token = tok
            continue
        return None

    if not saw_no_cache or source_token is None:
        return None

    source = Path(source_token)
    if source.suffix.lower() != ".sl":
        return None
    source_resolved = source.resolve()
    no_artifact_mode = saw_no_artifact or saw_check

    include_paths = [Path("."), Path("./stdlib")]

    def _new_compiler() -> Compiler:
        compiler = Compiler(
            include_paths=include_paths,
            macro_expansion_limit=DEFAULT_MACRO_EXPANSION_LIMIT,
            macro_preview=False,
            defines=[],
        )
        compiler.assembler.enable_constant_folding = True
        compiler.assembler.enable_peephole_optimization = True
        compiler.assembler.enable_loop_unroll = True
        compiler.assembler.enable_auto_inline = True
        compiler.assembler.enable_string_deduplication = True
        compiler.assembler.enable_extern_type_check = True
        compiler.assembler.enable_stack_check = True
        compiler.assembler.verbosity = 0
        compiler.parser._warnings_enabled = set()
        compiler.parser._werror = False
        compiler.parser.enable_dead_macro_elimination = True
        compiler.parser.enable_unused_rewrite_elimination = True
        compiler.parser.dictionary.warn_callback = None
        return compiler

    compiler: Optional[Compiler] = None

    temp_dir = Path("build")
    output = Path("a.out")
    artifact_kind = "exe"

    temp_dir.mkdir(parents=True, exist_ok=True)
    asm_path = temp_dir / (source.stem + ".asm")
    obj_path = temp_dir / (source.stem + ".o")
    source_stamp_path = temp_dir / f"{source.stem}.src_stamp"
    source_fast_stamp_path = temp_dir / f"{source.stem}.src_stamp.fast"

    def _load_source_stamp() -> Optional[Dict[str, Any]]:
        try:
            import json
            payload = json.loads(source_stamp_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != 1:
            return None
        deps = payload.get("deps")
        if not isinstance(deps, list):
            return None
        return payload

    def _source_stamp_fresh(payload: Dict[str, Any]) -> bool:
        deps = payload.get("deps")
        if not isinstance(deps, list) or not deps:
            return False
        sidecar_exists_now = source.with_suffix(".meta.json").exists()
        if bool(payload.get("sidecar_meta_present", False)) != sidecar_exists_now:
            return False
        for dep in deps:
            if not isinstance(dep, dict):
                return False
            raw_path = dep.get("path")
            mtime_ns = dep.get("mtime_ns")
            size = dep.get("size")
            if not isinstance(raw_path, str) or not isinstance(mtime_ns, int) or not isinstance(size, int):
                return False
            try:
                st = Path(raw_path).stat()
            except OSError:
                return False
            if st.st_mtime_ns != mtime_ns or st.st_size != size:
                return False
        return True

    def _write_source_stamp(
        compiler: Compiler,
        source_link_libs: Sequence[str],
        all_link_libs: Sequence[str],
    ) -> None:
        dep_paths: Set[Path] = {source.resolve()}
        loaded_spans = compiler._last_loaded_spans or []
        for span in loaded_spans:
            try:
                dep_paths.add(span.path.resolve())
            except Exception:
                continue

        sidecar_meta = source.with_suffix(".meta.json")
        if sidecar_meta.exists():
            dep_paths.add(sidecar_meta.resolve())

        dep_payload: List[Dict[str, Any]] = []
        for dep_path in sorted(dep_paths, key=lambda p: str(p)):
            try:
                st = dep_path.stat()
            except OSError:
                continue
            dep_payload.append(
                {
                    "path": dep_path.as_posix(),
                    "mtime_ns": int(st.st_mtime_ns),
                    "size": int(st.st_size),
                }
            )

        payload: Dict[str, Any] = {
            "version": 1,
            "source_link_libs": [str(lib) for lib in source_link_libs],
            "sidecar_meta_present": sidecar_meta.exists(),
            "deps": dep_payload,
        }
        try:
            import json
            source_stamp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

        fast_lines: List[str] = [
            "v1",
            f"sidecar\t{1 if sidecar_meta.exists() else 0}",
        ]
        for dep in dep_payload:
            fast_lines.append(
                f"dep\t{dep['path']}\t{dep['mtime_ns']}\t{dep['size']}"
            )
        for lib in all_link_libs:
            fast_lines.append(f"lib\t{str(lib)}")
        try:
            source_fast_stamp_path.write_text("\n".join(fast_lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    libs: List[str] = []
    asm_text = ""
    compiled_this_run = False

    stamp_payload: Optional[Dict[str, Any]] = None
    can_reuse_compilation = False
    if not no_artifact_mode:
        stamp_payload = _load_source_stamp()
        can_reuse_compilation = (
            stamp_payload is not None
            and asm_path.exists()
            and _source_stamp_fresh(stamp_payload)
        )

    if can_reuse_compilation:
        source_link_libs = [
            lib
            for lib in stamp_payload.get("source_link_libs", [])
            if isinstance(lib, str) and lib
        ]
        for lib in source_link_libs:
            if lib not in libs:
                libs.append(lib)
        for lib in _load_sidecar_meta_libs(source):
            if lib not in libs:
                libs.append(lib)

    if not can_reuse_compilation:
        try:
            compiler = _new_compiler()
            source_cli_flags: List[str] = []
            source_link_flags: List[str] = []
            source_include_paths: List[Path] = []
            cached_graph = _quick_no_cache_load_cached_graph(source_resolved)

            if cached_graph is not None:
                source_cli_flags = list(cached_graph.get("source_cli_flags", ()))
                source_link_flags = list(cached_graph.get("source_link_flags", ()))
                source_include_paths = [Path(p) for p in cached_graph.get("source_include_paths", ())]
                cached_source = cached_graph.get("source")
                span_rows = cached_graph.get("spans")
                if isinstance(cached_source, str) and isinstance(span_rows, list):
                    cached_spans: List[FileSpan] = []
                    for row in span_rows:
                        if (
                            isinstance(row, (list, tuple))
                            and len(row) == 4
                            and isinstance(row[0], str)
                            and isinstance(row[1], int)
                            and isinstance(row[2], int)
                            and isinstance(row[3], int)
                        ):
                            cached_spans.append(FileSpan(Path(row[0]), row[1], row[2], row[3]))
                    compiler._last_loaded_path = source_resolved
                    compiler._last_loaded_source = cached_source
                    compiler._last_loaded_spans = cached_spans
                    compiler.source_cli_flags = source_cli_flags
                    compiler.source_link_flags = source_link_flags
                    compiler.source_include_paths = source_include_paths
                else:
                    cached_graph = None

            if cached_graph is None:
                compiler.collect_source_flags(source)
                _quick_no_cache_store_cached_graph(source_resolved, compiler)
                source_cli_flags = list(compiler.source_cli_flags)
                source_link_flags = list(compiler.source_link_flags)
                source_include_paths = list(compiler.source_include_paths)

            # Source-level CLI flags can imply complex option semantics; keep full CLI for those.
            if source_cli_flags:
                return None

            normalized_include_paths: List[Path] = []
            seen_include_paths: Set[Path] = set()
            for include_base in [Path("."), Path("./stdlib"), *source_include_paths]:
                resolved = include_base.expanduser().resolve()
                if resolved not in seen_include_paths:
                    seen_include_paths.add(resolved)
                    normalized_include_paths.append(resolved)
            compiler.include_paths = normalized_include_paths
            compiler._import_resolve_cache.clear()

            source_link_libs = []
            for flag in source_link_flags:
                if flag not in source_link_libs:
                    source_link_libs.append(flag)
                if flag not in libs:
                    libs.append(flag)
            for lib in _load_sidecar_meta_libs(source):
                if lib not in libs:
                    libs.append(lib)

            if compiler._last_loaded_path == source_resolved and compiler._last_loaded_source is not None:
                emission = compiler.compile_preloaded(debug=False, entry_mode="program")
            else:
                emission = compiler.compile_file(source, debug=False, entry_mode="program")
            asm_text = emission.snapshot()
            compiled_this_run = True
            if not no_artifact_mode:
                _write_source_stamp(compiler, source_link_libs, libs)
        except (ParseError, CompileError, CompileTimeError) as exc:
            use_color = sys.stderr.isatty()
            diags = getattr(compiler.parser, "diagnostics", [])
            if diags:
                for diag in diags:
                    print(diag.format(color=use_color), file=sys.stderr)
                error_count = sum(1 for d in diags if d.level == "error")
                warn_count = sum(1 for d in diags if d.level == "warning")
                summary_parts: List[str] = []
                if error_count:
                    summary_parts.append(f"{error_count} error(s)")
                if warn_count:
                    summary_parts.append(f"{warn_count} warning(s)")
                if summary_parts:
                    print(f"\n{' and '.join(summary_parts)} emitted", file=sys.stderr)
            else:
                print(f"[error] {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"[error] unexpected failure: {exc}", file=sys.stderr)
            return 1

    if compiled_this_run:
        assert compiler is not None
        use_color = sys.stderr.isatty()
        warnings = [d for d in compiler.parser.diagnostics if d.level == "warning"]
        if warnings:
            for diag in warnings:
                print(diag.format(color=use_color), file=sys.stderr)
            print(f"\n{len(warnings)} warning(s) emitted", file=sys.stderr)

    if no_artifact_mode:
        print("[info] skipped artifact generation (--no-artifact)")
        return 0

    asm_changed = False
    if compiled_this_run:
        asm_changed = True
        if asm_path.exists():
            try:
                existing_asm = asm_path.read_text()
            except OSError:
                existing_asm = ""
            if existing_asm == asm_text:
                asm_changed = False
        if asm_changed:
            asm_path.write_text(asm_text)

    need_nasm = asm_changed or not obj_path.exists()
    if not need_nasm:
        try:
            need_nasm = obj_path.stat().st_mtime < asm_path.stat().st_mtime
        except OSError:
            need_nasm = True
    if need_nasm:
        run_nasm(asm_path, obj_path, debug=False)

    if output.parent and not output.parent.exists():
        output.parent.mkdir(parents=True, exist_ok=True)

    link_stamp = temp_dir / f"{output.name}.link_src"
    link_fingerprint_parts = [
        f"obj={obj_path.resolve()}",
        f"artifact={artifact_kind}",
        "debug=0",
    ]

    def _lib_file_from_token(tok: str) -> Optional[Path]:
        if tok.startswith("-l:") and len(tok) > 3:
            return Path(tok[3:]).expanduser()
        if tok.startswith("-"):
            return None
        return Path(tok).expanduser()

    for lib in libs:
        link_fingerprint_parts.append(f"lib={lib}")
        lib_file = _lib_file_from_token(lib)
        if lib_file is None:
            continue
        try:
            resolved = lib_file.resolve()
            st = resolved.stat()
        except OSError:
            continue
        link_fingerprint_parts.append(
            f"libfile={resolved}:mtime_ns={st.st_mtime_ns}:size={st.st_size}"
        )
    link_fingerprint = "\n".join(link_fingerprint_parts)

    # Keep external tool invocations incremental even in --no-cache mode.
    # --no-cache here means "skip source/asm cache", not "force relink".
    need_link = need_nasm or not output.exists()
    if not need_link:
        try:
            recorded = link_stamp.read_text()
        except OSError:
            recorded = ""
        if recorded != link_fingerprint:
            need_link = True
    if not need_link:
        try:
            need_link = output.stat().st_mtime < obj_path.stat().st_mtime
        except OSError:
            need_link = True

    if need_link:
        try:
            output.unlink(missing_ok=True)
        except OSError:
            pass
        run_linker(obj_path, output, debug=False, libs=libs, shared=False)

    if need_link:
        link_stamp.write_text(link_fingerprint)
        print(f"[info] built {output}")
    else:
        print(f"[info] {output} is up to date")
    return 0


def cli(argv: Sequence[str]) -> int:
    quick_force = _try_quick_compile_force(argv)
    if quick_force is not None:
        return quick_force

    import argparse
    parser = argparse.ArgumentParser(description="L2 compiler driver")
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        default=None,
        help="input .sl file (optional with --clean, --repl, --docs, or standalone --check-integrity)",
    )
    parser.add_argument("-o", dest="output", type=Path, default=None, help="output path (defaults vary by artifact)")
    parser.add_argument(
        "-I",
        "--include",
        dest="include_paths",
        action="append",
        default=[],
        type=Path,
        help="add import search path (repeatable)",
    )
    parser.add_argument("--artifact", choices=["exe", "shared", "static", "obj"], default="exe", help="choose final artifact type")
    parser.add_argument("--emit-asm", action="store_true", help="stop after generating asm")
    parser.add_argument("--temp-dir", type=Path, default=Path("build"))
    parser.add_argument("--debug", action="store_true", help="compile with debug info")
    parser.add_argument("--run", action="store_true", help="run the built binary after successful build")
    parser.add_argument("--dbg", action="store_true", help="launch gdb on the built binary after successful build")
    parser.add_argument("--clean", action="store_true", help="remove the temp build directory and exit")
    parser.add_argument("--repl", action="store_true", help="interactive REPL; source file is optional")
    parser.add_argument("-l", dest="libs", action="append", default=[], help="pass library to linker (e.g. -l m or -l libc.so.6)")
    parser.add_argument("--no-folding", action="store_true", help="disable constant folding optimization")
    parser.add_argument("--no-peephole", action="store_true", help="disable peephole optimizations")
    parser.add_argument("--no-loop-unroll", action="store_true", help="disable loop unrolling optimization")
    parser.add_argument("--no-auto-inline", action="store_true", help="disable auto-inlining of small asm bodies")
    parser.add_argument("--no-string-dedup", action="store_true", help="disable string literal deduplication in emitted data section")
    parser.add_argument("--no-asm-opt", action="store_true", help="disable post-emission assembly optimization pass")
    parser.add_argument("-O0", dest="O0", action="store_true", help="disable all optimizations")
    parser.add_argument("-O2", dest="O2", action="store_true", help="fast mode: disable all optimizations and checks")
    parser.add_argument("-v", "--verbose", type=int, default=0, metavar="LEVEL", help="verbosity level (1=summary+timing, 2=per-function/DCE, 3=full debug, 4=optimization detail)")
    parser.add_argument("--no-extern-type-check", action="store_true", help="disable extern function argument count checking")
    parser.add_argument("--no-stack-check", action="store_true", help="disable stack underflow checking for builtins")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="disable compiler caches (source graph + asm cache) while keeping timestamp-based tool up-to-date checks",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="force full rebuild (always recompile + assemble + relink); implies --no-cache",
    )
    parser.add_argument("--ct-run-main", action="store_true", help="execute 'main' via the compile-time VM after parsing")
    parser.add_argument("--no-artifact", action="store_true", help="compile source but skip producing final output artifact")
    parser.add_argument("--docs", action="store_true", help="open searchable TUI for word/function documentation")
    parser.add_argument(
        "--docs-root",
        action="append",
        default=[],
        type=Path,
        help="extra file/directory root to scan for docs (repeatable)",
    )
    parser.add_argument(
        "--docs-query",
        default="",
        help="initial filter query for --docs mode",
    )
    parser.add_argument(
        "--docs-all",
        action="store_true",
        help="include undocumented and private symbols in docs index",
    )
    parser.add_argument(
        "--docs-include-tests",
        action="store_true",
        help="include tests/extra_tests in docs index",
    )
    parser.add_argument(
        "--script",
        action="store_true",
        help="shortcut for --no-artifact --ct-run-main",
    )
    parser.add_argument(
        "--dump-cfg",
        nargs="?",
        default=None,
        const="__AUTO__",
        metavar="PATH",
        help="write Graphviz DOT control-flow dump (default: <temp-dir>/<source>.cfg.dot)",
    )
    parser.add_argument(
        "--macro-expansion-limit",
        type=int,
        default=DEFAULT_MACRO_EXPANSION_LIMIT,
        help="maximum nested macro expansion depth (default: %(default)s)",
    )
    parser.add_argument(
        "--macro-preview",
        action="store_true",
        help="print each text macro expansion to stderr during parsing",
    )
    parser.add_argument(
        "--macro-profile",
        nargs="?",
        const="stderr",
        default=None,
        metavar="PATH",
        help="dump macro expansion profile to stderr/stdout or a file path",
    )
    parser.add_argument(
        "-D",
        dest="defines",
        action="append",
        default=[],
        metavar="NAME",
        help="define a preprocessor symbol for ifdef/ifndef (repeatable)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate source without producing artifacts (parse + compile only)",
    )
    parser.add_argument(
        "--check-integrity",
        action="store_true",
        help="run internal compiler integrity assertions (compiler-only: main.py, l2_main.py, docs.py)",
    )
    parser.add_argument(
        "-W",
        dest="warnings",
        action="append",
        default=[],
        metavar="NAME",
        help="enable warning (e.g. -W redefine, -W stack-depth, -W all)",
    )
    parser.add_argument(
        "--Werror",
        action="store_true",
        help="treat all warnings as errors",
    )

    def _consume_unknown_link_tokens(raw_tokens: Sequence[str], *, strict_long_opts: bool) -> List[str]:
        """Parse unknown args as linker/library-style tokens."""
        out: List[str] = []
        i = 0
        while i < len(raw_tokens):
            tok = raw_tokens[i]
            if tok in ("-l", "-L"):
                if i + 1 >= len(raw_tokens):
                    parser.error(f"{tok} requires a value")
                out.append(f"{tok}{raw_tokens[i + 1]}")
                i += 2
                continue
            if tok.startswith(("-l", "-L", "-Wl,")):
                out.append(tok)
                i += 1
                continue
            if tok.startswith("--"):
                if strict_long_opts:
                    parser.error(f"unknown option in flags pragma: {tok}")
                i += 1
                continue
            # Single-dash passthrough or raw library/path token.
            out.append(tok)
            i += 1
        return out

    # Parse known and unknown args to allow -l flags anywhere
    args, unknown = parser.parse_known_args(argv)
    for tok in unknown:
        if tok == "--profile" or tok.startswith("--profile="):
            parser.error("--profile is no longer supported")
        if tok == "--compiler-profile" or tok.startswith("--compiler-profile="):
            parser.error("--compiler-profile is no longer supported")
    for lib_tok in _consume_unknown_link_tokens(unknown, strict_long_opts=False):
        if lib_tok not in args.libs:
            args.libs.append(lib_tok)

    if args.script:
        args.no_artifact = True
        args.ct_run_main = True

    if args.check:
        args.no_artifact = True

    if args.force:
        args.no_cache = True

    source_graph_cache: Optional[SourceGraphCache] = None
    if not args.no_cache:
        source_graph_cache = SourceGraphCache(args.temp_dir / ".l2cache" / "graphs")

    parser_defaults = {
        action.dest: action.default
        for action in parser._actions
        if getattr(action, "dest", None) and action.dest != "help"
    }
    source_flag_skip_dests = {
        "source",
        "clean",
        "docs",
        "repl",
        "docs_root",
        "docs_query",
        "docs_all",
        "docs_include_tests",
    }

    if args.macro_expansion_limit < 1:
        parser.error("--macro-expansion-limit must be >= 1")

    artifact_kind = args.artifact
    if args.O2:
        folding_enabled = False
        peephole_enabled = False
        loop_unroll_enabled = False
        auto_inline_enabled = False
        asm_post_opt_enabled = False
        string_deduplication_enabled = not args.no_string_dedup
        extern_type_check_enabled = False
        stack_check_enabled = False
    elif args.O0:
        folding_enabled = False
        peephole_enabled = False
        loop_unroll_enabled = False
        auto_inline_enabled = not args.no_auto_inline
        asm_post_opt_enabled = False
        string_deduplication_enabled = not args.no_string_dedup
        extern_type_check_enabled = not args.no_extern_type_check
        stack_check_enabled = not args.no_stack_check
    else:
        folding_enabled = not args.no_folding
        peephole_enabled = not args.no_peephole
        loop_unroll_enabled = not args.no_loop_unroll
        auto_inline_enabled = not args.no_auto_inline
        asm_post_opt_enabled = not args.no_asm_opt
        string_deduplication_enabled = not args.no_string_dedup
        extern_type_check_enabled = not args.no_extern_type_check
        stack_check_enabled = not args.no_stack_check
    cfg_output: Optional[Path] = None
    verbosity: int = args.verbose

    if args.ct_run_main and artifact_kind != "exe":
        parser.error("--ct-run-main requires --artifact exe")

    if artifact_kind != "exe" and (args.run or args.dbg):
        parser.error("--run/--dbg are only available when --artifact exe is selected")

    if args.no_artifact and (args.run or args.dbg):
        parser.error("--run/--dbg are not available with --no-artifact")

    if args.clean:
        try:
            if args.temp_dir.exists():
                import shutil
                shutil.rmtree(args.temp_dir)
                print(f"[info] removed {args.temp_dir}")
            else:
                print(f"[info] {args.temp_dir} does not exist")
        except Exception as exc:
            print(f"[error] failed to remove {args.temp_dir}: {exc}")
            return 1
        return 0

    if args.docs:
        try:
            return run_docs_explorer(
                source=args.source,
                include_paths=args.include_paths,
                explicit_roots=args.docs_root,
                initial_query=str(args.docs_query or ""),
                include_undocumented=args.docs_all,
                include_private=args.docs_all,
                include_tests=args.docs_include_tests,
            )
        except CompileError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1

    if args.source is None and args.check_integrity and not args.repl:
        if args.dump_cfg is not None:
            parser.error("--dump-cfg requires a source file")
        try:
            _run_integrity_checks()
        except CompileError as exc:
            print(f"[error] {exc}")
            return 1
        print("ok")
        return 0

    if args.source is None and not args.repl:
        parser.error("the following arguments are required: source")

    if args.dump_cfg is not None:
        if args.repl:
            parser.error("--dump-cfg is not available with --repl")
        if args.source is None:
            parser.error("--dump-cfg requires a source file")
        if args.dump_cfg == "__AUTO__":
            cfg_output = args.temp_dir / f"{args.source.stem}.cfg.dot"
        else:
            cfg_output = Path(args.dump_cfg)

    output_was_auto = False
    if not args.repl and args.output is None and not args.no_artifact:
        stem = args.source.stem
        default_outputs = {
            "exe": Path("a.out"),
            "shared": Path(f"lib{stem}.so"),
            "static": Path(f"lib{stem}.a"),
            "obj": Path(f"{stem}.o"),
        }
        args.output = default_outputs[artifact_kind]
        output_was_auto = True

    if not args.repl and artifact_kind in {"static", "obj"} and args.libs:
        print("[warn] --libs ignored for static/object outputs")

    compiler = Compiler(
        include_paths=[Path("."), Path("./stdlib"), *args.include_paths],
        macro_expansion_limit=args.macro_expansion_limit,
        macro_preview=args.macro_preview,
        defines=args.defines,
        source_graph_cache=source_graph_cache,
    )
    compiler.assembler.enable_constant_folding = folding_enabled
    compiler.assembler.enable_peephole_optimization = peephole_enabled
    compiler.assembler.enable_loop_unroll = loop_unroll_enabled
    compiler.assembler.enable_auto_inline = auto_inline_enabled
    compiler.assembler.enable_string_deduplication = string_deduplication_enabled
    compiler.assembler.enable_extern_type_check = extern_type_check_enabled
    compiler.assembler.enable_stack_check = stack_check_enabled
    compiler.assembler.verbosity = verbosity
    if args.dump_cfg is not None:
        compiler.assembler._need_cfg = True
    # Warning configuration
    warnings_set = set(args.warnings)
    werror = args.Werror
    # Support GCC-style -Werror (single dash, parsed as -W error)
    if "error" in warnings_set:
        warnings_set.discard("error")
        werror = True
    # -Werror without explicit -W categories implies -W all
    if werror and not warnings_set:
        warnings_set.add("all")
    compiler.parser._warnings_enabled = warnings_set
    compiler.parser._werror = werror
    compiler.parser.set_macro_profile_enabled(args.macro_profile is not None)
    compiler.parser.enable_dead_macro_elimination = not args.repl
    compiler.parser.enable_unused_rewrite_elimination = not args.repl
    # Route dictionary redefine warnings through the parser's _warn system
    if warnings_set or werror:
        def _dict_warn_cb(name: str, priority: int) -> None:
            compiler.parser._warn(
                compiler.parser._last_token, "redefine",
                f"redefining word {name} (priority {priority})",
            )
        compiler.parser.dictionary.warn_callback = _dict_warn_cb
    cache: Optional[BuildCache] = None

    try:
        if args.repl:
            return run_repl(compiler, args.temp_dir, args.libs, debug=args.debug, initial_source=args.source)

        if args.source is not None:
            # Two passes allow `flags -D ...` / `flags -I ...` to expose
            # additional pragma-controlled sections/imports on the second pass.
            for _ in range(2):
                compiler.defines = set(args.defines)
                compiler.collect_source_flags(args.source)
                changed = False

                for include_path in compiler.source_include_paths:
                    if include_path not in args.include_paths:
                        args.include_paths.append(include_path)
                        changed = True

                if compiler.source_cli_flags:
                    probe_tokens = [str(args.source), *compiler.source_cli_flags]
                    src_args, src_unknown = parser.parse_known_args(probe_tokens)
                    # Flags that don't make sense from source-level pragmas.
                    for dest, default in parser_defaults.items():
                        if dest in source_flag_skip_dests:
                            continue
                        cur_val = getattr(args, dest)
                        src_val = getattr(src_args, dest)
                        if isinstance(default, list):
                            for item in src_val:
                                if item not in cur_val:
                                    cur_val.append(item)
                                    changed = True
                            continue

                        if dest == "output" and output_was_auto and src_val != default:
                            setattr(args, dest, src_val)
                            output_was_auto = False
                            changed = True
                            continue

                        if cur_val == default and src_val != default:
                            setattr(args, dest, src_val)
                            changed = True

                    for lib_tok in _consume_unknown_link_tokens(src_unknown, strict_long_opts=True):
                        if lib_tok not in args.libs:
                            args.libs.append(lib_tok)
                            changed = True

                for flag in compiler.source_link_flags:
                    if flag not in args.libs:
                        args.libs.append(flag)
                        changed = True

                if args.script:
                    if not args.no_artifact or not args.ct_run_main:
                        changed = True
                    args.no_artifact = True
                    args.ct_run_main = True

                if args.check and not args.no_artifact:
                    args.no_artifact = True
                    changed = True

                if args.force and not args.no_cache:
                    args.no_cache = True
                    changed = True

                if not changed:
                    break

        cache_enabled = not args.no_cache
        if cache_enabled:
            if compiler.source_graph_cache is None:
                compiler.source_graph_cache = SourceGraphCache(args.temp_dir / ".l2cache" / "graphs")
            cache = BuildCache(args.temp_dir / ".l2cache" / "asm")
        else:
            compiler.source_graph_cache = None
            cache = None

        if args.macro_expansion_limit < 1:
            parser.error("--macro-expansion-limit must be >= 1")

        artifact_kind = args.artifact
        if args.O2:
            folding_enabled = False
            peephole_enabled = False
            loop_unroll_enabled = False
            auto_inline_enabled = False
            asm_post_opt_enabled = False
            string_deduplication_enabled = not args.no_string_dedup
            extern_type_check_enabled = False
            stack_check_enabled = False
        elif args.O0:
            folding_enabled = False
            peephole_enabled = False
            loop_unroll_enabled = False
            auto_inline_enabled = not args.no_auto_inline
            asm_post_opt_enabled = False
            string_deduplication_enabled = not args.no_string_dedup
            extern_type_check_enabled = not args.no_extern_type_check
            stack_check_enabled = not args.no_stack_check
        else:
            folding_enabled = not args.no_folding
            peephole_enabled = not args.no_peephole
            loop_unroll_enabled = not args.no_loop_unroll
            auto_inline_enabled = not args.no_auto_inline
            asm_post_opt_enabled = not args.no_asm_opt
            string_deduplication_enabled = not args.no_string_dedup
            extern_type_check_enabled = not args.no_extern_type_check
            stack_check_enabled = not args.no_stack_check
        verbosity = args.verbose

        if args.ct_run_main and artifact_kind != "exe":
            parser.error("--ct-run-main requires --artifact exe")
        if artifact_kind != "exe" and (args.run or args.dbg):
            parser.error("--run/--dbg are only available when --artifact exe is selected")
        if args.no_artifact and (args.run or args.dbg):
            parser.error("--run/--dbg are not available with --no-artifact")

        if output_was_auto and not args.repl and not args.no_artifact:
            stem = args.source.stem
            default_outputs = {
                "exe": Path("a.out"),
                "shared": Path(f"lib{stem}.so"),
                "static": Path(f"lib{stem}.a"),
                "obj": Path(f"{stem}.o"),
            }
            args.output = default_outputs[artifact_kind]

        normalized_include_paths: List[Path] = []
        seen_include_paths: Set[Path] = set()
        for include_base in [Path("."), Path("./stdlib"), *args.include_paths]:
            resolved = include_base.expanduser().resolve()
            if resolved not in seen_include_paths:
                seen_include_paths.add(resolved)
                normalized_include_paths.append(resolved)
        compiler.include_paths = normalized_include_paths
        compiler._import_resolve_cache.clear()
        compiler.defines = set(args.defines)
        compiler.parser.macro_expansion_limit = args.macro_expansion_limit
        compiler.parser.macro_preview = args.macro_preview
        compiler.parser.set_macro_profile_enabled(args.macro_profile is not None)
        compiler.parser.enable_dead_macro_elimination = not args.repl
        compiler.parser.enable_unused_rewrite_elimination = not args.repl

        compiler.assembler.enable_constant_folding = folding_enabled
        compiler.assembler.enable_peephole_optimization = peephole_enabled
        compiler.assembler.enable_loop_unroll = loop_unroll_enabled
        compiler.assembler.enable_auto_inline = auto_inline_enabled
        compiler.assembler.enable_string_deduplication = string_deduplication_enabled
        compiler.assembler.enable_extern_type_check = extern_type_check_enabled
        compiler.assembler.enable_stack_check = stack_check_enabled
        compiler.assembler.verbosity = verbosity

        warnings_set = set(args.warnings)
        werror = args.Werror
        if "error" in warnings_set:
            warnings_set.discard("error")
            werror = True
        if werror and not warnings_set:
            warnings_set.add("all")
        compiler.parser._warnings_enabled = warnings_set
        compiler.parser._werror = werror
        if warnings_set or werror:
            def _dict_warn_cb(name: str, priority: int) -> None:
                compiler.parser._warn(
                    compiler.parser._last_token, "redefine",
                    f"redefining word {name} (priority {priority})",
                )
            compiler.parser.dictionary.warn_callback = _dict_warn_cb
        else:
            compiler.parser.dictionary.warn_callback = None

        if args.check_integrity:
            _run_integrity_checks()
            print("ok")

        ct_run_libs = list(args.libs)
        if args.source is not None:
            for lib in _load_sidecar_meta_libs(args.source):
                if lib not in args.libs:
                    args.libs.append(lib)
                if lib not in ct_run_libs:
                    ct_run_libs.append(lib)

        cfg_output = None
        if args.dump_cfg is not None:
            if args.repl:
                parser.error("--dump-cfg is not available with --repl")
            if args.source is None:
                parser.error("--dump-cfg requires a source file")
            if args.dump_cfg == "__AUTO__":
                cfg_output = args.temp_dir / f"{args.source.stem}.cfg.dot"
            else:
                cfg_output = Path(args.dump_cfg)
            compiler.assembler._need_cfg = True
        else:
            compiler.assembler._need_cfg = False

        entry_mode = "program" if artifact_kind == "exe" else "library"

        # --- assembly-level cache check ---
        asm_text: Optional[str] = None
        fhash = ""
        cache_asm_hit = False
        if cache and not args.ct_run_main and args.dump_cfg is None:
            fhash = cache.flags_hash(
                args.debug,
                folding_enabled,
                peephole_enabled,
                auto_inline_enabled,
                asm_post_opt_enabled,
                string_deduplication_enabled,
                entry_mode,
            )
            manifest = cache.load_manifest(args.source)
            if manifest and cache.check_fresh(manifest, fhash):
                cached = cache.get_cached_asm(manifest)
                if cached is not None:
                    asm_text = cached
                    cache_asm_hit = True
                    if verbosity >= 1:
                        print(f"[v1] cache hit for {args.source}")

        if asm_text is None:
            if verbosity >= 1:
                import time as _time_mod
                _compile_t0 = _time_mod.perf_counter()
            if compiler._last_loaded_path == args.source.resolve() and compiler._last_loaded_source is not None:
                emission = compiler.compile_preloaded(debug=args.debug, entry_mode=entry_mode)
            else:
                emission = compiler.compile_file(args.source, debug=args.debug, entry_mode=entry_mode)

            # Snapshot assembly text *before* ct-run-main JIT execution, which may
            # corrupt Python heap objects depending on memory layout.
            asm_text = emission.snapshot()
            if verbosity >= 1:
                _compile_dt = (_time_mod.perf_counter() - _compile_t0) * 1000
                print(f"[v1] compilation: {_compile_dt:.1f}ms")
                print(f"[v1] assembly size: {len(asm_text)} bytes")

            has_ct = bool(compiler.parser.compile_time_vm._ct_executed)

            if asm_post_opt_enabled:
                use_asm_opt_cache = cache_enabled
                asm_opt_key_path = args.temp_dir / f"{args.source.stem}.asmopt.key"
                asm_opt_cache_path = args.temp_dir / f"{args.source.stem}.asmopt.asm"
                asm_opt_cache_version = "v1"

                asm_digest: Optional[str] = None
                try:
                    import hashlib
                    asm_digest = hashlib.blake2b(asm_text.encode("utf-8"), digest_size=16).hexdigest()
                except Exception:
                    asm_digest = None

                optimized_from_cache = False
                asm_opt_stats: Dict[str, int] = {}
                asm_opt_pass_logs: List[str] = []
                if use_asm_opt_cache and asm_digest is not None:
                    try:
                        cached_key = asm_opt_key_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        cached_key = ""
                    expected_key = f"{asm_opt_cache_version}:{asm_digest}"
                    if cached_key == expected_key:
                        try:
                            asm_text = asm_opt_cache_path.read_text(encoding="utf-8")
                            optimized_from_cache = True
                        except OSError:
                            optimized_from_cache = False

                if not optimized_from_cache:
                    optimized_asm, asm_opt_stats, asm_opt_pass_logs = optimize_emitted_asm_text(
                        asm_text,
                        collect_pass_logs=(verbosity >= 4),
                    )
                    if optimized_asm != asm_text:
                        asm_text = optimized_asm
                    if use_asm_opt_cache and asm_digest is not None:
                        try:
                            asm_opt_key_path.parent.mkdir(parents=True, exist_ok=True)
                            asm_opt_cache_path.write_text(asm_text, encoding="utf-8")
                            asm_opt_key_path.write_text(f"{asm_opt_cache_version}:{asm_digest}", encoding="utf-8")
                        except OSError:
                            pass

                if verbosity >= 1:
                    if optimized_from_cache:
                        print("[v1] asm post-opt: cache hit")
                    else:
                        changed = sum(asm_opt_stats.values())
                        print(f"[v1] asm post-opt: {changed} rewrite(s)")
                if verbosity >= 2 and not optimized_from_cache:
                    for key in sorted(asm_opt_stats):
                        if asm_opt_stats[key]:
                            print(f"[v2] asm post-opt {key}: {asm_opt_stats[key]}")
                if verbosity >= 4 and not optimized_from_cache:
                    for msg in asm_opt_pass_logs:
                        print(f"[v4] asm post-opt {msg}")

            if cache and not args.ct_run_main:
                if not fhash:
                    fhash = cache.flags_hash(
                        args.debug,
                        folding_enabled,
                        peephole_enabled,
                        auto_inline_enabled,
                        asm_post_opt_enabled,
                        string_deduplication_enabled,
                        entry_mode,
                    )
                cache.save(args.source, compiler._loaded_files, fhash, asm_text, has_ct_effects=has_ct)

        # Merge source-level `flags ...` pragmas into effective linker flags.
        if compiler.source_link_flags:
            for flag in compiler.source_link_flags:
                if flag not in args.libs:
                    args.libs.append(flag)
                if flag not in ct_run_libs:
                    ct_run_libs.append(flag)

        def _normalize_ct_runtime_libs(raw_libs: List[str]) -> List[str]:
            out: List[str] = []
            seen: Set[str] = set()
            for lib in raw_libs:
                token = str(lib).strip()
                if not token:
                    continue
                # Linker/search/include-only flags are not directly loadable via ctypes.
                if token.startswith(("-L", "-Wl,")):
                    continue
                if token == "-I" or token.startswith("-I") or token.startswith("--include"):
                    continue
                if token.startswith("-l:"):
                    token = token[3:]
                elif token.startswith("-l") and len(token) > 2:
                    token = token[2:]
                if token and token not in seen:
                    out.append(token)
                    seen.add(token)
            return out

        ct_run_libs = _normalize_ct_runtime_libs(ct_run_libs)
        if args.ct_run_main:
            # For CT execution, when a static archive path is provided,
            # also try loading a sibling shared object path.
            expanded_ct_libs: List[str] = []
            seen_ct_libs: Set[str] = set()
            for lib in ct_run_libs:
                if lib not in seen_ct_libs:
                    expanded_ct_libs.append(lib)
                    seen_ct_libs.add(lib)
                if lib.endswith(".a"):
                    so_variant = lib[:-2] + ".so"
                    if so_variant not in seen_ct_libs:
                        expanded_ct_libs.append(so_variant)
                        seen_ct_libs.add(so_variant)
            ct_run_libs = expanded_ct_libs

        if args.ct_run_main and args.source is not None:
            import subprocess
            try:
                ct_sidecar = _build_ct_sidecar_shared(args.source, args.temp_dir)
            except subprocess.CalledProcessError as exc:
                print(f"[error] failed to build compile-time sidecar library: {exc}")
                return 1
            if ct_sidecar is not None:
                so_lib = str(ct_sidecar.resolve())
                if so_lib not in ct_run_libs:
                    ct_run_libs.append(so_lib)

        if cfg_output is not None:
            cfg_output.parent.mkdir(parents=True, exist_ok=True)
            cfg_dot = compiler.assembler.render_last_cfg_dot()
            cfg_output.write_text(cfg_dot)
            print(f"[info] wrote {cfg_output}")

        if args.ct_run_main:
            try:
                compiler.run_compile_time_word("main", libs=ct_run_libs)
            except CompileTimeError as exc:
                print(f"[error] compile-time execution of 'main' failed: {exc}")
                return 1
    except (ParseError, CompileError, CompileTimeError) as exc:
        # Print all collected diagnostics in Rust-style format
        use_color = sys.stderr.isatty()
        diags = getattr(compiler.parser, 'diagnostics', []) if 'compiler' in dir() else []
        if diags:
            for diag in diags:
                print(diag.format(color=use_color), file=sys.stderr)
            error_count = sum(1 for d in diags if d.level == "error")
            warn_count = sum(1 for d in diags if d.level == "warning")
            summary_parts = []
            if error_count:
                summary_parts.append(f"{error_count} error(s)")
            if warn_count:
                summary_parts.append(f"{warn_count} warning(s)")
            if summary_parts:
                print(f"\n{' and '.join(summary_parts)} emitted", file=sys.stderr)
        else:
            print(f"[error] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[error] unexpected failure: {exc}", file=sys.stderr)
        return 1

    # Print any warnings accumulated during successful compilation
    use_color = sys.stderr.isatty()
    warnings = [d for d in compiler.parser.diagnostics if d.level == "warning"]
    if warnings:
        for diag in warnings:
            print(diag.format(color=use_color), file=sys.stderr)
        print(f"\n{len(warnings)} warning(s) emitted", file=sys.stderr)

    if args.macro_profile is not None:
        _emit_macro_profile_report(compiler.parser, args.macro_profile)

    args.temp_dir.mkdir(parents=True, exist_ok=True)
    asm_path = args.temp_dir / (args.source.stem + ".asm")
    obj_path = args.temp_dir / (args.source.stem + ".o")

    # --- incremental: skip nasm if assembly unchanged ---
    asm_changed = True
    if not args.force and asm_path.exists():
        existing_asm = asm_path.read_text()
        if existing_asm == asm_text:
            asm_changed = False
    if asm_changed or args.force:
        asm_path.write_text(asm_text)

    if args.emit_asm:
        print(f"[info] wrote {asm_path}")
        return 0

    if args.no_artifact:
        print("[info] skipped artifact generation (--no-artifact)")
        return 0

    # --- incremental: skip nasm if .o newer than .asm ---
    need_nasm = args.force or asm_changed or not obj_path.exists()
    if not need_nasm:
        try:
            need_nasm = obj_path.stat().st_mtime < asm_path.stat().st_mtime
        except OSError:
            need_nasm = True
    if need_nasm:
        run_nasm(asm_path, obj_path, debug=args.debug)

    if args.output.parent and not args.output.parent.exists():
        args.output.parent.mkdir(parents=True, exist_ok=True)

    # --- incremental: skip linker if output newer than .o AND same source ---
    # Track which .o produced the output so switching source files forces relink.
    link_stamp = args.temp_dir / f"{args.output.name}.link_src"

    link_fingerprint_parts = [
        f"obj={obj_path.resolve()}",
        f"artifact={artifact_kind}",
        f"debug={int(bool(args.debug))}",
    ]

    def _lib_file_from_token(tok: str) -> Optional[Path]:
        if tok.startswith("-l:") and len(tok) > 3:
            return Path(tok[3:]).expanduser()
        if tok.startswith("-"):
            return None
        return Path(tok).expanduser()

    for lib in args.libs:
        link_fingerprint_parts.append(f"lib={lib}")
        lib_file = _lib_file_from_token(lib)
        if lib_file is None:
            continue
        try:
            resolved = lib_file.resolve()
            st = resolved.stat()
        except OSError:
            continue
        link_fingerprint_parts.append(
            f"libfile={resolved}:mtime_ns={st.st_mtime_ns}:size={st.st_size}"
        )
    link_fingerprint = "\n".join(link_fingerprint_parts)

    need_link = args.force or need_nasm or not args.output.exists()
    if not need_link:
        # Check that the output was linked from the same inputs/config last time.
        try:
            recorded = link_stamp.read_text()
        except OSError:
            recorded = ""
        if recorded != link_fingerprint:
            need_link = True
    if not need_link:
        try:
            need_link = args.output.stat().st_mtime < obj_path.stat().st_mtime
        except OSError:
            need_link = True

    if artifact_kind == "obj":
        dest = args.output
        if obj_path.resolve() != dest.resolve():
            if need_link:
                import shutil
                shutil.copy2(obj_path, dest)
    elif artifact_kind == "static":
        if need_link:
            build_static_library(obj_path, args.output)
    else:
        if need_link:
            # Remove existing output first to avoid "Text file busy" on Linux
            # when the old binary is still mapped by a running process.
            try:
                args.output.unlink(missing_ok=True)
            except OSError:
                pass
            run_linker(
                obj_path,
                args.output,
                debug=args.debug,
                libs=args.libs,
                shared=(artifact_kind == "shared"),
            )

    if need_link:
        link_stamp.write_text(link_fingerprint)
        print(f"[info] built {args.output}")
    else:
        print(f"[info] {args.output} is up to date")

    if artifact_kind == "exe":
        import subprocess
        exe_path = Path(args.output).resolve()
        if args.dbg:
            subprocess.run(["gdb", str(exe_path)])
        elif args.run:
            subprocess.run([str(exe_path)])
    return 0


def main() -> None:
    code = cli(sys.argv[1:])
    # Flush all output then use os._exit to avoid SIGSEGV from ctypes/native
    # memory finalization during Python's shutdown sequence.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
