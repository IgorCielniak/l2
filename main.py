"""Bootstrap compiler for the L2 language.

This file now contains working scaffolding for:

* Parsing definitions, literals, and ordinary word references.
* Respecting immediate/macro words so syntax can be rewritten on the fly.
* Emitting NASM-compatible x86-64 assembly with explicit data and return stacks.
* Driving the toolchain via ``nasm`` + ``ld``.
"""

from __future__ import annotations

import argparse
import bisect
import ctypes
import hashlib
import json
import mmap
import os
import re
import shlex
import struct
import subprocess
import sys
import shutil
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
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


class ParseError(Exception):
    """Raised when the source stream cannot be parsed."""


class CompileError(Exception):
    """Raised when IR cannot be turned into assembly."""


class CompileTimeError(ParseError):
    """Raised when a compile-time word fails with context."""


# ---------------------------------------------------------------------------
# Tokenizer / Reader
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Token:
    lexeme: str
    line: int
    column: int
    start: int
    end: int

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Token({self.lexeme!r}@{self.line}:{self.column})"


@dataclass(frozen=True, slots=True)
class SourceLocation:
    path: Path
    line: int
    column: int


class Reader:
    """Default reader; users can swap implementations at runtime."""

    def __init__(self) -> None:
        self.line = 1
        self.column = 0
        self.custom_tokens: Set[str] = {"(", ")", "{", "}", ";", ",", "[", "]"}
        self._token_order: List[str] = sorted(self.custom_tokens, key=len, reverse=True)
        self._single_char_tokens: Set[str] = {t for t in self.custom_tokens if len(t) == 1}
        self._multi_char_tokens: List[str] = [t for t in self._token_order if len(t) > 1]

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

    def add_token_chars(self, chars: str) -> None:
        self.add_tokens(chars)

    def tokenize(self, source: str) -> Iterable[Token]:
        self.line = 1
        self.column = 0
        index = 0
        lexeme: List[str] = []
        token_start = 0
        token_line = 1
        token_column = 0
        source_len = len(source)
        while index < source_len:
            char = source[index]
            if char == '"':
                if lexeme:
                    yield Token("".join(lexeme), token_line, token_column, token_start, index)
                    lexeme.clear()
                    token_start = index
                    token_line = self.line
                    token_column = self.column
                index += 1
                self.column += 1
                string_parts = ['"']
                while True:
                    if index >= source_len:
                        raise ParseError("unterminated string literal")
                    ch = source[index]
                    string_parts.append(ch)
                    index += 1
                    if ch == "\n":
                        self.line += 1
                        self.column = 0
                    else:
                        self.column += 1
                    if ch == "\\":
                        if index >= source_len:
                            raise ParseError("unterminated string literal")
                        next_ch = source[index]
                        string_parts.append(next_ch)
                        index += 1
                        if next_ch == "\n":
                            self.line += 1
                            self.column = 0
                        else:
                            self.column += 1
                        continue
                    if ch == '"':
                        yield Token("".join(string_parts), token_line, token_column, token_start, index)
                        break
                continue
            if char == "#":
                while index < source_len and source[index] != "\n":
                    index += 1
                continue
            if char == ";" and index + 1 < source_len and source[index + 1].isalpha():
                if not lexeme:
                    token_start = index
                    token_line = self.line
                    token_column = self.column
                lexeme.append(";")
                index += 1
                self.column += 1
                continue
            matched_token: Optional[str] = None
            if char in self._single_char_tokens:
                matched_token = char
            elif self._multi_char_tokens:
                for tok in self._multi_char_tokens:
                    if source.startswith(tok, index):
                        matched_token = tok
                        break
            if matched_token is not None:
                if lexeme:
                    yield Token("".join(lexeme), token_line, token_column, token_start, index)
                    lexeme.clear()
                    token_start = index
                    token_line = self.line
                    token_column = self.column
                yield Token(matched_token, self.line, self.column, index, index + len(matched_token))
                index += len(matched_token)
                self.column += len(matched_token)
                token_start = index
                token_line = self.line
                token_column = self.column
                continue
            if char.isspace():
                if lexeme:
                    yield Token("".join(lexeme), token_line, token_column, token_start, index)
                    lexeme.clear()
                if char == "\n":
                    self.line += 1
                    self.column = 0
                else:
                    self.column += 1
                index += 1
                token_start = index
                token_line = self.line
                token_column = self.column
                continue
            if not lexeme:
                token_start = index
                token_line = self.line
                token_column = self.column
            lexeme.append(char)
            self.column += 1
            index += 1
        if lexeme:
            yield Token("".join(lexeme), token_line, token_column, token_start, source_len)


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
OP_OTHER = 11

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
}


@dataclass(slots=True)
class Op:
    """Flat operation used for both compile-time execution and emission."""

    op: str
    data: Any = None
    loc: Optional[SourceLocation] = None
    _word_ref: Optional["Word"] = field(default=None, repr=False, compare=False)
    _opcode: int = field(default=OP_OTHER, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._opcode = _OP_STR_TO_INT.get(self.op, OP_OTHER)


@dataclass(slots=True)
class Definition:
    name: str
    body: List[Op]
    immediate: bool = False
    compile_only: bool = False
    terminator: str = "end"
    inline: bool = False
    # Cached analysis (populated lazily by CT VM)
    _label_positions: Optional[Dict[str, int]] = field(default=None, repr=False, compare=False)
    _for_pairs: Optional[Dict[int, int]] = field(default=None, repr=False, compare=False)
    _begin_pairs: Optional[Dict[int, int]] = field(default=None, repr=False, compare=False)
    _words_resolved: bool = field(default=False, repr=False, compare=False)
    # Merged JIT runs: maps start_ip → (end_ip_exclusive, cache_key)
    _merged_runs: Optional[Dict[int, Tuple[int, str]]] = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class AsmDefinition:
    name: str
    body: str
    immediate: bool = False
    compile_only: bool = False
    effects: Set[str] = field(default_factory=set)


@dataclass(slots=True)
class Module:
    forms: List[Any]
    variables: Dict[str, str] = field(default_factory=dict)
    prelude: Optional[List[str]] = None
    bss: Optional[List[str]] = None


@dataclass(slots=True)
class MacroDefinition:
    name: str
    tokens: List[str]
    param_count: int = 0


@dataclass(slots=True)
class StructField:
    name: str
    offset: int
    size: int


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
        self._parser.emit_node(Op(op="literal", data=value))

    def emit_word(self, name: str) -> None:
        self._parser.emit_node(Op(op="word", data=name))

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


MacroHandler = Callable[[MacroContext], Optional[List[Op]]]
IntrinsicEmitter = Callable[["FunctionEmitter"], None]


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


@dataclass(slots=True)
class Word:
    name: str
    priority: int = 0
    immediate: bool = False
    definition: Optional[Union[Definition, AsmDefinition]] = None
    macro: Optional[MacroHandler] = None
    intrinsic: Optional[IntrinsicEmitter] = None
    macro_expansion: Optional[List[str]] = None
    macro_params: int = 0
    compile_time_intrinsic: Optional[Callable[["CompileTimeVM"], None]] = None
    runtime_intrinsic: Optional[Callable[["CompileTimeVM"], None]] = None
    compile_only: bool = False
    compile_time_override: bool = False
    is_extern: bool = False
    extern_inputs: int = 0
    extern_outputs: int = 0
    extern_signature: Optional[Tuple[List[str], str]] = None  # (arg_types, ret_type)
    inline: bool = False


_suppress_redefine_warnings = False


def _suppress_redefine_warnings_set(value: bool) -> None:
    global _suppress_redefine_warnings
    _suppress_redefine_warnings = value


@dataclass(slots=True)
class Dictionary:
    words: Dict[str, Word] = field(default_factory=dict)

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
            sys.stderr.write(f"[warn] redefining word {word.name} (priority {word.priority})\n")
        self.words[word.name] = word
        return word

    def lookup(self, name: str) -> Optional[Word]:
        return self.words.get(name)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


Context = Union[Module, Definition]


class Parser:
    def __init__(self, dictionary: Dictionary, reader: Optional[Reader] = None) -> None:
        self.dictionary = dictionary
        self.reader = reader or Reader()
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
        self.label_counter = 0
        self.token_hook: Optional[str] = None
        self._last_token: Optional[Token] = None
        self.variable_labels: Dict[str, str] = {}
        self.variable_words: Dict[str, str] = {}
        self.file_spans: List[FileSpan] = []
        self.compile_time_vm = CompileTimeVM(self)
        self.custom_prelude: Optional[List[str]] = None
        self.custom_bss: Optional[List[str]] = None
        self._pending_inline_definition: bool = False
        self._pending_priority: Optional[int] = None

    def _rebuild_span_index(self) -> None:
        """Rebuild bisect index after file_spans changes."""
        self._span_starts: List[int] = [s.start_line for s in self.file_spans]

    def location_for_token(self, token: Token) -> SourceLocation:
        if not hasattr(self, '_span_starts') or len(self._span_starts) != len(self.file_spans):
            self._rebuild_span_index()
        idx = bisect.bisect_right(self._span_starts, token.line) - 1
        if idx >= 0:
            span = self.file_spans[idx]
            if token.line < span.end_line:
                local_line = span.local_start_line + (token.line - span.start_line)
                return SourceLocation(span.path, local_line, token.column)
        return SourceLocation(Path("<source>"), token.line, token.column)

    def inject_token_objects(self, tokens: Sequence[Token]) -> None:
        """Insert tokens at the current parse position."""
        self.tokens[self.pos:self.pos] = list(tokens)

    # Public helpers for macros ------------------------------------------------
    def next_token(self) -> Token:
        return self._consume()

    def peek_token(self) -> Optional[Token]:
        self._ensure_tokens(self.pos)
        return None if self._eof() else self.tokens[self.pos]

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
        """Handle unified 'end' for all block types"""
        if not self.control_stack:
            raise ParseError("unexpected 'end' without matching block")

        entry = self.control_stack.pop()

        if entry["type"] in ("if", "elif"):
            # For if/elif without a trailing else
            if "false" in entry:
                self._append_op(Op(op="label", data=entry["false"]))
            if "end" in entry:
                self._append_op(Op(op="label", data=entry["end"]))
        elif entry["type"] == "else":
            self._append_op(Op(op="label", data=entry["end"]))
        elif entry["type"] == "while":
            self._append_op(Op(op="jump", data=entry["begin"]))
            self._append_op(Op(op="label", data=entry["end"]))
        elif entry["type"] == "for":
            # Emit ForEnd node for loop decrement
            self._append_op(Op(op="for_end", data={"loop": entry["loop"], "end": entry["end"]}))
        elif entry["type"] == "begin":
            self._append_op(Op(op="jump", data=entry["begin"]))
            self._append_op(Op(op="label", data=entry["end"]))

    # Parsing ------------------------------------------------------------------
    def parse(self, tokens: Iterable[Token], source: str) -> Module:
        self.tokens = []
        self._token_iter = iter(tokens)
        self._token_iter_exhausted = False
        self.source = source
        self.pos = 0
        self.variable_labels = {}
        self.variable_words = {}
        self.context_stack = [Module(forms=[], variables=self.variable_labels)]
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

        try:
            while not self._eof():
                token = self._consume()
                self._last_token = token
                if self._run_token_hook(token):
                    continue
                if self._handle_macro_recording(token):
                    continue
                lexeme = token.lexeme
                if self._pending_priority is not None and lexeme not in {
                    "word",
                    ":asm",
                    ":py",
                    "extern",
                    "inline",
                    "priority",
                }:
                    raise ParseError(
                        f"priority {self._pending_priority} must be followed by definition/extern"
                    )
                if lexeme == "[":
                    self._handle_list_begin()
                    continue
                if lexeme == "]":
                    self._handle_list_end(token)
                    continue
                if lexeme == "word":
                    inline_def = self._consume_pending_inline()
                    self._begin_definition(token, terminator="end", inline=inline_def)
                    continue
                if lexeme == "end":
                    if self.control_stack:
                        self._handle_end_control()
                        continue
                    if self._try_end_definition(token):
                        continue
                    raise ParseError(f"unexpected 'end' at {token.line}:{token.column}")
                if lexeme == ":asm":
                    self._parse_asm_definition(token)
                    continue
                if lexeme == ":py":
                    self._parse_py_definition(token)
                    continue
                if lexeme == "extern":
                    self._parse_extern(token)
                    continue
                if lexeme == "priority":
                    self._parse_priority_directive(token)
                    continue
                if lexeme == "if":
                    self._handle_if_control()
                    continue
                if lexeme == "else":
                    self._handle_else_control()
                    continue
                if lexeme == "for":
                    self._handle_for_control()
                    continue
                if lexeme == "while":
                    self._handle_while_control()
                    continue
                if lexeme == "do":
                    self._handle_do_control()
                    continue
                if self._maybe_expand_macro(token):
                    continue
                self._handle_token(token)
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
            raise ParseError("unterminated macro definition (missing ';')")
        if self._pending_priority is not None:
            raise ParseError(f"dangling priority {self._pending_priority} without following definition")

        if len(self.context_stack) != 1:
            raise ParseError("unclosed definition at EOF")
        if self.control_stack:
            raise ParseError("unclosed control structure at EOF")

        module = self.context_stack.pop()
        if not isinstance(module, Module):  # pragma: no cover - defensive
            raise ParseError("internal parser state corrupt")
        module.variables = dict(self.variable_labels)
        module.prelude = self.custom_prelude
        module.bss = self.custom_bss
        return module

    def _handle_list_begin(self) -> None:
        label = self._new_label("list")
        self._append_op(Op(op="list_begin", data=label))
        self._push_control({"type": "list", "label": label})

    def _handle_list_end(self, token: Token) -> None:
        entry = self._pop_control(("list",))
        label = entry["label"]
        self._append_op(Op(op="list_end", data=label))

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

    def _consume_pending_priority(self) -> int:
        if self._pending_priority is None:
            return 0
        value = self._pending_priority
        self._pending_priority = None
        return value

    # Internal helpers ---------------------------------------------------------

    def _parse_extern(self, token: Token) -> None:
        # extern <name> [inputs outputs]
        # OR
        # extern <ret_type> <name>(<args>)

        if self._eof():
            raise ParseError(f"extern missing name at {token.line}:{token.column}")

        priority = self._consume_pending_priority()
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
        inputs, arg_types = self._parse_c_param_list()
        outputs = 0 if ret_type == "void" else 1
        self._register_c_extern(name_lexeme, inputs, outputs, arg_types, ret_type, priority=priority)
        return True

    def _parse_c_param_list(self) -> Tuple[int, List[str]]:
        inputs = 0
        arg_types: List[str] = []

        if self._eof():
            raise ParseError("extern unclosed '('")
        peek = self.peek_token()
        if peek.lexeme == ")":
            self._consume()
            return inputs, arg_types

        while True:
            lexemes = self._collect_c_param_lexemes()
            arg_type = _normalize_c_type_tokens(lexemes, allow_default=False)
            if arg_type == "void" and inputs == 0:
                if self._eof():
                    raise ParseError("extern unclosed '(' after 'void'")
                closing = self._consume()
                if closing.lexeme != ")":
                    raise ParseError("expected ')' after 'void' in extern parameter list")
                return 0, []
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
        return inputs, arg_types

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
    ) -> None:
        candidate = Word(name=name, priority=priority)
        word = self.dictionary.register(candidate)
        if word is not candidate:
            return
        word.is_extern = True
        word.extern_inputs = inputs
        word.extern_outputs = outputs
        word.extern_signature = (arg_types, ret_type)

    def _handle_token(self, token: Token) -> None:
        if self._try_literal(token):
            return

        if token.lexeme.startswith("&"):
            target_name = token.lexeme[1:]
            if not target_name:
                raise ParseError(f"missing word name after '&' at {token.line}:{token.column}")
            self._append_op(Op(op="word_ptr", data=target_name))
            return

        word = self.dictionary.lookup(token.lexeme)
        if word and word.immediate:
            if word.macro:
                produced = word.macro(MacroContext(self))
                if produced:
                    for node in produced:
                        self._append_op(node)
            else:
                self._execute_immediate_word(word)
            return

        self._append_op(Op(op="word", data=token.lexeme))

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
        if token.lexeme == ";":
            self._finish_macro_recording(token)
        else:
            self.macro_recording.tokens.append(token.lexeme)
        return True

    def _maybe_expand_macro(self, token: Token) -> bool:
        word = self.dictionary.lookup(token.lexeme)
        if word and word.macro_expansion is not None:
            args = self._collect_macro_args(word.macro_params)
            self._inject_macro_tokens(word, token, args)
            return True
        return False

    def _inject_macro_tokens(self, word: Word, token: Token, args: List[str]) -> None:
        replaced: List[str] = []
        for lex in word.macro_expansion or []:
            if lex.startswith("$"):
                idx = int(lex[1:])
                if idx < 0 or idx >= len(args):
                    raise ParseError(f"macro {word.name} missing argument for {lex}")
                replaced.append(args[idx])
            else:
                replaced.append(lex)
        insertion = [
            Token(lexeme=lex, line=token.line, column=token.column, start=token.start, end=token.end)
            for lex in replaced
        ]
        self.tokens[self.pos:self.pos] = insertion

    def _collect_macro_args(self, count: int) -> List[str]:
        args: List[str] = []
        for _ in range(count):
            if self._eof():
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

    def _handle_if_control(self) -> None:
        token = self._last_token
        if (
            self.control_stack
            and self.control_stack[-1]["type"] == "else"
            and token is not None
            and self.control_stack[-1].get("line") == token.line
        ):
            entry = self.control_stack.pop()
            end_label = entry.get("end")
            if end_label is None:
                end_label = self._new_label("if_end")
            false_label = self._new_label("if_false")
            self._append_op(Op(op="branch_zero", data=false_label))
            self._push_control({"type": "elif", "false": false_label, "end": end_label})
            return
        false_label = self._new_label("if_false")
        self._append_op(Op(op="branch_zero", data=false_label))
        self._push_control({"type": "if", "false": false_label})

    def _handle_else_control(self) -> None:
        entry = self._pop_control(("if", "elif"))
        end_label = entry.get("end")
        if end_label is None:
            end_label = self._new_label("if_end")
        self._append_op(Op(op="jump", data=end_label))
        self._append_op(Op(op="label", data=entry["false"]))
        self._push_control({"type": "else", "end": end_label})

    def _handle_for_control(self) -> None:
        loop_label = self._new_label("for_loop")
        end_label = self._new_label("for_end")
        self._append_op(Op(op="for_begin", data={"loop": loop_label, "end": end_label}))
        self._push_control({"type": "for", "loop": loop_label, "end": end_label})

    def _handle_while_control(self) -> None:
        begin_label = self._new_label("begin")
        end_label = self._new_label("end")
        self._append_op(Op(op="label", data=begin_label))
        self._push_control({"type": "begin", "begin": begin_label, "end": end_label})

    def _handle_do_control(self) -> None:
        entry = self._pop_control(("begin",))
        self._append_op(Op(op="branch_zero", data=entry["end"]))
        self._push_control(entry)

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
        while not self._eof():
            next_token = self._consume()
            if next_token.lexeme == "}":
                block_end = next_token.start
                break
        if block_end is None:
            raise ParseError("missing '}' to terminate asm body")
        asm_body = self.source[block_start:block_end]
        priority = self._consume_pending_priority()
        definition = AsmDefinition(name=name_token.lexeme, body=asm_body)
        if effect_names is not None:
            definition.effects = set(effect_names)
        candidate = Word(name=definition.name, priority=priority)
        candidate.definition = definition
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
        while not self._eof():
            next_token = self._consume()
            if next_token.lexeme == "}":
                block_end = next_token.start
                break
        if block_end is None:
            raise ParseError("missing '}' to terminate py body")
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

    def _append_op(self, node: Op, token: Optional[Token] = None) -> None:
        if node.loc is None:
            tok = token or self._last_token
            if tok is not None:
                node.loc = self.location_for_token(tok)
        target = self.context_stack[-1]
        if isinstance(target, Module):
            target.forms.append(node)
        elif isinstance(target, Definition):
            target.body.append(node)
        else:  # pragma: no cover - defensive
            raise ParseError("unknown parse context")

    def _try_literal(self, token: Token) -> bool:
        lexeme = token.lexeme
        first = lexeme[0] if lexeme else '\0'
        if first.isdigit() or first == '-' or first == '+':
            try:
                value = int(lexeme, 0)
                self._append_op(Op(op="literal", data=value))
                return True
            except ValueError:
                pass

        # Try float
        if first.isdigit() or first == '-' or first == '+' or first == '.':
            try:
                if "." in lexeme or "e" in lexeme.lower():
                    value = float(lexeme)
                    self._append_op(Op(op="literal", data=value))
                    return True
            except ValueError:
                pass

        string_value = _parse_string_literal(token)
        if string_value is not None:
            self._append_op(Op(op="literal", data=string_value))
            return True

        return False

    def _consume(self) -> Token:
        self._ensure_tokens(self.pos)
        if self._eof():
            raise ParseError("unexpected EOF")
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def _eof(self) -> bool:
        self._ensure_tokens(self.pos)
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
        # Runtime-faithful execution state
        self.memory = CTMemory()
        self.runtime_mode: bool = False
        self._list_capture_stack: List[Any] = []  # for list_begin/list_end (int depth or native r12 addr)
        self._ct_executed: Set[str] = set()  # words already executed at CT
        # Native stack state (used only in runtime_mode)
        self.r12: int = 0  # data stack pointer (grows downward)
        self.r13: int = 0  # return stack pointer (grows downward)
        self._native_data_stack: Optional[Any] = None   # ctypes buffer
        self._native_data_top: int = 0
        self._native_return_stack: Optional[Any] = None  # ctypes buffer
        self._native_return_top: int = 0
        # JIT cache: word name → ctypes callable
        self._jit_cache: Dict[str, Any] = {}
        self._jit_code_pages: List[Any] = []  # keep mmap pages alive
        # Pre-allocated output structs for JIT calls (avoid per-call allocation)
        self._jit_out2 = (ctypes.c_int64 * 2)()
        self._jit_out2_addr = ctypes.addressof(self._jit_out2)
        self._jit_out4 = (ctypes.c_int64 * 4)()
        self._jit_out4_addr = ctypes.addressof(self._jit_out4)
        # BSS symbol table for JIT patching
        self._bss_symbols: Dict[str, int] = {}
        # dlopen handles for C extern support
        self._dl_handles: List[Any] = []  # ctypes.CDLL handles
        self._dl_func_cache: Dict[str, Any] = {}  # name → ctypes callable
        self._ct_libs: List[str] = []  # library names from -l flags
        self.current_location: Optional[SourceLocation] = None

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

    def invoke(self, word: Word, *, runtime_mode: bool = False, libs: Optional[List[str]] = None) -> None:
        self.reset()
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

    def push(self, value: Any) -> None:
        if self.runtime_mode:
            self.r12 -= 8
            if isinstance(value, float):
                bits = struct.unpack("q", struct.pack("d", value))[0]
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

    _CTYPE_MAP: Dict[str, Any] = {
        "int": ctypes.c_int,
        "long": ctypes.c_long,
        "long long": ctypes.c_longlong,
        "unsigned int": ctypes.c_uint,
        "unsigned long": ctypes.c_ulong,
        "size_t": ctypes.c_size_t,
        "char": ctypes.c_char,
        "char*": ctypes.c_void_p,  # use void* so raw integer addrs work
        "void*": ctypes.c_void_p,
        "double": ctypes.c_double,
        "float": ctypes.c_float,
    }

    def _resolve_ctype(self, type_name: str) -> Any:
        """Map a C type name string to a ctypes type."""
        t = type_name.strip().replace("*", "* ").replace("  ", " ").strip()
        if t in self._CTYPE_MAP:
            return self._CTYPE_MAP[t]
        # Pointer types
        if t.endswith("*"):
            return ctypes.c_void_p
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
        arg_types = func._ct_signature[0] if func._ct_signature else []

        # Pop arguments off the native data stack (right-to-left / reverse order)
        raw_args = []
        for i in range(inputs):
            raw_args.append(self.pop())
        raw_args.reverse()

        # Convert arguments to proper ctypes values
        call_args = []
        for i, raw in enumerate(raw_args):
            if i < len(arg_types) and arg_types[i] in ("float", "double"):
                # Reinterpret the int64 bits as a double (matching the language's convention)
                raw_int = _to_i64(int(raw))
                double_val = struct.unpack("d", struct.pack("q", raw_int))[0]
                call_args.append(double_val)
            else:
                call_args.append(int(raw))

        result = func(*call_args)

        if outputs > 0 and result is not None:
            ret_type = func._ct_signature[1] if func._ct_signature else None
            if ret_type in ("float", "double"):
                int_bits = struct.unpack("q", struct.pack("d", float(result)))[0]
                self.push(int_bits)
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

    _JIT_FUNC_TYPE = ctypes.CFUNCTYPE(None, ctypes.c_int64, ctypes.c_int64, ctypes.c_void_p)

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

        bss = self._bss_symbols

        # Build wrapper
        lines: List[str] = []
        # Entry: save callee-saved regs, set r12/r13, stash output ptr at [rsp]
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
        for raw_line in asm_body.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("extern"):
                continue  # strip extern declarations
            if line == "ret":
                line = "jmp _ct_save"

            # Patch [rel SYMBOL] → concrete address
            m = _RE_REL_PAT.search(line)
            if m and m.group(1) in bss:
                sym = m.group(1)
                addr = bss[sym]
                if line.lstrip().startswith("lea"):
                    # lea REG, [rel X] → mov REG, addr
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
            lines.append(f"    {line}")

        # Save: restore output ptr from [rsp], write r12/r13 out, restore regs
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
                f"JIT assembly failed for '{word.name}': {exc}\n--- asm ---\n{debug_txt}\n--- end ---"
            ) from exc
        if encoding is None:
            raise ParseError(f"JIT produced no code for '{word.name}'")

        code = bytes(encoding)
        # Allocate RWX memory via libc mmap (not Python's mmap module) so
        # Python's GC never tries to finalize the mapping.
        page_size = max(len(code), 4096)
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.mmap.restype = ctypes.c_void_p
        _libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_long]
        PROT_RWX = 0x1 | 0x2 | 0x4  # READ | WRITE | EXEC
        MAP_PRIVATE = 0x02
        MAP_ANONYMOUS = 0x20
        ptr = _libc.mmap(None, page_size, PROT_RWX,
                          MAP_PRIVATE | MAP_ANONYMOUS, -1, 0)
        if ptr == ctypes.c_void_p(-1).value or ptr is None:
            raise RuntimeError(f"mmap failed for JIT code ({page_size} bytes)")
        ctypes.memmove(ptr, code, len(code))
        # Store (ptr, size) so we can munmap later
        self._jit_code_pages.append((ptr, page_size))
        func = self._JIT_FUNC_TYPE(ptr)
        return func

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
            # Build BSS symbol table for [rel X] → concrete address substitution
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
                    # lea REG, [rel X]  →  mov REG, addr
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
        """Pre-resolve word name → Word objects on Op nodes (once per Definition)."""
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
                        continue  # just skip ret → fall through
                    else:
                        line = "jmp _ct_save"

                # Replace all references to local labels with prefixed versions
                for label in local_labels:
                    # Use word-boundary replacement to avoid partial matches
                    line = re.sub(rf'(?<!\w){re.escape(label)}(?=\s|:|,|$|\]|\))', prefix + label, line)

                # Patch [rel SYMBOL] → concrete address
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
        begin_stack: List[Dict[str, int]] = []

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

        n_nodes = len(nodes)
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
        _OP_LIST_END = OP_LIST_END
        _OP_LIST_LITERAL = OP_LIST_LITERAL
        try:
            while ip < n_nodes:
                node = nodes[ip]
                self.current_location = node.loc
                kind = node._opcode

                if kind == _OP_WORD:
                    # Merged JIT run: call one combined function for N words
                    if _merged_runs is not None:
                        run_info = _merged_runs.get(ip)
                        if run_info is not None:
                            end_ip, cache_key = run_info
                            func = _jit_cache.get(cache_key)
                            if func is None:
                                # Warmup: only compile merged function after seen 2+ times
                                hit_key = cache_key + "_hits"
                                hits = _jit_cache.get(hit_key, 0) + 1
                                _jit_cache[hit_key] = hits
                                if hits < 2:
                                    # Fall through to individual JIT calls
                                    pass
                                else:
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
                    word = node._word_ref
                    if word is not None:
                        # Inlined _call_word for common cases (JIT asm words)
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
                                # Ultra-hot path: inline JIT call, skip call_stack
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
                        # Fall through to full _call_word for other cases
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
                    name = str(node.data)
                    if name == "begin":
                        end_idx = begin_pairs.get(ip)
                        if end_idx is None:
                            raise ParseError("'begin' without matching 'again'")
                        begin_stack.append({"begin": ip, "end": end_idx})
                        ip += 1
                        continue
                    if name == "again":
                        if not begin_stack or begin_stack[-1]["end"] != ip:
                            raise ParseError("'again' without matching 'begin'")
                        ip = begin_stack[-1]["begin"] + 1
                        continue
                    if name == "continue":
                        if not begin_stack:
                            raise ParseError("'continue' outside begin/again loop")
                        ip = begin_stack[-1]["begin"] + 1
                        continue
                    if name == "exit":
                        if begin_stack:
                            frame = begin_stack.pop()
                            ip = frame["end"] + 1
                            continue
                        return
                    if _runtime_mode and name == "get_addr":
                        _push(ip + 1)
                        ip += 1
                        continue
                    # Lookup at runtime (rare: word was defined after body was compiled)
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

                if kind == _OP_WORD_PTR:
                    target_name = str(node.data)
                    target_word = _dict_lookup(target_name)
                    if target_word is None:
                        raise ParseError(
                            f"unknown word '{target_name}' referenced by pointer during compile-time execution"
                        )
                    _push(self._handles.store(target_word))
                    ip += 1
                    continue

                if kind == _OP_LITERAL:
                    data = node.data
                    if _runtime_mode and isinstance(data, str):
                        addr, length = self.memory.store_string(data)
                        _push(addr)
                        _push(length)
                    else:
                        _push(data)
                    ip += 1
                    continue

                if kind == _OP_FOR_END:
                    if not self.loop_stack:
                        raise ParseError("'next' without matching 'for'")
                    val = _peek_return() - 1
                    _poke_return(val)
                    if val > 0:
                        ip = self.loop_stack[-1]["begin"] + 1
                        continue
                    _pop_return()
                    self.loop_stack.pop()
                    ip += 1
                    continue

                if kind == _OP_FOR_BEGIN:
                    count = _pop_int()
                    if count <= 0:
                        match = loop_pairs.get(ip)
                        if match is None:
                            raise ParseError("internal loop bookkeeping error")
                        ip = match + 1
                        continue
                    _push_return(count)
                    self.loop_stack.append({"begin": ip})
                    ip += 1
                    continue

                if kind == _OP_BRANCH_ZERO:
                    condition = _pop()
                    if isinstance(condition, bool):
                        flag = condition
                    elif isinstance(condition, int):
                        flag = condition != 0
                    else:
                        raise ParseError("branch expects integer or boolean condition")
                    if not flag:
                        ip = label_positions.get(str(node.data), -1)
                        if ip == -1:
                            raise ParseError(f"unknown label '{node.data}' during compile-time execution")
                    else:
                        ip += 1
                    continue

                if kind == _OP_JUMP:
                    ip = label_positions.get(str(node.data), -1)
                    if ip == -1:
                        raise ParseError(f"unknown label '{node.data}' during compile-time execution")
                    continue

                if kind == _OP_LABEL:
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
                    values = list(node.data or [])
                    count = len(values)
                    buf_size = (count + 1) * 8
                    addr = self.memory.allocate(buf_size)
                    CTMemory.write_qword(addr, count)
                    for idx_item, val in enumerate(values):
                        CTMemory.write_qword(addr + 8 + idx_item * 8, int(val))
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
                            items.append(CTMemory.read_qword(ptr))
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

                raise ParseError(f"unsupported compile-time op {node!r}")
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


@dataclass(slots=True)
class Emission:
    text: List[str] = field(default_factory=list)
    data: List[str] = field(default_factory=list)
    bss: List[str] = field(default_factory=list)

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

    def set_location(self, loc: Optional[SourceLocation]) -> None:
        if not self.debug_enabled:
            return
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
        self.text.extend([
            f"    ; push {value}",
            "    sub r12, 8",
            f"    mov qword [r12], {value}",
        ])

    def push_float(self, label: str) -> None:
        self.text.extend([
            f"    ; push float from {label}",
            "    sub r12, 8",
            f"    mov rax, [rel {label}]",
            "    mov [r12], rax",
        ])

    def push_label(self, label: str) -> None:
        self.text.extend([
            f"    ; push {label}",
            "    sub r12, 8",
            f"    mov qword [r12], {label}",
        ])

    def push_from(self, register: str) -> None:
        self.text.extend([
            "    sub r12, 8",
            f"    mov [r12], {register}",
        ])

    def pop_to(self, register: str) -> None:
        self.text.extend([
            f"    mov {register}, [r12]",
            "    add r12, 8",
        ])


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
        loop_unroll_threshold: int = 8,
    ) -> None:
        self.dictionary = dictionary
        self._string_literals: Dict[str, Tuple[str, int]] = {}
        self._float_literals: Dict[float, str] = {}
        self._data_section: Optional[List[str]] = None
        self._inline_stack: List[str] = []
        self._inline_counter: int = 0
        self._unroll_counter: int = 0
        self._emit_stack: List[str] = []
        self._export_all_defs: bool = False
        self.enable_constant_folding = enable_constant_folding
        self.enable_peephole_optimization = enable_peephole_optimization
        self.loop_unroll_threshold = loop_unroll_threshold

    def _peephole_optimize_definition(self, definition: Definition) -> None:
        # Rewrite short stack-manipulation sequences into canonical forms.
        rules: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = [
            (("swap", "drop"), ("nip",)),
            # Stack no-ops
            (("dup", "drop"), tuple()),
            (("swap", "swap"), tuple()),
            (("over", "drop"), tuple()),
            (("dup", "nip"), tuple()),
            (("2dup", "2drop"), tuple()),
            (("2swap", "2swap"), tuple()),
            (("rot", "rot", "rot"), tuple()),
            # Canonicalizations
            (("swap", "over"), ("tuck",)),
            (("swap", "nip"), ("drop",)),
            (("nip", "drop"), ("2drop",)),
            (("tuck", "drop"), ("swap",)),
        ]

        max_pat_len = max(len(pattern) for pattern, _ in rules)

        # Build index: first word -> list of (pattern, replacement)
        rule_index: Dict[str, List[Tuple[Tuple[str, ...], Tuple[str, ...]]]] = {}
        for pattern, repl in rules:
            rule_index.setdefault(pattern[0], []).append((pattern, repl))

        nodes = definition.body
        changed = True
        while changed:
            changed = False
            optimized: List[Op] = []
            idx = 0
            while idx < len(nodes):
                node = nodes[idx]
                matched = False
                if node._opcode == OP_WORD:
                    candidates = rule_index.get(str(node.data))
                    if candidates:
                        for window in range(min(max_pat_len, len(nodes) - idx), 1, -1):
                            segment = nodes[idx:idx + window]
                            if any(n._opcode != OP_WORD for n in segment):
                                continue
                            names = tuple(str(n.data) for n in segment)
                            replacement: Optional[Tuple[str, ...]] = None
                            for pattern, repl in candidates:
                                if names == pattern:
                                    replacement = repl
                                    break
                            if replacement is None:
                                continue
                            base_loc = segment[0].loc
                            for repl_name in replacement:
                                optimized.append(Op(op="word", data=repl_name, loc=base_loc))
                            idx += window
                            changed = True
                            matched = True
                            break
                if matched:
                    continue
                optimized.append(nodes[idx])
                idx += 1
            nodes = optimized

        # Literal-aware algebraic identities and redundant unary chains.
        changed = True
        while changed:
            changed = False
            optimized = []
            idx = 0

            while idx < len(nodes):
                # Redundant unary pairs.
                if idx + 1 < len(nodes):
                    a = nodes[idx]
                    b = nodes[idx + 1]
                    if a._opcode == OP_WORD and b._opcode == OP_WORD:
                        wa = str(a.data)
                        wb = str(b.data)
                        if (wa, wb) in {
                            ("not", "not"),
                            ("neg", "neg"),
                        }:
                            idx += 2
                            changed = True
                            continue

                # Binary op identities where right operand is a literal.
                if idx + 1 < len(nodes):
                    lit = nodes[idx]
                    op = nodes[idx + 1]
                    if lit._opcode == OP_LITERAL and isinstance(lit.data, int) and op._opcode == OP_WORD:
                        k = int(lit.data)
                        w = str(op.data)
                        base_loc = lit.loc or op.loc

                        if (w == "+" and k == 0) or (w == "-" and k == 0) or (w == "*" and k == 1) or (w == "/" and k == 1):
                            idx += 2
                            changed = True
                            continue

                        if w == "*" and k == -1:
                            optimized.append(Op(op="word", data="neg", loc=base_loc))
                            idx += 2
                            changed = True
                            continue

                        if w == "%" and k == 1:
                            optimized.append(Op(op="word", data="drop", loc=base_loc))
                            optimized.append(Op(op="literal", data=0, loc=base_loc))
                            idx += 2
                            changed = True
                            continue

                        if w == "==" and k == 0:
                            optimized.append(Op(op="word", data="not", loc=base_loc))
                            idx += 2
                            changed = True
                            continue

                        if (w == "bor" and k == 0) or (w == "bxor" and k == 0):
                            idx += 2
                            changed = True
                            continue

                        if w == "band" and k == -1:
                            idx += 2
                            changed = True
                            continue

                        if w in {"shl", "shr", "sar"} and k == 0:
                            idx += 2
                            changed = True
                            continue

                optimized.append(nodes[idx])
                idx += 1

            nodes = optimized
        definition.body = nodes

    def _fold_constants_in_definition(self, definition: Definition) -> None:
        optimized: List[Op] = []
        for node in definition.body:
            optimized.append(node)
            self._attempt_constant_fold_tail(optimized)
        definition.body = optimized

    def _attempt_constant_fold_tail(self, nodes: List[Op]) -> None:
        while nodes:
            last = nodes[-1]
            if last.op != "word":
                return
            fold_entry = _FOLDABLE_WORDS.get(str(last.data))
            if fold_entry is None:
                return
            arity, func = fold_entry
            if len(nodes) < arity + 1:
                return
            operands = nodes[-(arity + 1):-1]
            if any(op._opcode != OP_LITERAL or not isinstance(op.data, int) for op in operands):
                return
            values = [int(op.data) for op in operands]
            try:
                result = func(*values)
            except Exception:
                return
            new_loc = operands[0].loc or last.loc
            nodes[-(arity + 1):] = [Op(op="literal", data=result, loc=new_loc)]

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
                cloned.append(Op(op="label", data=remap(str(data)), loc=node.loc))
                continue
            if kind == OP_JUMP or kind == OP_BRANCH_ZERO:
                target = str(data)
                mapped = remap(target) if target in internal_labels else target
                cloned.append(Op(op=node.op, data=mapped, loc=node.loc))
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
                cloned.append(Op(op=node.op, data=remap(str(data)), loc=node.loc))
                continue
            cloned.append(Op(op=node.op, data=data, loc=node.loc))
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
                rebuilt.append(Op(op="list_literal", data=static_values, loc=node.loc))
                idx = j + 1
                continue

            rebuilt.append(node)
            idx += 1

        definition.body = rebuilt

    def _reachable_runtime_defs(self, runtime_defs: Sequence[Union[Definition, AsmDefinition]], extra_roots: Optional[Sequence[str]] = None) -> Set[str]:
        edges: Dict[str, Set[str]] = {}
        for definition in runtime_defs:
            refs: Set[str] = set()
            if isinstance(definition, Definition):
                for node in definition.body:
                    if node.op in {"word", "word_ptr"}:
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
        pattern = re.compile(r"call\s+(?:qword\s+)?(?:\[rel\s+([A-Za-z0-9_.$@]+)\]|([A-Za-z0-9_.$@]+))")
        for m in pattern.finditer(asm_body):
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
            self._float_literals = {}
            self._data_section = emission.data

            valid_defs = (Definition, AsmDefinition)
            raw_defs = [form for form in module.forms if isinstance(form, valid_defs)]
            definitions = self._dedup_definitions(raw_defs)
            for defn in definitions:
                if isinstance(defn, Definition):
                    self._unroll_constant_for_loops(defn)
            if self.enable_peephole_optimization:
                for defn in definitions:
                    if isinstance(defn, Definition):
                        self._peephole_optimize_definition(defn)
            if self.enable_constant_folding:
                for defn in definitions:
                    if isinstance(defn, Definition):
                        self._fold_constants_in_definition(defn)
            for defn in definitions:
                if isinstance(defn, Definition):
                    self._fold_static_list_literals_definition(defn)
            stray_forms = [form for form in module.forms if not isinstance(form, valid_defs)]
            if stray_forms:
                raise CompileError("top-level literals or word references are not supported yet")

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
                    runtime_defs = [defn for defn in runtime_defs if defn.name in reachable]
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

            for definition in runtime_defs:
                self._emit_definition(definition, emission.text, debug=debug)

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
                self._emitted_start = any(l.strip().startswith("_start:") for l in emission.text)
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
                    "    mov r15, r12",
                    "    lea r13, [rel rstack_top]",
                    f"    call {sanitize_label('main')}",
                    "    mov rax, 0",
                    "    cmp r12, r15",
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
            emission.bss.extend(bss_lines)
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

    def _ensure_data_start(self) -> None:
        if self._data_section is None:
            raise CompileError("data section is not initialized")
        if not self._data_section:
            self._data_section.append("data_start:")

    def _intern_string_literal(self, value: str) -> Tuple[str, int]:
        if self._data_section is None:
            raise CompileError("string literal emission requested without data section")
        self._ensure_data_start()
        if value in self._string_literals:
            return self._string_literals[value]
        label = f"str_{len(self._string_literals)}"
        encoded = value.encode("utf-8")
        bytes_with_nul = list(encoded) + [0]
        byte_list = ", ".join(str(b) for b in bytes_with_nul)
        self._data_section.append(f"{label}: db {byte_list}")
        self._data_section.append(f"{label}_len equ {len(encoded)}")
        self._string_literals[value] = (label, len(encoded))
        return self._string_literals[value]

    def _intern_float_literal(self, value: float) -> str:
        if self._data_section is None:
            raise CompileError("float literal emission requested without data section")
        self._ensure_data_start()
        if value in self._float_literals:
            return self._float_literals[value]
        label = f"flt_{len(self._float_literals)}"
        # Use hex representation of double precision float
        import struct
        hex_val = struct.pack('>d', value).hex()
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
                self._emit_node(Op(op="label", data=mapped), builder)
                continue
            if kind == OP_JUMP:
                mapped = remap(str(data))
                self._emit_node(Op(op="jump", data=mapped), builder)
                continue
            if kind == OP_BRANCH_ZERO:
                mapped = remap(str(data))
                self._emit_node(Op(op="branch_zero", data=mapped), builder)
                continue
            if kind == OP_FOR_BEGIN:
                mapped = {
                    "loop": remap(data["loop"]),
                    "end": remap(data["end"]),
                }
                self._emit_node(Op(op="for_begin", data=mapped), builder)
                continue
            if kind == OP_FOR_END:
                mapped = {
                    "loop": remap(data["loop"]),
                    "end": remap(data["end"]),
                }
                self._emit_node(Op(op="for_end", data=mapped), builder)
                continue
            if kind == OP_LIST_BEGIN or kind == OP_LIST_END:
                mapped = remap(str(data))
                self._emit_node(Op(op=node.op, data=mapped), builder)
                continue
            self._emit_node(node, builder)

        self._emit_stack.pop()

    def _emit_asm_body(self, definition: AsmDefinition, builder: FunctionEmitter) -> None:
        body = definition.body.strip("\n")
        if not body:
            return
        import re
        for line in body.splitlines():
            if not line.strip():
                continue
            # Sanitize symbol references in raw asm bodies so they match
            # the sanitized labels emitted for high-level definitions.
            # Handle common patterns: `call NAME`, `global NAME`, `extern NAME`.
            def repl_sym(m: re.Match) -> str:
                name = m.group(1)
                return m.group(0).replace(name, sanitize_label(name))

            # `call NAME`
            line = re.sub(r"\bcall\s+([A-Za-z_][A-Za-z0-9_]*)\b", repl_sym, line)
            # `global NAME`
            line = re.sub(r"\bglobal\s+([A-Za-z_][A-Za-z0-9_]*)\b", repl_sym, line)
            # `extern NAME`
            line = re.sub(r"\bextern\s+([A-Za-z_][A-Za-z0-9_]*)\b", repl_sym, line)

            builder.emit(line)

    def _emit_node(self, node: Op, builder: FunctionEmitter) -> None:
        kind = node._opcode
        data = node.data
        builder.set_location(node.loc)

        def ctx() -> str:
            return f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""

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
            raise CompileError(f"unsupported literal type {type(data)!r}{ctx()}")

        if kind == OP_WORD:
            self._emit_wordref(str(data), builder)
            return

        if kind == OP_WORD_PTR:
            self._emit_wordptr(str(data), builder)
            return

        if kind == OP_BRANCH_ZERO:
            self._emit_branch_zero(str(data), builder)
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

        raise CompileError(f"unsupported op {node!r}{ctx()}")

    def _emit_wordref(self, name: str, builder: FunctionEmitter) -> None:
        word = self.dictionary.lookup(name)
        if word is None:
            suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
            raise CompileError(f"unknown word '{name}'{suffix}")
        if word.compile_only:
            return  # silently skip compile-time-only words during emission
        if getattr(word, "inline", False) and isinstance(word.definition, Definition):
            if word.name in self._inline_stack:
                suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
                raise CompileError(f"recursive inline expansion for '{word.name}'{suffix}")
            self._inline_stack.append(word.name)
            self._emit_inline_definition(word, builder)
            self._inline_stack.pop()
            return
        if word.intrinsic:
            word.intrinsic(builder)
            return
        if getattr(word, "is_extern", False):
            inputs = getattr(word, "extern_inputs", 0)
            outputs = getattr(word, "extern_outputs", 0)
            signature = getattr(word, "extern_signature", None)

            if signature is not None or inputs > 0 or outputs > 0:
                regs = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
                xmm_regs = [f"xmm{i}" for i in range(8)]

                arg_types = signature[0] if signature else []
                ret_type = signature[1] if signature else None

                if len(arg_types) != inputs and signature:
                    suffix = f" while emitting '{self._emit_stack[-1]}'" if self._emit_stack else ""
                    raise CompileError(f"extern '{name}' mismatch: {inputs} inputs vs {len(arg_types)} types{suffix}")

                int_idx = 0
                xmm_idx = 0

                mapping: List[Tuple[str, str]] = []  # (type, target)

                # Assign registers for first args; overflow goes to stack
                if not arg_types:
                    # Legacy/Raw mode: assume all ints
                    for i in range(inputs):
                        if int_idx < len(regs):
                            mapping.append(("int", regs[int_idx]))
                            int_idx += 1
                        else:
                            mapping.append(("int", "stack"))
                else:
                    for type_name in arg_types:
                        if type_name in ("float", "double"):
                            if xmm_idx < len(xmm_regs):
                                mapping.append(("float", xmm_regs[xmm_idx]))
                                xmm_idx += 1
                            else:
                                mapping.append(("float", "stack"))
                        else:
                            if int_idx < len(regs):
                                mapping.append(("int", regs[int_idx]))
                                int_idx += 1
                            else:
                                mapping.append(("int", "stack"))

                # Count stack slots required
                stack_slots = sum(1 for t, target in mapping if target == "stack")
                # stack allocation in bytes; make it a multiple of 16 for alignment
                stack_bytes = ((stack_slots * 8 + 15) // 16) * 16 if stack_slots > 0 else 0

                # Prepare stack-passed arguments: allocate space (16-byte multiple)
                if stack_bytes:
                    builder.emit(f"    sub rsp, {stack_bytes}")

                # Read all arguments from the CT stack by indexed addressing
                # (without advancing r12) and write them to registers or the
                # prepared spill area. After all reads are emitted we advance
                # r12 once by the total number of arguments to pop them.
                total_args = len(mapping)
                if stack_slots:
                    stack_write_idx = stack_slots - 1
                else:
                    stack_write_idx = 0

                # Iterate over reversed mapping (right-to-left) but use an
                # index to address the CT stack without modifying r12.
                for idx, (typ, target) in enumerate(reversed(mapping)):
                    addr = f"[r12 + {idx * 8}]" if idx > 0 else "[r12]"
                    if target == "stack":
                        # Read spilled arg from indexed CT stack slot and store
                        # it into the caller's spill area at the computed offset.
                        builder.emit(f"    mov rax, {addr}")
                        offset = stack_write_idx * 8
                        builder.emit(f"    mov [rsp + {offset}], rax")
                        stack_write_idx -= 1
                    else:
                        if typ == "float":
                            builder.emit(f"    mov rax, {addr}")
                            builder.emit(f"    movq {target}, rax")
                        else:
                            builder.emit(f"    mov {target}, {addr}")

                # Advance the CT stack pointer once to pop all arguments.
                if total_args:
                    builder.emit(f"    add r12, {total_args * 8}")

                # Call the external function. We allocated a multiple-of-16
                # area for spilled args above so `rsp` is already aligned
                # for the call; set `al` (SSE count) then call directly.
                builder.emit(f"    mov al, {xmm_idx}")
                builder.emit(f"    call {name}")

                # Restore stack after the call
                if stack_bytes:
                    builder.emit(f"    add rsp, {stack_bytes}")

                # Handle Return Value
                if _ctype_uses_sse(ret_type):
                    # Result in xmm0, move to stack
                    builder.emit("    sub r12, 8")
                    builder.emit("    movq rax, xmm0")
                    builder.emit("    mov [r12], rax")
                elif outputs == 1:
                    builder.push_from("rax")
                elif outputs > 1:
                    raise CompileError("extern only supports 0 or 1 output")
            else:
                # Emit call to unresolved symbol (let linker resolve it)
                builder.emit(f"    call {name}")
        else:
            builder.emit(f"    call {sanitize_label(name)}")

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
    if next_tok is None or next_tok.lexeme != "word":
        raise ParseError("'inline' must be followed by 'word'")
    if parser._pending_inline_definition:
        raise ParseError("duplicate 'inline' before 'word'")
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
    parser.emit_node(Op(op="label", data=name))
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
    parser.emit_node(Op(op="jump", data=name))
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
        parser.emit_node(Op(op="word", data=name))
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
    depth = 0
    while True:
        if parser._eof():
            raise ParseError("unterminated 'with' block (missing 'end')")
        tok = parser.next_token()
        if tok.lexeme == "end":
            if depth == 0:
                break
            depth -= 1
            body.append(tok)
            continue
        if tok.lexeme in ("with", "if", "for", "while", "begin", "word"):
            depth += 1
        body.append(tok)

    helper_for: Dict[str, str] = {}
    for name in names:
        _, helper = parser.allocate_variable(name)
        helper_for[name] = helper

    emitted: List[str] = []

    # Initialize variables by storing current stack values into their buffers
    for name in reversed(names):
        helper = helper_for[name]
        emitted.append(helper)
        emitted.append("swap")
        emitted.append("!")

    i = 0
    while i < len(body):
        tok = body[i]
        name = tok.lexeme
        helper = helper_for.get(name)
        if helper is not None:
            next_tok = body[i + 1] if i + 1 < len(body) else None
            if next_tok is not None and next_tok.lexeme == "!":
                emitted.append(helper)
                emitted.append("swap")
                emitted.append("!")
                i += 2
                continue
            if next_tok is not None and next_tok.lexeme == "@":
                emitted.append(helper)
                i += 1
                continue
            emitted.append(helper)
            emitted.append("@")
            i += 1
            continue
        emitted.append(tok.lexeme)
        i += 1

    ctx.inject_tokens(emitted, template=template)
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
    #   TOS:   syscall number → rax
    #   TOS-1: arg count → rcx
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
    # Same signature: (r12, r13, out_ptr) → void
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

def macro_here(ctx: MacroContext) -> Optional[List[Op]]:
    tok = ctx.parser._last_token
    if tok is None:
        return [Op(op="literal", data="<source>:0:0")]
    loc = ctx.parser.location_for_token(tok)
    return [Op(op="literal", data=f"{loc.path.name}:{loc.line}:{loc.column}")]


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
    _register_compile_time_primitives(dictionary)
    _register_runtime_intrinsics(dictionary)
    return dictionary


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileSpan:
    path: Path
    start_line: int  # inclusive (global line number in expanded source, 1-based)
    end_line: int    # exclusive
    local_start_line: int  # 1-based line in the original file


class Compiler:
    def __init__(self, include_paths: Optional[Sequence[Path]] = None) -> None:
        self.reader = Reader()
        self.dictionary = bootstrap_dictionary()
        self._syscall_label_counter = 0
        self._register_syscall_words()
        self.parser = Parser(self.dictionary, self.reader)
        self.assembler = Assembler(self.dictionary)
        if include_paths is None:
            include_paths = [Path("."), Path("./stdlib")]
        self.include_paths: List[Path] = [p.expanduser().resolve() for p in include_paths]
        self._loaded_files: Set[Path] = set()

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

    def _resolve_import_target(self, importing_file: Path, target: str) -> Path:
        raw = Path(target)
        tried: List[Path] = []

        if raw.is_absolute():
            candidate = raw
            tried.append(candidate)
            if candidate.exists():
                return candidate.resolve()

        candidate = (importing_file.parent / raw).resolve()
        tried.append(candidate)
        if candidate.exists():
            return candidate

        for base in self.include_paths:
            candidate = (base / raw).resolve()
            tried.append(candidate)
            if candidate.exists():
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
        path = path.resolve()
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

        def begin_segment_if_needed() -> None:
            nonlocal segment_start_global, segment_start_local
            if segment_start_global is None:
                segment_start_global = len(out_lines) + 1
                segment_start_local = file_line_no

        def close_segment_if_open() -> None:
            nonlocal segment_start_global
            if segment_start_global is None:
                return
            spans.append(
                FileSpan(
                    path=path,
                    start_line=segment_start_global,
                    end_line=len(out_lines) + 1,
                    local_start_line=segment_start_local,
                )
            )
            segment_start_global = None

        def scan_line(line: str) -> None:
            nonlocal brace_depth, string_char, escape
            for ch in line:
                if string_char:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == string_char:
                        string_char = None
                else:
                    if ch in ("'", '"'):
                        string_char = ch
                    elif ch == "{":
                        brace_depth += 1
                    elif ch == "}":
                        brace_depth -= 1

        for idx, line in enumerate(contents.splitlines()):
            stripped = line.strip()

            if not in_py_block and stripped.startswith(":py") and "{" in stripped:
                in_py_block = True
                brace_depth = 0
                string_char = None
                escape = False
                scan_line(line)
                begin_segment_if_needed()
                out_lines.append(line)
                file_line_no += 1
                if brace_depth == 0:
                    in_py_block = False
                continue

            if in_py_block:
                scan_line(line)
                begin_segment_if_needed()
                out_lines.append(line)
                file_line_no += 1
                if brace_depth == 0:
                    in_py_block = False
                continue

            if stripped.startswith("import "):
                target = stripped.split(None, 1)[1].strip()
                if not target:
                    raise ParseError(f"empty import target in {path}:{idx + 1}")

                # Keep a placeholder line so line numbers in the importing file stay stable.
                begin_segment_if_needed()
                out_lines.append("")
                file_line_no += 1
                close_segment_if_open()

                target_path = self._resolve_import_target(path, target)
                self._append_file_with_imports(target_path, out_lines, spans, seen)
                continue

            begin_segment_if_needed()
            out_lines.append(line)
            file_line_no += 1

        close_segment_if_open()


class BuildCache:
    """Caches compilation artifacts keyed by source content and compiler flags."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hash_str(s: str) -> str:
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _manifest_path(self, source: Path) -> Path:
        key = self._hash_str(str(source.resolve()))
        return self.cache_dir / f"{key}.json"

    def flags_hash(self, debug: bool, folding: bool, peephole: bool, entry_mode: str) -> str:
        return self._hash_str(
            f"debug={debug},folding={folding},peephole={peephole},entry_mode={entry_mode}"
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
            return json.loads(mp.read_text())
        except (json.JSONDecodeError, OSError):
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
        self._manifest_path(source).write_text(json.dumps(manifest))

    def clean(self) -> None:
        if self.cache_dir.exists():
            shutil.rmtree(self.cache_dir)


def run_nasm(asm_path: Path, obj_path: Path, debug: bool = False) -> None:
    cmd = ["nasm", "-f", "elf64"]
    if debug:
        cmd.extend(["-g", "-F", "dwarf"])
    cmd += ["-o", str(obj_path), str(asm_path)]
    subprocess.run(cmd, check=True)


def run_linker(obj_path: Path, exe_path: Path, debug: bool = False, libs=None, *, shared: bool = False):
    libs = libs or []

    lld = shutil.which("ld.lld")
    ld = shutil.which("ld")

    if lld:
        linker = lld
        use_lld = True
    elif ld:
        linker = ld
        use_lld = False
    else:
        raise RuntimeError("No linker found")

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
        if not shared:
            cmd.extend([
                "-dynamic-linker", "/lib64/ld-linux-x86-64.so.2",
            ])
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

    subprocess.run(cmd, check=True)


def build_static_library(obj_path: Path, archive_path: Path) -> None:
    parent = archive_path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ar", "rcs", str(archive_path), str(obj_path)], check=True)


def run_repl(
    compiler: Compiler,
    temp_dir: Path,
    libs: Sequence[str],
    debug: bool = False,
    initial_source: Optional[Path] = None,
) -> int:
    """REPL backed by the compile-time VM for instant execution."""

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
            print(f"[repl] loaded {initial_source}")
        except Exception as exc:
            print(f"[repl] failed to load {initial_source}: {exc}")

    def _run_on_ct_vm(source: str, word_name: str = "main") -> bool:
        """Parse source and execute word_name via the compile-time VM.

        Returns True on success, False on error (already printed).
        """
        nonlocal compiler
        src_path.write_text(source)
        try:
            _suppress_redefine_warnings_set(True)
            compiler._loaded_files.clear()
            compiler.parse_file(src_path)
        except (ParseError, CompileError, CompileTimeError) as exc:
            print(f"[error] {exc}")
            return False
        except Exception as exc:
            print(f"[error] parse failed: {exc}")
            return False
        finally:
            _suppress_redefine_warnings_set(False)

        try:
            compiler.run_compile_time_word(word_name, libs=list(libs))
        except (CompileTimeError, _CTVMExit) as exc:
            if isinstance(exc, _CTVMExit):
                code = exc.args[0] if exc.args else 0
                if code != 0:
                    print(f"[warn] program exited with code {code}")
            else:
                print(f"[error] {exc}")
                return False
        except Exception as exc:
            print(f"[error] execution failed: {exc}")
            return False
        return True

    def _print_help() -> None:
        print("[repl] commands:")
        print("  :help              show this help")
        print("  :show              display current session source (with synthetic main if pending snippet)")
        print("  :reset             clear session imports/defs")
        print("  :load <file>       load a source file into the session")
        print("  :call <word>       execute a word via the compile-time VM")
        print("  :edit [file]       open session file or given file in editor")
        print("  :seteditor [cmd]   show/set editor command (default from $EDITOR or vim)")
        print("  :quit | :q         exit the REPL")
        print("[repl] free-form input:")
        print("  definitions (word/:asm/:py/extern/macro/struct) extend the session")
        print("  imports add to session imports")
        print("  other lines run immediately via the compile-time VM (not saved)")
        print("  multiline: end lines with \\ to continue; finish with a non-\\ line")

    print("[repl] type L2 code; :help for commands; :quit to exit")
    print("[repl] execution via compile-time VM (instant, no nasm/ld)")
    print("[repl] enter multiline with trailing \\; finish with a line without \\")

    pending_block: List[str] = []

    while True:
        try:
            line = input("l2> ")
        except EOFError:
            print()
            break

        stripped = line.strip()
        if stripped in {":quit", ":q"}:
            break
        if stripped == ":help":
            _print_help()
            continue
        if stripped == ":reset":
            imports = list(default_imports)
            user_defs_files.clear()
            user_defs_repl.clear()
            main_body.clear()
            has_user_main = False
            pending_block.clear()
            # Re-create compiler for a clean dictionary state
            compiler = Compiler(include_paths=include_paths)
            print("[repl] session cleared")
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
                print(f"[repl] failed to sync source before edit: {exc}")
            try:
                if not target_path.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.touch()
                cmd_parts = shlex.split(editor_cmd)
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
                        print("[repl] reloaded session source from editor")
                    except Exception as exc:
                        print(f"[repl] failed to reload edited source: {exc}")
            except Exception as exc:
                print(f"[repl] failed to launch editor: {exc}")
            continue
        if stripped == ":show":
            source = _repl_build_source(imports, user_defs_files, user_defs_repl, main_body, has_user_main, force_synthetic=True)
            print(source.rstrip())
            continue
        if stripped.startswith(":load "):
            path_text = stripped.split(None, 1)[1].strip()
            target_path = Path(path_text)
            if not target_path.exists():
                print(f"[repl] file not found: {target_path}")
                continue
            try:
                loaded_text = target_path.read_text()
                user_defs_files.append(loaded_text)
                if _block_defines_main(loaded_text):
                    has_user_main = True
                    main_body.clear()
                print(f"[repl] loaded {target_path}")
            except Exception as exc:
                print(f"[repl] failed to load {target_path}: {exc}")
            continue
        if stripped.startswith(":call "):
            word_name = stripped.split(None, 1)[1].strip()
            if not word_name:
                print("[repl] usage: :call <word>")
                continue
            if word_name == "main" and not has_user_main:
                print("[repl] cannot call main; no user-defined main present")
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
            # Execute snippet immediately via the compile-time VM.
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
            print(f"[error] {exc}")
            continue

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


@dataclass(frozen=True)
class DocEntry:
    name: str
    stack_effect: str
    description: str
    kind: str
    path: Path
    line: int


_DOC_STACK_RE = re.compile(r"^\s*#\s*([^\s]+)\s*(.*)$")
_DOC_WORD_RE = re.compile(r"^\s*(?:inline\s+)?word\s+([^\s]+)\b")
_DOC_ASM_RE = re.compile(r"^\s*:asm\s+([^\s{]+)")
_DOC_PY_RE = re.compile(r"^\s*:py\s+([^\s{]+)")
_DOC_MACRO_RE = re.compile(r"^\s*macro\s+([^\s]+)")


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


def _extract_definition_name(text: str, *, include_macros: bool = False) -> Optional[Tuple[str, str]]:
    for kind, regex in (("word", _DOC_WORD_RE), ("asm", _DOC_ASM_RE), ("py", _DOC_PY_RE)):
        match = regex.match(text)
        if match is not None:
            return kind, match.group(1)
    if include_macros:
        match = _DOC_MACRO_RE.match(text)
        if match is not None:
            return "macro", match.group(1)
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
        kind, name = parsed
        if not _is_doc_symbol_name(name, include_private=include_private):
            continue
        defined_names.add(name)
        stack_effect, description = _collect_leading_doc_comments(lines, idx, name)
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

    _FILTER_KINDS = ["all", "word", "asm", "py", "macro"]

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

        nonlocal entries
        query = initial_query
        selected = 0
        scroll = 0
        mode = _MODE_BROWSE

        # Search mode state
        search_buf = query

        # Detail mode state
        detail_scroll = 0
        detail_lines: List[str] = []

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

            # -- BROWSE MODE --
            list_height = max(1, height - 4)
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
            hint = " / search  f filters  r reload  Enter detail  j/k nav  q quit"
            _safe_addnstr(stdscr, 1, 0, hint, width - 1, curses.A_DIM)

            for row in range(list_height):
                idx = scroll + row
                if idx >= len(filtered):
                    break
                entry = filtered[idx]
                effect = entry.stack_effect if entry.stack_effect else ""
                kind_tag = f"[{entry.kind}]"
                line = f" {entry.name:24} {effect:30} {kind_tag}"
                attr = curses.A_REVERSE if idx == selected else 0
                _safe_addnstr(stdscr, 2 + row, 0, line, width - 1, attr)

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

    artifact_kind = args.artifact
    folding_enabled = not args.no_folding
    peephole_enabled = not args.no_peephole

    if args.ct_run_main and artifact_kind != "exe":
        parser.error("--ct-run-main requires --artifact exe")

    if artifact_kind != "exe" and (args.run or args.dbg):
        parser.error("--run/--dbg are only available when --artifact exe is selected")

    if args.no_artifact and (args.run or args.dbg):
        parser.error("--run/--dbg are not available with --no-artifact")

    if args.clean:
        try:
            if args.temp_dir.exists():
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

    compiler = Compiler(include_paths=[Path("."), Path("./stdlib"), *args.include_paths])
    compiler.assembler.enable_constant_folding = folding_enabled
    compiler.assembler.enable_peephole_optimization = peephole_enabled

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
        if cache and not args.ct_run_main:
            fhash = cache.flags_hash(args.debug, folding_enabled, peephole_enabled, entry_mode)
            manifest = cache.load_manifest(args.source)
            if manifest and cache.check_fresh(manifest, fhash):
                cached = cache.get_cached_asm(manifest)
                if cached is not None:
                    asm_text = cached

        if asm_text is None:
            emission = compiler.compile_file(args.source, debug=args.debug, entry_mode=entry_mode)

            # Snapshot assembly text *before* ct-run-main JIT execution, which may
            # corrupt Python heap objects depending on memory layout.
            asm_text = emission.snapshot()

            if cache and not args.ct_run_main:
                if not fhash:
                    fhash = cache.flags_hash(args.debug, folding_enabled, peephole_enabled, entry_mode)
                has_ct = bool(compiler.parser.compile_time_vm._ct_executed)
                cache.save(args.source, compiler._loaded_files, fhash, asm_text, has_ct_effects=has_ct)

        if args.ct_run_main:
            try:
                compiler.run_compile_time_word("main", libs=args.libs)
            except CompileTimeError as exc:
                print(f"[error] compile-time execution of 'main' failed: {exc}")
                return 1
    except (ParseError, CompileError, CompileTimeError) as exc:
        print(f"[error] {exc}")
        return 1
    except Exception as exc:
        print(f"[error] unexpected failure: {exc}")
        return 1

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

    # --- incremental: skip linker if output newer than .o ---
    need_link = need_nasm or not args.output.exists()
    if not need_link:
        try:
            need_link = args.output.stat().st_mtime < obj_path.stat().st_mtime
        except OSError:
            need_link = True

    if artifact_kind == "obj":
        dest = args.output
        if obj_path.resolve() != dest.resolve():
            if need_link:
                shutil.copy2(obj_path, dest)
    elif artifact_kind == "static":
        if need_link:
            build_static_library(obj_path, args.output)
    else:
        if need_link:
            run_linker(
                obj_path,
                args.output,
                debug=args.debug,
                libs=args.libs,
                shared=(artifact_kind == "shared"),
            )

    print(f"[info] built {args.output}")

    if artifact_kind == "exe":
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
