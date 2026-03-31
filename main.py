"""Bootstrap compiler for the L2 language.

This file now contains working scaffolding for:

* Parsing definitions, literals, and ordinary word references.
* Respecting immediate/macro words so syntax can be rewritten on the fly.
* Emitting NASM-compatible x86-64 assembly with explicit data and return stacks.
* Driving the toolchain via ``nasm`` + ``ld``.
"""

from __future__ import annotations

import bisect
import os
import re
import sys
from pathlib import Path
TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Union, Tuple

try:  # lazy optional import; required for compile-time :asm execution
    from keystone import Ks, KsError, KS_ARCH_X86, KS_MODE_64
except Exception:  # pragma: no cover - optional dependency
    Ks = None
    KsError = Exception
    KS_ARCH_X86 = KS_MODE_64 = None

# Pre-compiled regex patterns used by JIT and BSS code
_RE_REL_PAT = re.compile(r'\[rel\s+(\w+)\]')
_RE_LABEL_PAT = re.compile(r'^(\.\w+|\w+):')
_RE_BSS_PERSISTENT = re.compile(r'persistent:\s*resb\s+(\d+)')
_RE_NEWLINE = re.compile('\n')
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
        _line_starts = [0] + [m.end() for m in _RE_NEWLINE.finditer(source)]
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


def _is_scalar_literal(node: Op) -> bool:
    return node._opcode == OP_LITERAL and not isinstance(node.data, str)


_RE_MACRO_ARG_TOKEN = re.compile(r"^\$(\d+)$")
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

_PEEPHOLE_CANCEL_PAIRS = frozenset({
    ("not", "not"), ("neg", "neg"),
    ("bitnot", "bitnot"), ("bnot", "bnot"),
    ("inc", "dec"), ("dec", "inc"),
})
_PEEPHOLE_SHIFT_OPS = frozenset({"shl", "shr", "sar"})
_DEFAULT_CONTROL_WORDS = frozenset({"if", "else", "for", "while", "do"})


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


class Definition:
    __slots__ = ('name', 'body', 'immediate', 'compile_only', 'terminator', 'inline',
                 'stack_inputs', '_label_positions', '_for_pairs', '_begin_pairs',
                 '_words_resolved', '_merged_runs')

    def __init__(self, name: str, body: List[Op], immediate: bool = False,
                 compile_only: bool = False, terminator: str = "end", inline: bool = False,
                 stack_inputs: Optional[int] = None) -> None:
        self.name = name
        self.body = body
        self.immediate = immediate
        self.compile_only = compile_only
        self.terminator = terminator
        self.inline = inline
        self.stack_inputs = stack_inputs
        self._label_positions = None
        self._for_pairs = None
        self._begin_pairs = None
        self._words_resolved = False
        self._merged_runs = None


class AsmDefinition:
    __slots__ = ('name', 'body', 'immediate', 'compile_only', 'inline', 'effects', '_inline_lines')

    def __init__(self, name: str, body: str, immediate: bool = False,
                 compile_only: bool = False, inline: bool = False,
                 effects: Set[str] = None, _inline_lines: Optional[List[str]] = None) -> None:
        self.name = name
        self.body = body
        self.immediate = immediate
        self.compile_only = compile_only
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
    __slots__ = ('name', 'tokens', 'param_count', 'asm_brace_depth', 'awaiting_asm_body', 'awaiting_asm_terminator')

    def __init__(self, name: str, tokens: List[str], param_count: int = 0) -> None:
        self.name = name
        self.tokens = tokens
        self.param_count = param_count
        self.asm_brace_depth = 0
        self.awaiting_asm_body = False
        self.awaiting_asm_terminator = False


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
                 'runtime_intrinsic', 'compile_only', 'compile_time_override',
                 'is_extern', 'extern_inputs', 'extern_outputs', 'extern_signature',
                 'extern_variadic', 'inline')

    def __init__(self, name: str, priority: int = 0, immediate: bool = False,
                 definition=None, macro=None, intrinsic=None,
                 macro_expansion=None, macro_params: int = 0,
                 compile_time_intrinsic=None, runtime_intrinsic=None,
                 compile_only: bool = False, compile_time_override: bool = False,
                 is_extern: bool = False, extern_inputs: int = 0, extern_outputs: int = 0,
                 extern_signature=None, extern_variadic: bool = False,
                 inline: bool = False) -> None:
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
        self.compile_time_override = compile_time_override
        self.is_extern = is_extern
        self.extern_inputs = extern_inputs
        self.extern_outputs = extern_outputs
        self.extern_signature = extern_signature
        self.extern_variadic = extern_variadic
        self.inline = inline


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
        self.diagnostics: List[Diagnostic] = []
        self._max_errors: int = 20
        self._warnings_enabled: Set[str] = set()
        self._werror: bool = False

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

        _priority_keywords = {
            "word", ":asm", ":py", "extern", "inline", "priority",
        }

        # Sentinel values for dispatch actions
        _KW_LIST_BEGIN = 1
        _KW_LIST_END = 2
        _KW_WORD = 3
        _KW_END = 4
        _KW_ASM = 5
        _KW_PY = 6
        _KW_EXTERN = 7
        _KW_PRIORITY = 8
        _KW_RET = 9
        _KW_BSS_LIST_BEGIN = 10
        _keyword_dispatch = {
            "[": _KW_LIST_BEGIN, "]": _KW_LIST_END, "word": _KW_WORD,
            "end": _KW_END, ":asm": _KW_ASM, ":py": _KW_PY,
            "extern": _KW_EXTERN, "priority": _KW_PRIORITY, "ret": _KW_RET,
            "{": _KW_BSS_LIST_BEGIN,
        }
        _kw_get = _keyword_dispatch.get
        _tokens = self.tokens
        try:
            while self.pos < len(_tokens):
              try:
                token = _tokens[self.pos]
                self.pos += 1
                self._last_token = token
                if self.token_hook and self._run_token_hook(token):
                    continue
                if self._handle_macro_recording(token):
                    continue
                lexeme = token.lexeme
                if self._pending_priority is not None and lexeme not in _priority_keywords:
                    raise ParseError(
                        f"priority {self._pending_priority} must be followed by definition/extern"
                    )
                kw = _kw_get(lexeme)
                if kw is not None:
                    if kw == _KW_LIST_BEGIN:
                        self._handle_list_begin()
                    elif kw == _KW_LIST_END:
                        self._handle_list_end(token)
                    elif kw == _KW_WORD:
                        inline_def = self._consume_pending_inline()
                        self._begin_definition(token, terminator="end", inline=inline_def)
                    elif kw == _KW_END:
                        if self.control_stack:
                            self._handle_end_control()
                        elif self._try_end_definition(token):
                            pass
                        else:
                            raise ParseError(f"unexpected 'end' at {token.line}:{token.column}")
                    elif kw == _KW_ASM:
                        self._parse_asm_definition(token)
                        _tokens = self.tokens
                    elif kw == _KW_PY:
                        self._parse_py_definition(token)
                        _tokens = self.tokens
                    elif kw == _KW_EXTERN:
                        self._parse_extern(token)
                    elif kw == _KW_PRIORITY:
                        self._parse_priority_directive(token)
                    elif kw == _KW_RET:
                        self._handle_ret(token)
                    elif kw == _KW_BSS_LIST_BEGIN:
                        self._parse_bss_list_literal(token)
                    continue
                if self._try_handle_builtin_control(token):
                    continue
                if self._handle_token(token):
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
        # Support shorthand `else <cond> if` by sharing the previous else-end label.
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
        first = lexeme[0]
        # Fast-path: inline integer literal parse (most common literal type)
        if first.isdigit() or first == '-' or first == '+':
            try:
                value = int(lexeme, 0)
                self._append_op(_make_literal_op(value))
                return False
            except ValueError:
                pass
            # Fall through to float/string check
            if self._try_literal(token):
                return False
        elif first == '"' or first == '.' or first == "'":
            if self._try_literal(token):
                return False

        if first == '&':
            target_name = lexeme[1:]
            if not target_name:
                raise ParseError(f"missing word name after '&' at {token.line}:{token.column}")
            self._append_op(_make_op("word_ptr", target_name))
            return False

        word = self.dictionary.words.get(lexeme)
        if word is not None:
            if word.macro_expansion is not None:
                args = self._collect_macro_args(word.macro_params, word_name=word.name, call_token=token)
                self._inject_macro_tokens(word, token, args)
                return True
            if word.immediate:
                if word.macro:
                    produced = word.macro(MacroContext(self))
                    if produced:
                        for node in produced:
                            self._append_op(node)
                else:
                    self._execute_immediate_word(word)
                return False

        self._append_op(_make_word_op(lexeme))
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
            self._finish_macro_recording(token)
        else:
            rec.tokens.append(lex)
        return True

    def _maybe_expand_macro(self, token: Token) -> bool:
        word = self.dictionary.lookup(token.lexeme)
        if word and word.macro_expansion is not None:
            args = self._collect_macro_args(word.macro_params, word_name=word.name, call_token=token)
            self._inject_macro_tokens(word, token, args)
            return True
        return False

    def _inject_macro_tokens(self, word: Word, token: Token, args: List[str]) -> None:
        next_depth = token.expansion_depth + 1
        if next_depth > self.macro_expansion_limit:
            raise ParseError(
                f"macro expansion depth limit ({self.macro_expansion_limit}) exceeded while expanding '{word.name}'"
            )
        replaced: List[str] = []
        for lex in word.macro_expansion or []:
            arg_match = _RE_MACRO_ARG_TOKEN.fullmatch(lex)
            if arg_match is not None:
                idx = int(arg_match.group(1))
                if idx < 0 or idx >= len(args):
                    raise ParseError(
                        f"macro '{word.name}' references argument {lex}, "
                        f"but call provided only {len(args)} argument(s)"
                    )
                replaced.append(args[idx])
            else:
                replaced.append(lex)
        if self.macro_preview:
            preview = " ".join(replaced)
            if len(preview) > 240:
                preview = preview[:237] + "..."
            sys.stderr.write(
                f"[macro-preview] {word.name} at {token.line}:{token.column} -> {preview}\n"
            )
        insertion = [
            Token(
                lexeme=lex,
                line=token.line,
                column=token.column,
                start=token.start,
                end=token.end,
                expansion_depth=next_depth,
            )
            for lex in replaced
        ]
        self.tokens[self.pos:self.pos] = insertion

    def _collect_macro_args(
        self,
        count: int,
        *,
        word_name: Optional[str] = None,
        call_token: Optional[Token] = None,
    ) -> List[str]:
        args: List[str] = []
        for _ in range(count):
            if self._eof():
                if word_name is not None and call_token is not None:
                    raise ParseError(
                        f"macro '{word_name}' at {call_token.line}:{call_token.column} "
                        f"expects {count} argument(s), got {len(args)}"
                    )
                raise ParseError("macro invocation missing arguments")
            args.append(self._consume().lexeme)
        return args

    def _start_macro_recording(self, name: str, param_count: int) -> None:
        if self.macro_recording is not None:
            raise ParseError("nested macro definitions are not supported")
        self.macro_recording = MacroDefinition(name=name, tokens=[], param_count=param_count)

    def _finish_macro_recording(self, token: Token) -> None:
        if self.macro_recording is None:
            raise ParseError(f"unexpected ';' closing a macro at {token.line}:{token.column}")
        macro_def = self.macro_recording
        self.macro_recording = None
        word = Word(name=macro_def.name)
        word.macro_expansion = list(macro_def.tokens)
        word.macro_params = macro_def.param_count
        self.dictionary.register(word)

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
        ctx.inline = word.inline
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
        if node.loc is None:
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
        first = lexeme[0] if lexeme else '\0'
        if first.isdigit() or first == '-' or first == '+':
            try:
                value = int(lexeme, 0)
                self._append_op(_make_literal_op(value))
                return True
            except ValueError:
                pass

        # Try float
        if first.isdigit() or first == '-' or first == '+' or first == '.':
            try:
                if "." in lexeme or "e" in lexeme.lower():
                    value = float(lexeme)
                    self._append_op(_make_literal_op(value))
                    return True
            except ValueError:
                pass

        if first == '"':
            string_value = _parse_string_literal(token)
            if string_value is not None:
                self._append_op(_make_literal_op(string_value))
                return True

        if first == "'":
            char_value = _parse_char_literal(token)
            if char_value is not None:
                self._append_op(_make_literal_op(char_value))
                return True

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
            definition = word.definition
            # In runtime_mode, prefer runtime_intrinsic (for exit/jmp/syscall
            # and __with_* variables).  All other :asm words run as native JIT.
            if self.runtime_mode and word.runtime_intrinsic is not None:
                word.runtime_intrinsic(self)
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
        if Ks is None:
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
        if Ks is None:
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
        if Ks is None:
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
        if Ks is None:
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

                    # Try longest match first (max pattern len = 3)
                    matched = False
                    # Try window=3
                    if idx + 2 < nlen:
                        b = nodes[idx + 1]
                        c = nodes[idx + 2]
                        if b._opcode == _OP_W and c._opcode == _OP_W:
                            repl = all_rules.get((word_name, b.data, c.data))
                            if repl is not None:
                                base_loc = node.loc
                                for r in repl:
                                    if r[0] == 'l' and r[:8] == "literal_":
                                        _opt_append(_make_literal_op(int(r[8:]), loc=base_loc))
                                    else:
                                        _opt_append(_make_word_op(r, base_loc))
                                idx += 3
                                changed = True
                                matched = True
                    # Try window=2
                    if not matched and idx + 1 < nlen:
                        b = nodes[idx + 1]
                        if b._opcode == _OP_W:
                            repl = all_rules.get((word_name, b.data))
                            if repl is not None:
                                base_loc = node.loc
                                for r in repl:
                                    if r[0] == 'l' and r[:8] == "literal_":
                                        _opt_append(_make_literal_op(int(r[8:]), loc=base_loc))
                                    else:
                                        _opt_append(_make_word_op(r, base_loc))
                                idx += 2
                                changed = True
                                matched = True
                    if not matched:
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
                _start_lines = [
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
                    "    mov r15, r12",
                    "    lea r13, [rel rstack_top]",
                    f"    call {sanitize_label('main')}",
                    "    mov rax, 0",
                    "    cmp r12, r15",
                    "    je .no_exit_value",
                    "    mov rax, [r12]",
                    "    add r12, 8",
                    ".no_exit_value:",
                ]
                _start_lines.extend([
                    "    mov rdi, rax",
                    "    mov rax, 60",
                    "    syscall",
                ])
                emission.text.extend(_start_lines)

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
    word.immediate = True
    if word.definition is not None:
        word.definition.immediate = True
    return None


def macro_compile_only(ctx: MacroContext) -> Optional[List[Op]]:
    parser = ctx.parser
    word = parser.most_recent_definition()
    if word is None:
        raise ParseError("'compile-only' must follow a definition")
    word.compile_only = True
    if word.definition is not None:
        word.definition.compile_only = True
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
    peek = parser.peek_token()
    if peek is not None:
        try:
            param_count = int(peek.lexeme, 0)
            parser.next_token()
        except ValueError:
            param_count = 0
    parser._start_macro_recording(name_token.lexeme, param_count)
    return None


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
    word.compile_time_intrinsic = None
    word.compile_time_override = True


def _ct_add_token(vm: CompileTimeVM) -> None:
    tok = vm.pop_str()
    vm.parser.reader.add_tokens([tok])


def _ct_add_token_chars(vm: CompileTimeVM) -> None:
    chars = vm.pop_str()
    vm.parser.reader.add_token_chars(chars)


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
    if Ks is None:
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
        "    push r15",
        "    sub rsp, 16",
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
        # Save syscall num in r15
        "    mov r15, rax",
        # Check for exit (60) / exit_group (231)
        "    cmp r15, 60",
        "    je _do_exit",
        "    cmp r15, 231",
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
        "    mov rax, r15",          # syscall number
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
        "    add rsp, 16",
        "    pop r15",
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

    register("string=", _ct_string_eq, compile_only=True)
    register("string-length", _ct_string_length, compile_only=True)
    register("string-append", _ct_string_append, compile_only=True)
    register("string>number", _ct_string_to_number, compile_only=True)
    register("int>string", _ct_int_to_string, compile_only=True)
    register("identifier?", _ct_identifier_p, compile_only=True)
    register("shunt", _ct_shunt, compile_only=True)

    register("token-lexeme", _ct_token_lexeme, compile_only=True)
    register("token-from-lexeme", _ct_token_from_lexeme, compile_only=True)
    register("next-token", _ct_next_token, compile_only=True)
    register("peek-token", _ct_peek_token, compile_only=True)
    register("inject-tokens", _ct_inject_tokens, compile_only=True)
    register("add-token", _ct_add_token, compile_only=True)
    register("add-token-chars", _ct_add_token_chars, compile_only=True)
    register("set-token-hook", _ct_set_token_hook, compile_only=True)
    register("clear-token-hook", _ct_clear_token_hook, compile_only=True)
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


def bootstrap_dictionary() -> Dictionary:
    dictionary = Dictionary()
    dictionary.register(Word(name="immediate", immediate=True, macro=macro_immediate))
    dictionary.register(Word(name="compile-only", immediate=True, macro=macro_compile_only))
    dictionary.register(Word(name="inline", immediate=True, macro=macro_inline))
    dictionary.register(Word(name="label", immediate=True, macro=macro_label))
    dictionary.register(Word(name="goto", immediate=True, macro=macro_goto))
    dictionary.register(Word(name="compile-time", immediate=True, macro=macro_compile_time))
    dictionary.register(Word(name="here", immediate=True, macro=macro_here))
    dictionary.register(Word(name="with", immediate=True, macro=macro_with))
    dictionary.register(Word(name="macro", immediate=True, macro=macro_begin_text_macro))
    dictionary.register(Word(name="struct", immediate=True, macro=macro_struct_begin))
    dictionary.register(Word(name="cstruct", immediate=True, macro=macro_cstruct_begin))
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
        is_ptr = "*" in decl
        if is_ptr:
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
        source, spans = self._load_with_imports(path.resolve())
        self.parser.file_spans = spans or []
        tokens = self.reader.tokenize(source)
        self.parser.parse(tokens, source)

    def compile_file(self, path: Path, *, debug: bool = False, entry_mode: str = "program") -> Emission:
        source, spans = self._load_with_imports(path.resolve())
        return self.compile_source(source, spans=spans, debug=debug, entry_mode=entry_mode)

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
            candidate = raw
            tried.append(candidate)
            if candidate.exists():
                result = candidate.resolve()
                self._import_resolve_cache[cache_key] = result
                return result

        candidate = (importing_file.parent / raw).resolve()
        tried.append(candidate)
        if candidate.exists():
            self._import_resolve_cache[cache_key] = candidate
            return candidate

        for base in self.include_paths:
            candidate = (base / raw).resolve()
            tried.append(candidate)
            if candidate.exists():
                self._import_resolve_cache[cache_key] = candidate
                return candidate

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

        def _ifdef_active() -> bool:
            return all(_ifdef_stack)

        for line in contents.splitlines():
            stripped = line.strip()

            # --- Conditional compilation directives ---
            if stripped[:6] == "ifdef " or stripped == "ifdef":
                name = stripped[6:].strip() if len(stripped) > 6 else ""
                if not name:
                    raise ParseError(f"ifdef missing symbol name at {path}:{file_line_no}")
                _ifdef_stack.append(name in self.defines if _ifdef_active() else False)
                _out_append("")  # placeholder to keep line numbers aligned
                file_line_no += 1
                continue
            if stripped[:7] == "ifndef " or stripped == "ifndef":
                name = stripped[7:].strip() if len(stripped) > 7 else ""
                if not name:
                    raise ParseError(f"ifndef missing symbol name at {path}:{file_line_no}")
                _ifdef_stack.append(name not in self.defines if _ifdef_active() else False)
                _out_append("")
                file_line_no += 1
                continue
            if stripped == "elsedef":
                if not _ifdef_stack:
                    raise ParseError(f"elsedef without matching ifdef/ifndef at {path}:{file_line_no}")
                _ifdef_stack[-1] = not _ifdef_stack[-1]
                _out_append("")
                file_line_no += 1
                continue
            if stripped == "endif":
                if not _ifdef_stack:
                    raise ParseError(f"endif without matching ifdef/ifndef at {path}:{file_line_no}")
                _ifdef_stack.pop()
                _out_append("")
                file_line_no += 1
                continue

            # If inside a false ifdef branch, skip the line
            if not _ifdef_active():
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

            if stripped[:7] == "import ":
                target = stripped.split(None, 1)[1].strip()
                if not target:
                    raise ParseError(f"empty import target in {path}:{file_line_no}")

                # begin_segment_if_needed inline
                if segment_start_global is None:
                    segment_start_global = len(out_lines) + 1
                    segment_start_local = file_line_no
                _out_append("")
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
                continue

            if stripped[:9] == 'cimport "' or stripped[:9] == "cimport \"":
                # cimport "header.h" — extract extern declarations from a C header
                m_cimport = re.match(r'cimport\s+"([^"]+)"', stripped)
                if not m_cimport:
                    raise ParseError(f"invalid cimport syntax at {path}:{file_line_no}")
                header_target = m_cimport.group(1)
                header_path = self._resolve_import_target(path, header_target)
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
                # Replace the cimport line with the extracted extern + struct declarations
                for ext_line in extern_lines:
                    _out_append(ext_line)
                for st_line in struct_lines:
                    _out_append(st_line)
                _out_append("")  # blank line after externs
                file_line_no += 1
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


class BuildCache:
    """Caches compilation artifacts keyed by source content and compiler flags."""

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
            return json.loads(mp.read_text())
        except (ValueError, OSError):
            return None

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

def _find_nasm() -> str:
    global _nasm_path
    if _nasm_path:
        return _nasm_path
    import shutil
    p = shutil.which("nasm")
    if not p:
        raise RuntimeError("nasm not found")
    _nasm_path = p
    return p

def _find_linker() -> tuple:
    global _linker_path, _linker_is_lld
    if _linker_path:
        return _linker_path, _linker_is_lld
    import shutil
    lld = shutil.which("ld.lld")
    if lld:
        _linker_path = lld
        _linker_is_lld = True
        return lld, True
    ld = shutil.which("ld")
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


def _iter_doc_files(roots: Sequence[Path], *, include_tests: bool = False) -> List[Path]:
    seen: Set[Path] = set()
    files: List[Path] = []
    skip_parts = {"build", ".git", ".venv", "raylib-5.5_linux_amd64"}
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
        for path in resolved.rglob("*.sl"):
            if _should_skip(path):
                continue
            candidate = path.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            files.append(candidate)
    files.sort()
    return files


def collect_docs(
    roots: Sequence[Path],
    *,
    include_undocumented: bool = False,
    include_private: bool = False,
    include_macros: bool = False,
    include_tests: bool = False,
) -> List[DocEntry]:
    entries: List[DocEntry] = []
    for doc_file in _iter_doc_files(roots, include_tests=include_tests):
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

    _TAB_LIBRARY = 0
    _TAB_LANG_REF = 1
    _TAB_CT_REF = 2
    _TAB_NAMES = ["Library Docs", "Language Reference", "Compile-Time Reference"]

    _FILTER_KINDS = ["all", "word", "asm", "py", "macro"]

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
            "syntax": "macro <name> <param_count> <tokens...> ;",
            "summary": "Define a text macro with positional substitution.",
            "detail": (
                "Records raw tokens until `;`. On expansion, `$0`, `$1`, ... "
                "(exact `$<number>` tokens only) are replaced by positional "
                "arguments. A bare `$` token is left unchanged (useful in asm). "
                "Macros cannot nest.\n\n"
                "Use --macro-preview to print fully expanded macro tokens "
                "during parsing.\n\n"
                "Example:\n"
                "  macro max2 2 $0 $1 > if $0 else $1 end ;\n"
                "  5 3 max2   # leaves 5 on stack"
            ),
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
            "name": "if ... end",
            "category": "Control Flow",
            "syntax": "<cond> if <body> end\n<cond> if <then> else <otherwise> end",
            "summary": "Conditional execution — pops a flag from the stack.",
            "detail": (
                "Pops the top of stack. If non-zero, executes the `then` branch; "
                "otherwise executes the `else` branch (if present).\n\n"
                "For else-if chains, place `if` on the same line as `else`:\n"
                "  <cond1> if\n"
                "    ... branch 1 ...\n"
                "  else <cond2> if\n"
                "    ... branch 2 ...\n"
                "  else\n"
                "    ... fallback ...\n"
                "  end\n\n"
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
        "  All words listed below are compile-only: they exist only\n"
        "  during compilation and produce no runtime code.\n"
        "\n"
        "  Stack notation:  [*, deeper, deeper | top] -> [*] || [*, result]\n"
        "    *   = rest of stack (unchanged)\n"
        "    |   = separates deeper elements from the top\n"
        "    ->  = before / after\n"
        "    ||  = separates alternative stack effects\n"
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
        "  inline                                   [immediate]\n"
        "    Mark a word for inline expansion: its body\n"
        "    is expanded at each call site instead of emitting a call.\n"
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
        "  inject-tokens       [* | list-of-tokens] -> [*]\n"
        "    Insert a list of token objects at the current parser\n"
        "    position. The parser will read them before continuing\n"
        "    with the original stream.\n"
        "\n"
        "  add-token           [* | str] -> [*]\n"
        "    Register a single-character string as a token separator\n"
        "    recognized by the reader.\n"
        "\n"
        "  add-token-chars     [* | str] -> [*]\n"
        "    Register each character of the string as a token\n"
        "    separator character.\n"
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
        "  assembly: the prelude (code inside _start) and the\n"
        "  BSS section (uninitialized data).\n"
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
        "  macro <name> <number> <body...> ;\n"
        "    Define a text macro that expands during tokenization.\n"
        "    The number is the parameter count. The body tokens are\n"
        "    substituted literally wherever the macro is invoked.\n"
        "    Only exact `$<number>` tokens are replaced (for example\n"
        "    $0, $1). Bare `$` tokens remain unchanged for asm use.\n"
        "    Use --macro-preview to print each expansion.\n"
        "\n"
        "      macro BUFFER_SIZE 0 4096 ;\n"
        "      macro MAX 2 >r dup r> dup >r < if drop r> else r> drop end ;\n"
        "\n"
        "  :py { ... }\n"
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
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  § 17  SUMMARY TABLE\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "  Word                  Category        Stack Effect\n"
        "  ────────────────────  ──────────────  ──────────────────────────\n"
        "  nil                   Nil             [*] -> [* | nil]\n"
        "  nil?                  Nil             [* | v] -> [* | flag]\n"
        "  list-new              List            [*] -> [* | list]\n"
        "  list-clone            List            [* | list] -> [* | copy]\n"
        "  list-append           List            [*, list | v] -> [* | list]\n"
        "  list-pop              List            [* | list] -> [*, list | v]\n"
        "  list-pop-front        List            [* | list] -> [*, list | v]\n"
        "  list-peek-front       List            [* | list] -> [*, list | v]\n"
        "  list-push-front       List            [*, list | v] -> [* | list]\n"
        "  list-reverse          List            [* | list] -> [* | list]\n"
        "  list-length           List            [* | list] -> [* | n]\n"
        "  list-empty?           List            [* | list] -> [* | flag]\n"
        "  list-get              List            [*, list | i] -> [* | v]\n"
        "  list-set              List            [*, list, i | v] -> [* | list]\n"
        "  list-clear            List            [* | list] -> [* | list]\n"
        "  list-extend           List            [*, tgt | src] -> [* | tgt]\n"
        "  list-last             List            [* | list] -> [* | v]\n"
        "  map-new               Map             [*] -> [* | map]\n"
        "  map-set               Map             [*, map, k | v] -> [* | map]\n"
        "  map-get               Map             [*, map | k] -> [*, map, v | f]\n"
        "  map-has?              Map             [*, map | k] -> [*, map | f]\n"
        "  string=               String          [*, a | b] -> [* | flag]\n"
        "  string-length         String          [* | s] -> [* | n]\n"
        "  string-append         String          [*, l | r] -> [* | lr]\n"
        "  string>number         String          [* | s] -> [*, v | flag]\n"
        "  int>string            String          [* | n] -> [* | s]\n"
        "  identifier?           String          [* | v] -> [* | flag]\n"
        "  next-token            Token           [*] -> [* | tok]\n"
        "  peek-token            Token           [*] -> [* | tok]\n"
        "  token-lexeme          Token           [* | tok] -> [* | s]\n"
        "  token-from-lexeme     Token           [*, s | tmpl] -> [* | tok]\n"
        "  inject-tokens         Token           [* | list] -> [*]\n"
        "  add-token             Token           [* | s] -> [*]\n"
        "  add-token-chars       Token           [* | s] -> [*]\n"
        "  emit-definition       Token           [*, name | body] -> [*]\n"
        "  ct-control-frame-new  Control         [* | type] -> [* | frame]\n"
        "  ct-control-get        Control         [*, frame | key] -> [* | value]\n"
        "  ct-control-set        Control         [*, frame, key | value] -> [* | frame]\n"
        "  ct-control-push       Control         [* | frame] -> [*]\n"
        "  ct-control-pop        Control         [*] -> [* | frame]\n"
        "  ct-control-peek       Control         [*] -> [* | frame]\n"
        "  ct-control-depth      Control         [*] -> [* | n]\n"
        "  ct-control-add-close-op Control       [*, frame, op | data] -> [* | frame]\n"
        "  ct-new-label          Control         [* | prefix] -> [* | label]\n"
        "  ct-emit-op            Control         [*, op | data] -> [*]\n"
        "  ct-last-token-line    Control         [*] -> [* | line]\n"
        "  ct-register-block-opener Control      [* | name] -> [*]\n"
        "  ct-unregister-block-opener Control    [* | name] -> [*]\n"
        "  ct-register-control-override Control  [* | name] -> [*]\n"
        "  ct-unregister-control-override Control [* | name] -> [*]\n"
        "  set-token-hook        Hook            [* | name] -> [*]\n"
        "  clear-token-hook      Hook            [*] -> [*]\n"
        "  prelude-clear         Assembly        [*] -> [*]\n"
        "  prelude-append        Assembly        [* | line] -> [*]\n"
        "  prelude-set           Assembly        [* | list] -> [*]\n"
        "  bss-clear             Assembly        [*] -> [*]\n"
        "  bss-append            Assembly        [* | line] -> [*]\n"
        "  bss-set               Assembly        [* | list] -> [*]\n"
        "  shunt                 Expression      [* | list] -> [* | list]\n"
        "  i                     Loop            [*] -> [* | idx]\n"
        "  static_assert         Assert          [* | cond] -> [*]\n"
        "  parse-error           Assert          [* | msg] -> (aborts)\n"
        "  eval                  Eval            [* | str] -> [*]\n"
        "  lexer-new             Lexer           [* | seps] -> [* | lex]\n"
        "  lexer-pop             Lexer           [* | lex] -> [*, lex | tok]\n"
        "  lexer-peek            Lexer           [* | lex] -> [*, lex | tok]\n"
        "  lexer-expect          Lexer           [*, lex | s] -> [*, lex | tok]\n"
        "  lexer-collect-brace   Lexer           [* | lex] -> [*, lex | list]\n"
        "  lexer-push-back       Lexer           [* | lex] -> [* | lex]\n"
        "  use-l2-ct             Hook            [* | name?] -> [*]\n"
        "\n"
        "═══════════════════════════════════════════════════════════════\n"
    )

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
        "      - Set breakpoints on word labels  (b word_main)\n"
        "      - Inspect the data stack via r12  (x/8gx $r12)\n"
        "      - Step through asm instructions   (si / ni)\n"
        "      - View registers                  (info registers)\n"
        "      - Disassemble a word              (disas word_foo)\n"
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
        "    Text macros are template expansions. Define with\n"
        "    an optional parameter count:\n"
        "\n"
        "      macro square       # 0-arg: inline expansion\n"
        "        dup *\n"
        "      ;\n"
        "\n"
        "      macro defconst 2   # 2-arg: $0 and $1 are args\n"
        "        word $0\n"
        "          $1\n"
        "        end\n"
        "      ;\n"
        "\n"
        "    Use them normally; macro args are positional:\n"
        "\n"
        "      5 square           # expands to: 5 dup *\n"
        "      defconst TEN 10    # defines: word TEN 10 end\n"
        "\n"
        "    Placeholder substitution only applies to exact `$<number>`\n"
        "    tokens. This keeps bare `$` usable inside asm syntax.\n"
        "    Use --macro-preview to print each expansion while parsing.\n"
        "\n"
        "  WHAT IS THE L2 DATA MODEL FOR ARRAYS/STRINGS?\n"
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
        "       Comment lines (starting with #) in word bodies are\n"
        "       preserved as metadata but not compiled.\n"
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
        "       file, then linked (via ld or ld.ldd) into the final\n"
        "       binary.\n"
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
        "      Small deterministic loops (e.g., '4 for ... next')\n"
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
        "      Use --macro-preview to trace each expansion.\n"
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
                    info_lines = _L2_CT_REF_TEXT.splitlines()
                    info_scroll = 0
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
                    info_lines = _L2_CT_REF_TEXT.splitlines()
                    info_scroll = 0
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
                        info_lines = _L2_CT_REF_TEXT.splitlines()
                        info_scroll = 0
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

            # -- COMPILE-TIME REFERENCE MODE --
            if mode == _MODE_CT_REF:
                stdscr.erase()
                _safe_addnstr(stdscr, 0, 0, " Compile-Time Reference ", width - 1, curses.A_BOLD)
                _render_tab_bar(stdscr, 1, width)
                _safe_addnstr(stdscr, 2, 0, " j/k scroll  PgUp/PgDn  Tab switch  ? Q&A  H how  P philosophy  L license  q quit", width - 1, curses.A_DIM)
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
                    info_lines = _L2_CT_REF_TEXT.splitlines()
                    info_scroll = 0
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
                info_lines = _L2_CT_REF_TEXT.splitlines()
                info_scroll = 0
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
) -> int:
    roots: List[Path] = [Path("."), Path("./stdlib"), Path("./libs")]
    roots.extend(include_paths)
    roots.extend(explicit_roots)
    if source is not None:
        roots.append(source.parent)
        roots.append(source)

    collect_opts: Dict[str, Any] = dict(
        include_undocumented=include_undocumented,
        include_private=include_private,
        include_tests=include_tests,
        include_macros=False,
    )

    def _reload(**overrides: Any) -> List[DocEntry]:
        extra = overrides.pop("extra_roots", [])
        opts = {**collect_opts, **overrides}
        entries = collect_docs(roots, **opts)
        # Scan extra roots directly, bypassing _iter_doc_files skip filters
        # Always include undocumented entries from user-added paths
        if extra:
            seen_names = {e.name for e in entries}
            scan_opts = dict(
                include_undocumented=True,
                include_private=True,
                include_macros=opts.get("include_macros", False),
            )
            for p in extra:
                ep = Path(p).expanduser().resolve()
                if not ep.exists():
                    continue
                if ep.is_file() and ep.suffix == ".sl":
                    for e in _scan_doc_file(ep, **scan_opts):
                        if e.name not in seen_names:
                            seen_names.add(e.name)
                            entries.append(e)
                elif ep.is_dir():
                    for sl in sorted(ep.rglob("*.sl")):
                        for e in _scan_doc_file(sl.resolve(), **scan_opts):
                            if e.name not in seen_names:
                                seen_names.add(e.name)
                                entries.append(e)
            entries.sort(key=lambda item: (item.name.lower(), str(item.path), item.line))
        return entries

    entries = _reload()
    return _run_docs_tui(entries, initial_query=initial_query, reload_fn=_reload)


def cli(argv: Sequence[str]) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="L2 compiler driver")
    parser.add_argument("source", type=Path, nargs="?", default=None, help="input .sl file (optional when --clean is used)")
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
    parser.add_argument("-O0", dest="O0", action="store_true", help="disable all optimizations")
    parser.add_argument("-O2", dest="O2", action="store_true", help="fast mode: disable all optimizations and checks")
    parser.add_argument("-v", "--verbose", type=int, default=0, metavar="LEVEL", help="verbosity level (1=summary+timing, 2=per-function/DCE, 3=full debug, 4=optimization detail)")
    parser.add_argument("--no-extern-type-check", action="store_true", help="disable extern function argument count checking")
    parser.add_argument("--no-stack-check", action="store_true", help="disable stack underflow checking for builtins")
    parser.add_argument("--no-cache", action="store_true", help="disable incremental build cache")
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

    # Parse known and unknown args to allow -l flags anywhere
    args, unknown = parser.parse_known_args(argv)
    # Collect any -l flags from unknown args (e.g. -lfoo or -l foo)
    i = 0
    while i < len(unknown):
        if unknown[i] == "-l" and i + 1 < len(unknown):
            args.libs.append(unknown[i + 1])
            i += 2
        elif unknown[i].startswith("-l"):
            args.libs.append(unknown[i][2:])
            i += 1
        else:
            i += 1

    if args.script:
        args.no_artifact = True
        args.ct_run_main = True

    if args.check:
        args.no_artifact = True

    if args.macro_expansion_limit < 1:
        parser.error("--macro-expansion-limit must be >= 1")

    artifact_kind = args.artifact
    if args.O2:
        folding_enabled = False
        peephole_enabled = False
        loop_unroll_enabled = False
        auto_inline_enabled = False
        string_deduplication_enabled = not args.no_string_dedup
        extern_type_check_enabled = False
        stack_check_enabled = False
    elif args.O0:
        folding_enabled = False
        peephole_enabled = False
        loop_unroll_enabled = False
        auto_inline_enabled = not args.no_auto_inline
        string_deduplication_enabled = not args.no_string_dedup
        extern_type_check_enabled = not args.no_extern_type_check
        stack_check_enabled = not args.no_stack_check
    else:
        folding_enabled = not args.no_folding
        peephole_enabled = not args.no_peephole
        loop_unroll_enabled = not args.no_loop_unroll
        auto_inline_enabled = not args.no_auto_inline
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
        return run_docs_explorer(
            source=args.source,
            include_paths=args.include_paths,
            explicit_roots=args.docs_root,
            initial_query=str(args.docs_query or ""),
            include_undocumented=args.docs_all,
            include_private=args.docs_all,
            include_tests=args.docs_include_tests,
        )

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

    if not args.repl and args.output is None and not args.no_artifact:
        stem = args.source.stem
        default_outputs = {
            "exe": Path("a.out"),
            "shared": Path(f"lib{stem}.so"),
            "static": Path(f"lib{stem}.a"),
            "obj": Path(f"{stem}.o"),
        }
        args.output = default_outputs[artifact_kind]

    if not args.repl and artifact_kind in {"static", "obj"} and args.libs:
        print("[warn] --libs ignored for static/object outputs")

    ct_run_libs: List[str] = list(args.libs)
    if args.source is not None:
        for lib in _load_sidecar_meta_libs(args.source):
            if lib not in args.libs:
                args.libs.append(lib)
            if lib not in ct_run_libs:
                ct_run_libs.append(lib)

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

    compiler = Compiler(
        include_paths=[Path("."), Path("./stdlib"), *args.include_paths],
        macro_expansion_limit=args.macro_expansion_limit,
        macro_preview=args.macro_preview,
        defines=args.defines,
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
    # Route dictionary redefine warnings through the parser's _warn system
    if warnings_set or werror:
        def _dict_warn_cb(name: str, priority: int) -> None:
            compiler.parser._warn(
                compiler.parser._last_token, "redefine",
                f"redefining word {name} (priority {priority})",
            )
        compiler.parser.dictionary.warn_callback = _dict_warn_cb
    cache: Optional[BuildCache] = None
    if not args.no_cache:
        cache = BuildCache(args.temp_dir / ".l2cache")

    try:
        if args.repl:
            return run_repl(compiler, args.temp_dir, args.libs, debug=args.debug, initial_source=args.source)

        entry_mode = "program" if artifact_kind == "exe" else "library"

        # --- assembly-level cache check ---
        asm_text: Optional[str] = None
        fhash = ""
        if cache and not args.ct_run_main and args.dump_cfg is None:
            fhash = cache.flags_hash(
                args.debug,
                folding_enabled,
                peephole_enabled,
                auto_inline_enabled,
                string_deduplication_enabled,
                entry_mode,
            )
            manifest = cache.load_manifest(args.source)
            if manifest and cache.check_fresh(manifest, fhash):
                cached = cache.get_cached_asm(manifest)
                if cached is not None:
                    asm_text = cached
                    if verbosity >= 1:
                        print(f"[v1] cache hit for {args.source}")

        if asm_text is None:
            if verbosity >= 1:
                import time as _time_mod
                _compile_t0 = _time_mod.perf_counter()
            emission = compiler.compile_file(args.source, debug=args.debug, entry_mode=entry_mode)

            # Snapshot assembly text *before* ct-run-main JIT execution, which may
            # corrupt Python heap objects depending on memory layout.
            asm_text = emission.snapshot()
            if verbosity >= 1:
                _compile_dt = (_time_mod.perf_counter() - _compile_t0) * 1000
                print(f"[v1] compilation: {_compile_dt:.1f}ms")
                print(f"[v1] assembly size: {len(asm_text)} bytes")

            if cache and not args.ct_run_main:
                if not fhash:
                    fhash = cache.flags_hash(
                        args.debug,
                        folding_enabled,
                        peephole_enabled,
                        auto_inline_enabled,
                        string_deduplication_enabled,
                        entry_mode,
                    )
                has_ct = bool(compiler.parser.compile_time_vm._ct_executed)
                cache.save(args.source, compiler._loaded_files, fhash, asm_text, has_ct_effects=has_ct)

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

    args.temp_dir.mkdir(parents=True, exist_ok=True)
    asm_path = args.temp_dir / (args.source.stem + ".asm")
    obj_path = args.temp_dir / (args.source.stem + ".o")

    # --- incremental: skip nasm if assembly unchanged ---
    asm_changed = True
    if asm_path.exists():
        existing_asm = asm_path.read_text()
        if existing_asm == asm_text:
            asm_changed = False
    if asm_changed:
        asm_path.write_text(asm_text)

    if args.emit_asm:
        print(f"[info] wrote {asm_path}")
        return 0

    if args.no_artifact:
        print("[info] skipped artifact generation (--no-artifact)")
        return 0

    # --- incremental: skip nasm if .o newer than .asm ---
    need_nasm = asm_changed or not obj_path.exists()
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
    need_link = need_nasm or not args.output.exists() or args.no_cache
    if not need_link:
        # Check that the output was linked from the same .o last time.
        try:
            recorded = link_stamp.read_text()
        except OSError:
            recorded = ""
        if recorded != str(obj_path.resolve()):
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
        link_stamp.write_text(str(obj_path.resolve()))
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
