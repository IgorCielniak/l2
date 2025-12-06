"""Bootstrap compiler for the L2 language.

This file now contains working scaffolding for:

* Parsing definitions, literals, and ordinary word references.
* Respecting immediate/macro words so syntax can be rewritten on the fly.
* Emitting NASM-compatible x86-64 assembly with explicit data and return stacks.
* Driving the toolchain via ``nasm`` + ``ld``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Union, Tuple


class ParseError(Exception):
	"""Raised when the source stream cannot be parsed."""


class CompileError(Exception):
	"""Raised when IR cannot be turned into assembly."""


# ---------------------------------------------------------------------------
# Tokenizer / Reader
# ---------------------------------------------------------------------------


@dataclass
class Token:
	lexeme: str
	line: int
	column: int
	start: int
	end: int

	def __repr__(self) -> str:  # pragma: no cover - debug helper
		return f"Token({self.lexeme!r}@{self.line}:{self.column})"


class Reader:
	"""Default reader; users can swap implementations at runtime."""

	def __init__(self) -> None:
		self.line = 1
		self.column = 0

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
			if char == "#":
				while index < source_len and source[index] != "\n":
					index += 1
				continue
			if char.isspace():
				if lexeme:
					yield Token(
						"".join(lexeme),
						token_line,
						token_column,
						token_start,
						index,
					)
					lexeme.clear()
				if char == "\n":
					self.line += 1
					self.column = 0
				else:
					self.column += 1
				index += 1
				continue
			if not lexeme:
				token_start = index
				token_line = self.line
				token_column = self.column
			lexeme.append(char)
			self.column += 1
			index += 1
		if lexeme:
			yield Token("".join(lexeme), token_line, token_column, token_start, index)


# ---------------------------------------------------------------------------
# Dictionary / Words
# ---------------------------------------------------------------------------


class ASTNode:
	"""Base class for all AST nodes."""


@dataclass
class WordRef(ASTNode):
	name: str


@dataclass
class Literal(ASTNode):
	value: int


@dataclass
class Definition(ASTNode):
	name: str
	body: List[ASTNode]
	immediate: bool = False


@dataclass
class AsmDefinition(ASTNode):
	name: str
	body: str
	immediate: bool = False


@dataclass
class Module(ASTNode):
	forms: List[ASTNode]


@dataclass
class MacroDefinition:
	name: str
	tokens: List[str]
	param_count: int = 0


@dataclass
class StructField:
	name: str
	offset: int
	size: int


@dataclass
class BranchZero(ASTNode):
	target: str


@dataclass
class Jump(ASTNode):
	target: str


@dataclass
class Label(ASTNode):
	name: str


@dataclass
class ForBegin(ASTNode):
	loop_label: str
	end_label: str


@dataclass
class ForNext(ASTNode):
	loop_label: str
	end_label: str


MacroHandler = Callable[["Parser"], Optional[List[ASTNode]]]
IntrinsicEmitter = Callable[["FunctionEmitter"], None]


@dataclass
class Word:
	name: str
	immediate: bool = False
	stack_effect: str = "( -- )"
	definition: Optional[Union[Definition, AsmDefinition]] = None
	macro: Optional[MacroHandler] = None
	intrinsic: Optional[IntrinsicEmitter] = None
	macro_expansion: Optional[List[str]] = None
	macro_params: int = 0


@dataclass
class Dictionary:
	words: Dict[str, Word] = field(default_factory=dict)

	def register(self, word: Word) -> None:
		if word.name in self.words:
			sys.stderr.write(f"[warn] redefining word {word.name}\n")
		self.words[word.name] = word

	def lookup(self, name: str) -> Optional[Word]:
		return self.words.get(name)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


Context = Union[Module, Definition]


class Parser:
	def __init__(self, dictionary: Dictionary) -> None:
		self.dictionary = dictionary
		self.tokens: List[Token] = []
		self.pos = 0
		self.context_stack: List[Context] = []
		self.definition_stack: List[Word] = []
		self.last_defined: Optional[Word] = None
		self.source: str = ""
		self.macro_recording: Optional[MacroDefinition] = None
		self.control_stack: List[Dict[str, str]] = []
		self.label_counter = 0

	# Public helpers for macros ------------------------------------------------
	def next_token(self) -> Token:
		return self._consume()

	def peek_token(self) -> Optional[Token]:
		return None if self._eof() else self.tokens[self.pos]

	def emit_node(self, node: ASTNode) -> None:
		self._append_node(node)

	def most_recent_definition(self) -> Optional[Word]:
		return self.last_defined

	# Parsing ------------------------------------------------------------------
	def parse(self, tokens: Iterable[Token], source: str) -> Module:
		self.tokens = list(tokens)
		self.source = source
		self.pos = 0
		self.context_stack = [Module(forms=[])]
		self.definition_stack.clear()
		self.last_defined = None
		self.control_stack = []
		self.label_counter = 0

		while not self._eof():
			token = self._consume()
			if self._handle_macro_recording(token):
				continue
			lexeme = token.lexeme
			if lexeme == ":":
				self._begin_definition(token)
				continue
			if lexeme == ";":
				self._end_definition(token)
				continue
			if lexeme == ":asm":
				self._parse_asm_definition(token)
				continue
			if lexeme == "if":
				self._handle_if_control()
				continue
			if lexeme == "else":
				self._handle_else_control()
				continue
			if lexeme == "then":
				self._handle_then_control()
				continue
			if lexeme == "for":
				self._handle_for_control()
				continue
			if lexeme == "next":
				self._handle_next_control()
				continue
			if self._maybe_expand_macro(token):
				continue
			self._handle_token(token)

		if len(self.context_stack) != 1:
			raise ParseError("unclosed definition at EOF")
		if self.control_stack:
			raise ParseError("unclosed control structure at EOF")

		module = self.context_stack.pop()
		if not isinstance(module, Module):  # pragma: no cover - defensive
			raise ParseError("internal parser state corrupt")
		return module

	# Internal helpers ---------------------------------------------------------
	def _handle_token(self, token: Token) -> None:
		if self._try_literal(token):
			return

		word = self.dictionary.lookup(token.lexeme)
		if word and word.immediate:
			if not word.macro:
				raise ParseError(f"immediate word {word.name} lacks macro handler")
			produced = word.macro(self)
			if produced:
				for node in produced:
					self._append_node(node)
			return

		self._append_node(WordRef(name=token.lexeme))

	def _handle_macro_recording(self, token: Token) -> bool:
		if self.macro_recording is None:
			return False
		if token.lexeme == ";macro":
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
				idx = int(lex[1:]) - 1
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
			raise ParseError(f"unexpected ';macro' at {token.line}:{token.column}")
		macro_def = self.macro_recording
		self.macro_recording = None
		word = Word(name=macro_def.name)
		word.macro_expansion = list(macro_def.tokens)
		word.macro_params = macro_def.param_count
		self.dictionary.register(word)

	def _push_control(self, entry: Dict[str, str]) -> None:
		self.control_stack.append(entry)

	def _pop_control(self, expected: Tuple[str, ...]) -> Dict[str, str]:
		if not self.control_stack:
			raise ParseError("control stack underflow")
		entry = self.control_stack.pop()
		if entry.get("type") not in expected:
			raise ParseError(f"mismatched control word '{entry.get('type')}'")
		return entry

	def _new_label(self, prefix: str) -> str:
		label = f"L_{prefix}_{self.label_counter}"
		self.label_counter += 1
		return label

	def _handle_if_control(self) -> None:
		false_label = self._new_label("if_false")
		self._append_node(BranchZero(target=false_label))
		self._push_control({"type": "if", "false": false_label})

	def _handle_else_control(self) -> None:
		entry = self._pop_control(("if",))
		end_label = self._new_label("if_end")
		self._append_node(Jump(target=end_label))
		self._append_node(Label(name=entry["false"]))
		self._push_control({"type": "else", "end": end_label})

	def _handle_then_control(self) -> None:
		entry = self._pop_control(("if", "else"))
		if entry["type"] == "if":
			self._append_node(Label(name=entry["false"]))
		else:
			self._append_node(Label(name=entry["end"]))

	def _handle_for_control(self) -> None:
		loop_label = self._new_label("for_loop")
		end_label = self._new_label("for_end")
		self._append_node(ForBegin(loop_label=loop_label, end_label=end_label))
		self._push_control({"type": "for", "loop": loop_label, "end": end_label})

	def _handle_next_control(self) -> None:
		entry = self._pop_control(("for",))
		self._append_node(ForNext(loop_label=entry["loop"], end_label=entry["end"]))

	def _begin_definition(self, token: Token) -> None:
		if self._eof():
			raise ParseError(f"definition name missing after ':' at {token.line}:{token.column}")
		name_token = self._consume()
		definition = Definition(name=name_token.lexeme, body=[])
		self.context_stack.append(definition)
		word = self.dictionary.lookup(definition.name)
		if word is None:
			word = Word(name=definition.name)
			self.dictionary.register(word)
		word.definition = definition
		self.definition_stack.append(word)

	def _end_definition(self, token: Token) -> None:
		if len(self.context_stack) <= 1:
			raise ParseError(f"unexpected ';' at {token.line}:{token.column}")
		ctx = self.context_stack.pop()
		if not isinstance(ctx, Definition):
			raise ParseError("';' can only close definitions")
		word = self.definition_stack.pop()
		ctx.immediate = word.immediate
		module = self.context_stack[-1]
		if not isinstance(module, Module):
			raise ParseError("nested definitions are not supported yet")
		module.forms.append(ctx)
		self.last_defined = word

	def _parse_asm_definition(self, token: Token) -> None:
		if self._eof():
			raise ParseError(f"definition name missing after ':asm' at {token.line}:{token.column}")
		name_token = self._consume()
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
		definition = AsmDefinition(name=name_token.lexeme, body=asm_body)
		word = self.dictionary.lookup(definition.name)
		if word is None:
			word = Word(name=definition.name)
			self.dictionary.register(word)
		word.definition = definition
		definition.immediate = word.immediate
		module = self.context_stack[-1]
		if not isinstance(module, Module):
			raise ParseError("asm definitions must be top-level forms")
		module.forms.append(definition)
		self.last_defined = word
		if self._eof():
			raise ParseError("asm definition missing terminator ';'")
		terminator = self._consume()
		if terminator.lexeme != ";":
			raise ParseError(f"expected ';' after asm definition at {terminator.line}:{terminator.column}")

	def _append_node(self, node: ASTNode) -> None:
		target = self.context_stack[-1]
		if isinstance(target, Module):
			target.forms.append(node)
		elif isinstance(target, Definition):
			target.body.append(node)
		else:  # pragma: no cover - defensive
			raise ParseError("unknown parse context")

	def _try_literal(self, token: Token) -> bool:
		try:
			value = int(token.lexeme, 0)
		except ValueError:
			return False
		self._append_node(Literal(value=value))
		return True

	def _consume(self) -> Token:
		if self._eof():
			raise ParseError("unexpected EOF")
		token = self.tokens[self.pos]
		self.pos += 1
		return token

	def _eof(self) -> bool:
		return self.pos >= len(self.tokens)


# ---------------------------------------------------------------------------
# NASM Emitter
# ---------------------------------------------------------------------------


@dataclass
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
		return "\n".join(parts)


class FunctionEmitter:
	"""Utility for emitting per-word assembly."""

	def __init__(self, text: List[str]) -> None:
		self.text = text

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


def sanitize_label(name: str) -> str:
	parts: List[str] = []
	for ch in name:
		if ch.isalnum() or ch == "_":
			parts.append(ch)
		else:
			parts.append(f"_{ord(ch):02x}")
	safe = "".join(parts) or "anon"
	return f"word_{safe}"


class Assembler:
	def __init__(self, dictionary: Dictionary) -> None:
		self.dictionary = dictionary
		self.stack_bytes = 65536
		self.io_buffer_bytes = 128

	def emit(self, module: Module) -> Emission:
		emission = Emission()
		emission.text.extend(self._runtime_prelude())

		valid_defs = (Definition, AsmDefinition)
		definitions = [form for form in module.forms if isinstance(form, valid_defs)]
		stray_forms = [form for form in module.forms if not isinstance(form, valid_defs)]
		if stray_forms:
			raise CompileError("top-level literals or word references are not supported yet")

		if not any(defn.name == "main" for defn in definitions):
			raise CompileError("missing 'main' definition")

		for definition in definitions:
			self._emit_definition(definition, emission.text)

		emission.bss.extend(self._bss_layout())
		return emission

	def _emit_definition(self, definition: Union[Definition, AsmDefinition], text: List[str]) -> None:
		label = sanitize_label(definition.name)
		text.append(f"{label}:")
		builder = FunctionEmitter(text)
		if isinstance(definition, Definition):
			for node in definition.body:
				self._emit_node(node, builder)
		elif isinstance(definition, AsmDefinition):
			self._emit_asm_body(definition, builder)
		else:  # pragma: no cover - defensive
			raise CompileError("unknown definition type")
		builder.emit("    ret")

	def _emit_asm_body(self, definition: AsmDefinition, builder: FunctionEmitter) -> None:
		body = definition.body.strip("\n")
		if not body:
			return
		for line in body.splitlines():
			if line.strip():
				builder.emit(line)
			else:
				builder.emit("")

	def _emit_node(self, node: ASTNode, builder: FunctionEmitter) -> None:
		if isinstance(node, Literal):
			builder.push_literal(node.value)
			return
		if isinstance(node, WordRef):
			self._emit_wordref(node, builder)
			return
		if isinstance(node, BranchZero):
			self._emit_branch_zero(node, builder)
			return
		if isinstance(node, Jump):
			builder.emit(f"    jmp {node.target}")
			return
		if isinstance(node, Label):
			builder.emit(f"{node.name}:")
			return
		if isinstance(node, ForBegin):
			self._emit_for_begin(node, builder)
			return
		if isinstance(node, ForNext):
			self._emit_for_next(node, builder)
			return
		raise CompileError(f"unsupported AST node {node!r}")

	def _emit_wordref(self, ref: WordRef, builder: FunctionEmitter) -> None:
		word = self.dictionary.lookup(ref.name)
		if word is None:
			raise CompileError(f"unknown word '{ref.name}'")
		if word.intrinsic:
			word.intrinsic(builder)
			return
		builder.emit(f"    call {sanitize_label(ref.name)}")

	def _emit_branch_zero(self, node: BranchZero, builder: FunctionEmitter) -> None:
		builder.pop_to("rax")
		builder.emit("    test rax, rax")
		builder.emit(f"    jz {node.target}")

	def _emit_for_begin(self, node: ForBegin, builder: FunctionEmitter) -> None:
		builder.pop_to("rax")
		builder.emit("    cmp rax, 0")
		builder.emit(f"    jle {node.end_label}")
		builder.emit("    sub r13, 8")
		builder.emit("    mov [r13], rax")
		builder.emit(f"{node.loop_label}:")

	def _emit_for_next(self, node: ForNext, builder: FunctionEmitter) -> None:
		builder.emit("    mov rax, [r13]")
		builder.emit("    dec rax")
		builder.emit("    mov [r13], rax")
		builder.emit(f"    jg {node.loop_label}")
		builder.emit("    add r13, 8")
		builder.emit(f"{node.end_label}:")

	def _runtime_prelude(self) -> List[str]:
		return [
			"%define DSTK_BYTES 65536",
			"%define RSTK_BYTES 65536",
			"%define PRINT_BUF_BYTES 128",
			"global _start",
			"_start:",
			"    ; initialize data/return stack pointers",
			"    lea r12, [rel dstack_top]",
			"    mov r15, r12",
			"    lea r13, [rel rstack_top]",
			"    call word_main",
			"    mov rax, 0",
			"    cmp r12, r15",
			"    je .no_exit_value",
			"    mov rax, [r12]",
			"    add r12, 8",
			".no_exit_value:",
			"    mov rdi, rax",
			"    mov rax, 60",
			"    syscall",
		]

	def _bss_layout(self) -> List[str]:
		return [
			"align 16",
			"dstack: resb DSTK_BYTES",
			"dstack_top:",
			"align 16",
			"rstack: resb RSTK_BYTES",
			"rstack_top:",
			"align 16",
			"print_buf: resb PRINT_BUF_BYTES",
			"print_buf_end:",
		]

	def write_asm(self, emission: Emission, path: Path) -> None:
		path.write_text(emission.snapshot())


# ---------------------------------------------------------------------------
# Built-in macros and intrinsics
# ---------------------------------------------------------------------------


def macro_immediate(parser: Parser) -> Optional[List[ASTNode]]:
	word = parser.most_recent_definition()
	if word is None:
		raise ParseError("'immediate' must follow a definition")
	word.immediate = True
	if word.definition is not None:
		word.definition.immediate = True
	return None


def macro_begin_text_macro(parser: Parser) -> Optional[List[ASTNode]]:
	if parser._eof():
		raise ParseError("macro name missing after 'macro:'")
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


def macro_end_text_macro(parser: Parser) -> Optional[List[ASTNode]]:
	if parser.macro_recording is None:
		raise ParseError("';macro' without matching 'macro:'")
	# Actual closing handled in parser loop when ';macro' token is seen.
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

	tokens.append(make_token(":"))
	tokens.append(make_token(name))
	for lexeme in body:
		tokens.append(make_token(lexeme))
	tokens.append(make_token(";"))


def macro_struct_begin(parser: Parser) -> Optional[List[ASTNode]]:
	if parser._eof():
		raise ParseError("struct name missing after 'struct:'")
	name_token = parser.next_token()
	struct_name = name_token.lexeme
	fields: List[StructField] = []
	current_offset = 0
	while True:
		if parser._eof():
			raise ParseError("unterminated struct definition (missing ';struct')")
		token = parser.next_token()
		if token.lexeme == ";struct":
			break
		if token.lexeme != "field":
			raise ParseError(f"expected 'field' or ';struct' in struct '{struct_name}' definition")
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
			[offset_word, "+", "!"],
		)

	parser.tokens[parser.pos:parser.pos] = generated
	return None


def macro_struct_end(parser: Parser) -> Optional[List[ASTNode]]:
	raise ParseError("';struct' must follow a 'struct:' block")


def bootstrap_dictionary() -> Dictionary:
	dictionary = Dictionary()
	dictionary.register(Word(name="immediate", immediate=True, macro=macro_immediate))
	dictionary.register(Word(name="macro:", immediate=True, macro=macro_begin_text_macro))
	dictionary.register(Word(name=";macro", immediate=True, macro=macro_end_text_macro))
	dictionary.register(Word(name="struct:", immediate=True, macro=macro_struct_begin))
	dictionary.register(Word(name=";struct", immediate=True, macro=macro_struct_end))
	return dictionary


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Compiler:
	def __init__(self) -> None:
		self.reader = Reader()
		self.dictionary = bootstrap_dictionary()
		self.parser = Parser(self.dictionary)
		self.assembler = Assembler(self.dictionary)

	def compile_source(self, source: str) -> Emission:
		tokens = list(self.reader.tokenize(source))
		module = self.parser.parse(tokens, source)
		return self.assembler.emit(module)

	def compile_file(self, path: Path) -> Emission:
		source = self._load_with_imports(path.resolve())
		return self.compile_source(source)

	def _load_with_imports(self, path: Path, seen: Optional[Set[Path]] = None) -> str:
		if seen is None:
			seen = set()
		path = path.resolve()
		if path in seen:
			return ""
		seen.add(path)
		try:
			contents = path.read_text()
		except FileNotFoundError as exc:
			raise ParseError(f"cannot import {path}: {exc}") from exc
		lines: List[str] = []
		for idx, line in enumerate(contents.splitlines()):
			stripped = line.strip()
			if stripped.startswith("import "):
				target = stripped.split(None, 1)[1].strip()
				if not target:
					raise ParseError(f"empty import target in {path}:{idx + 1}")
				target_path = (path.parent / target).resolve()
				lines.append(self._load_with_imports(target_path, seen))
				continue
			lines.append(line)
		return "\n".join(lines) + "\n"


def run_nasm(asm_path: Path, obj_path: Path) -> None:
	subprocess.run(["nasm", "-f", "elf64", "-o", str(obj_path), str(asm_path)], check=True)


def run_linker(obj_path: Path, exe_path: Path) -> None:
	subprocess.run(["ld", "-o", str(exe_path), str(obj_path)], check=True)


def cli(argv: Sequence[str]) -> int:
	parser = argparse.ArgumentParser(description="L2 compiler driver")
	parser.add_argument("source", type=Path, help="input .sl file")
	parser.add_argument("-o", dest="output", type=Path, default=Path("a.out"))
	parser.add_argument("--emit-asm", action="store_true", help="stop after generating asm")
	parser.add_argument("--temp-dir", type=Path, default=Path("build"))
	args = parser.parse_args(argv)

	compiler = Compiler()
	emission = compiler.compile_file(args.source)

	args.temp_dir.mkdir(parents=True, exist_ok=True)
	asm_path = args.temp_dir / (args.source.stem + ".asm")
	obj_path = args.temp_dir / (args.source.stem + ".o")
	compiler.assembler.write_asm(emission, asm_path)

	if args.emit_asm:
		print(f"[info] wrote {asm_path}")
		return 0

	run_nasm(asm_path, obj_path)
	run_linker(obj_path, args.output)
	print(f"[info] built {args.output}")
	return 0


def main() -> None:
	sys.exit(cli(sys.argv[1:]))


if __name__ == "__main__":
	main()
