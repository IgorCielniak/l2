"""Bootstrap compiler for the L2 language.

This file now contains working scaffolding for:

* Parsing definitions, literals, and ordinary word references.
* Respecting immediate/macro words so syntax can be rewritten on the fly.
* Emitting NASM-compatible x86-64 assembly with explicit data and return stacks.
* Driving the toolchain via ``nasm`` + ``ld``.
"""

from __future__ import annotations

import argparse
import ctypes
import mmap
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
		self.custom_tokens: Set[str] = {"(", ")", "{", "}", ";", ",", "[", "]"}
		self._token_order: List[str] = sorted(self.custom_tokens, key=len, reverse=True)

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
			for tok in self._token_order:
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


class ASTNode:
	"""Base class for all AST nodes."""


@dataclass
class WordRef(ASTNode):
	name: str


@dataclass
class Literal(ASTNode):
	value: Any


@dataclass
class Definition(ASTNode):
	name: str
	body: List[ASTNode]
	immediate: bool = False
	compile_only: bool = False


@dataclass
class AsmDefinition(ASTNode):
	name: str
	body: str
	immediate: bool = False
	compile_only: bool = False


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
		self._parser.emit_node(Literal(value=value))

	def emit_word(self, name: str) -> None:
		self._parser.emit_node(WordRef(name=name))

	def emit_node(self, node: ASTNode) -> None:
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


MacroHandler = Callable[[MacroContext], Optional[List[ASTNode]]]
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
	compile_time_intrinsic: Optional[Callable[["CompileTimeVM"], None]] = None
	compile_only: bool = False
	compile_time_override: bool = False
	is_extern: bool = False  # New: mark as extern
	extern_inputs: int = 0
	extern_outputs: int = 0
	extern_signature: Optional[Tuple[List[str], str]] = None  # (arg_types, ret_type)


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
	def __init__(self, dictionary: Dictionary, reader: Optional[Reader] = None) -> None:
		self.dictionary = dictionary
		self.reader = reader or Reader()
		self.tokens: List[Token] = []
		self._token_iter: Optional[Iterable[Token]] = None
		self._token_iter_exhausted = True
		self.pos = 0
		self.context_stack: List[Context] = []
		self.definition_stack: List[Word] = []
		self.last_defined: Optional[Word] = None
		self.source: str = ""
		self.macro_recording: Optional[MacroDefinition] = None
		self.control_stack: List[Dict[str, str]] = []
		self.label_counter = 0
		self.token_hook: Optional[str] = None
		self._last_token: Optional[Token] = None
		self.compile_time_vm = CompileTimeVM(self)

	def inject_token_objects(self, tokens: Sequence[Token]) -> None:
		"""Insert tokens at the current parse position."""
		self.tokens[self.pos:self.pos] = list(tokens)

	# Public helpers for macros ------------------------------------------------
	def next_token(self) -> Token:
		return self._consume()

	def peek_token(self) -> Optional[Token]:
		self._ensure_tokens(self.pos)
		return None if self._eof() else self.tokens[self.pos]

	def emit_node(self, node: ASTNode) -> None:
		self._append_node(node)

	def most_recent_definition(self) -> Optional[Word]:
		return self.last_defined

	# Parsing ------------------------------------------------------------------
	def parse(self, tokens: Iterable[Token], source: str) -> Module:
		self.tokens = []
		self._token_iter = iter(tokens)
		self._token_iter_exhausted = False
		self.source = source
		self.pos = 0
		self.context_stack = [Module(forms=[])]
		self.definition_stack.clear()
		self.last_defined = None
		self.control_stack = []
		self.label_counter = 0
		self.token_hook = None
		self._last_token = None

		while not self._eof():
			token = self._consume()
			self._last_token = token
			if self._run_token_hook(token):
				continue
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
			if lexeme == ":py":
				self._parse_py_definition(token)
				continue
			if lexeme == "extern":
				self._parse_extern(token)
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

	def _parse_extern(self, token: Token) -> None:
		# extern <name> [inputs outputs]
		# OR
		# extern <ret_type> <name>(<args>)

		if self._eof():
			raise ParseError(f"extern missing name at {token.line}:{token.column}")

		# Heuristic: check if the first token is a likely C type
		c_types = {"void", "int", "long", "char", "bool", "size_t", "float", "double"}
		
		# Peek at tokens to decide mode
		t1 = self._consume()
		is_c_decl = False
		
		# If t1 is a type (or type*), and next is name, and next is '(', it's C-style.
		# But since we already consumed t1, we have to proceed carefully.
		
		# Check if t1 looks like a type
		base_type = t1.lexeme.rstrip("*")
		if base_type in c_types:
			# Likely C-style, but let's confirm next token is not a number (which would mean t1 was the name in raw mode)
			peek = self.peek_token()
			if peek is not None and peek.lexeme != "(" and not peek.lexeme.isdigit():
				# t1=type, peek=name. Confirm peek2='('?
				# Actually, if t1 is "int", it's extremely unlikely to be a function name in L2.
				is_c_decl = True

		if is_c_decl:
			# C-Style Parsing
			ret_type = t1.lexeme
			if self._eof():
				raise ParseError("extern missing name after return type")
			name_token = self._consume()
			name = name_token.lexeme
			
			# Handle pointers in name token if tokenizer didn't split them (e.g. *name)
			while name.startswith("*"):
				name = name[1:]
			
			if self._eof() or self._consume().lexeme != "(":
				raise ParseError(f"expected '(' after extern function name '{name}'")
			
			inputs = 0
			arg_types: List[str] = []
			while True:
				if self._eof():
					raise ParseError("extern unclosed '('")
				peek = self.peek_token()
				if peek.lexeme == ")":
					self._consume()
					break
				
				# Parse argument type
				arg_type_tok = self._consume()
				arg_type = arg_type_tok.lexeme
				
				# Handle "type *" sequence
				peek_ptr = self.peek_token()
				while peek_ptr and peek_ptr.lexeme == "*":
					self._consume()
					arg_type += "*"
					peek_ptr = self.peek_token()

				if arg_type != "void":
					inputs += 1
					arg_types.append(arg_type)
					# Optional argument name
					peek_name = self.peek_token()
					if peek_name and peek_name.lexeme not in (",", ")"):
						self._consume() # Consume arg name

				peek_sep = self.peek_token()
				if peek_sep and peek_sep.lexeme == ",":
					self._consume()
			
			outputs = 0 if ret_type == "void" else 1
			
			word = self.dictionary.lookup(name)
			if word is None:
				word = Word(name=name)
				self.dictionary.register(word)
			word.is_extern = True
			word.extern_inputs = inputs
			word.extern_outputs = outputs
			word.extern_signature = (arg_types, ret_type)
			
		else:
			# Raw/Legacy Parsing
			name = t1.lexeme
			word = self.dictionary.lookup(name)
			if word is None:
				word = Word(name=name)
				self.dictionary.register(word)
			word.is_extern = True

			# Check for optional inputs/outputs
			peek = self.peek_token()
			if peek is not None and peek.lexeme.isdigit():
				word.extern_inputs = int(self._consume().lexeme)
				peek = self.peek_token()
				if peek is not None and peek.lexeme.isdigit():
					word.extern_outputs = int(self._consume().lexeme)
			else:
				word.extern_inputs = 0
				word.extern_outputs = 0

	def _handle_token(self, token: Token) -> None:
		if self._try_literal(token):
			return

		word = self.dictionary.lookup(token.lexeme)
		if word and word.immediate:
			if word.macro:
				produced = word.macro(MacroContext(self))
				if produced:
					for node in produced:
						self._append_node(node)
			else:
				self._execute_immediate_word(word)
			return

		self._append_node(WordRef(name=token.lexeme))

	def _execute_immediate_word(self, word: Word) -> None:
		try:
			self.compile_time_vm.invoke(word)
		except ParseError:
			raise
		except Exception as exc:  # pragma: no cover - defensive
			raise ParseError(f"compile-time word '{word.name}' failed: {exc}") from exc

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
		ctx.compile_only = word.compile_only
		if word.compile_only or word.immediate:
			word.compile_time_override = True
			word.compile_time_intrinsic = None
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
		definition.compile_only = word.compile_only
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
		word = self.dictionary.lookup(name_token.lexeme)
		if word is None:
			word = Word(name=name_token.lexeme)
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
		self.dictionary.register(word)
		if self._eof():
			raise ParseError("py definition missing terminator ';'")
		terminator = self._consume()
		if terminator.lexeme != ";":
			raise ParseError(f"expected ';' after py definition at {terminator.line}:{terminator.column}")

	def _py_exec_namespace(self) -> Dict[str, Any]:
		return dict(PY_EXEC_GLOBALS)

	def _append_node(self, node: ASTNode) -> None:
		target = self.context_stack[-1]
		if isinstance(target, Module):
			target.forms.append(node)
		elif isinstance(target, Definition):
			target.body.append(node)
		else:  # pragma: no cover - defensive
			raise ParseError("unknown parse context")

	def _try_literal(self, token: Token) -> None:
		try:
			value = int(token.lexeme, 0)
			self._append_node(Literal(value=value))
			return True
		except ValueError:
			pass

		# Try float
		try:
			if "." in token.lexeme or "e" in token.lexeme.lower():
				value = float(token.lexeme)
				self._append_node(Literal(value=value))
				return True
		except ValueError:
			pass

		string_value = _parse_string_literal(token)
		if string_value is not None:
			self._append_node(Literal(value=string_value))
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


class CompileTimeVM:
	def __init__(self, parser: Parser) -> None:
		self.parser = parser
		self.dictionary = parser.dictionary
		self.stack: List[Any] = []
		self.return_stack: List[Any] = []
		self.loop_stack: List[Dict[str, Any]] = []
		self._handles = _CTHandleTable()

	def reset(self) -> None:
		self.stack.clear()
		self.return_stack.clear()
		self.loop_stack.clear()
		self._handles.clear()

	def push(self, value: Any) -> None:
		self.stack.append(value)

	def pop(self) -> Any:
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
		if not self.stack:
			raise ParseError("compile-time stack underflow")
		return self.stack[-1]

	def pop_int(self) -> int:
		value = self.pop()
		if not isinstance(value, int):
			raise ParseError("expected integer on compile-time stack")
		return value

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

	def invoke(self, word: Word) -> None:
		self.reset()
		self._call_word(word)

	def invoke_with_args(self, word: Word, args: Sequence[Any]) -> None:
		self.reset()
		for value in args:
			self.push(value)
		self._call_word(word)

	def _call_word(self, word: Word) -> None:
		definition = word.definition
		prefer_definition = word.compile_time_override or (isinstance(definition, Definition) and (word.immediate or word.compile_only))
		if not prefer_definition and word.compile_time_intrinsic is not None:
			word.compile_time_intrinsic(self)
			return
		if definition is None:
			raise ParseError(f"word '{word.name}' has no compile-time definition")
		if isinstance(definition, AsmDefinition):
			self._run_asm_definition(word)
			return
		self._execute_nodes(definition.body)

	def _run_asm_definition(self, word: Word) -> None:
		definition = word.definition
		if Ks is None:
			raise ParseError("keystone is required for compile-time :asm execution; install keystone-engine")
		if not isinstance(definition, AsmDefinition):  # pragma: no cover - defensive
			raise ParseError(f"word '{word.name}' has no asm body")
		asm_body = definition.body.strip("\n")

		# Determine whether this asm uses string semantics (len,addr pairs)
		# by scanning the asm body for string-related labels. This avoids
		# hardcoding a specific word name (like 'puts') and lets any word
		# that expects (len, addr) work the same way.
		string_mode = False
		if asm_body:
			lowered = asm_body.lower()
			if any(k in lowered for k in ("data_start", "data_end", "print_buf", "print_buf_end")):
				string_mode = True

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
			for line in asm_body.splitlines():
				line = line.strip()
				if line == "ret":
					line = "jmp _ct_save"
				if "lea r8, [rel data_start]" in line:
					line = line.replace("lea r8, [rel data_start]", f"mov r8, {data_start}")
				if "lea r9, [rel data_end]" in line:
					line = line.replace("lea r9, [rel data_end]", f"mov r9, {data_end}")
				if "mov byte [rel print_buf]" in line or "mov byte ptr [rel print_buf]" in line:
					patched_body.append(f"mov rax, {print_buf}")
					patched_body.append("mov byte ptr [rax], 10")
					continue
				if "lea rsi, [rel print_buf_end]" in line:
					line = f"mov rsi, {print_buf + PRINT_BUF_BYTES}"
				if "lea rsi, [rel print_buf]" in line:
					line = f"mov rsi, {print_buf}"
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

	def _execute_nodes(self, nodes: Sequence[ASTNode]) -> None:
		label_positions = self._label_positions(nodes)
		loop_pairs = self._for_pairs(nodes)
		begin_pairs = self._begin_pairs(nodes)
		self.loop_stack = []
		begin_stack: List[Dict[str, int]] = []
		ip = 0
		while ip < len(nodes):
			node = nodes[ip]
			if isinstance(node, Literal):
				self.push(node.value)
				ip += 1
				continue
			if isinstance(node, WordRef):
				name = node.name
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
				self._call_word_by_name(name)
				ip += 1
				continue
			if isinstance(node, BranchZero):
				condition = self.pop()
				flag: bool
				if isinstance(condition, bool):
					flag = condition
				elif isinstance(condition, int):
					flag = condition != 0
				else:
					raise ParseError("branch expects integer or boolean condition")
				if not flag:
					ip = self._jump_to_label(label_positions, node.target)
				else:
					ip += 1
				continue
			if isinstance(node, Jump):
				ip = self._jump_to_label(label_positions, node.target)
				continue
			if isinstance(node, Label):
				ip += 1
				continue
			if isinstance(node, ForBegin):
				count = self.pop_int()
				if count <= 0:
					match = loop_pairs.get(ip)
					if match is None:
						raise ParseError("internal loop bookkeeping error")
					ip = match + 1
					continue
				self.loop_stack.append({"remaining": count, "begin": ip, "initial": count})
				ip += 1
				continue
			if isinstance(node, ForNext):
				if not self.loop_stack:
					raise ParseError("'next' without matching 'for'")
				frame = self.loop_stack[-1]
				frame["remaining"] -= 1
				if frame["remaining"] > 0:
					ip = frame["begin"] + 1
					continue
				self.loop_stack.pop()
				ip += 1
				continue
			raise ParseError(f"unsupported compile-time AST node {node!r}")

	def _label_positions(self, nodes: Sequence[ASTNode]) -> Dict[str, int]:
		positions: Dict[str, int] = {}
		for idx, node in enumerate(nodes):
			if isinstance(node, Label):
				positions[node.name] = idx
		return positions

	def _for_pairs(self, nodes: Sequence[ASTNode]) -> Dict[int, int]:
		stack: List[int] = []
		pairs: Dict[int, int] = {}
		for idx, node in enumerate(nodes):
			if isinstance(node, ForBegin):
				stack.append(idx)
			elif isinstance(node, ForNext):
				if not stack:
					raise ParseError("'next' without matching 'for'")
				begin_idx = stack.pop()
				pairs[begin_idx] = idx
				pairs[idx] = begin_idx
		if stack:
			raise ParseError("'for' without matching 'next'")
		return pairs

	def _begin_pairs(self, nodes: Sequence[ASTNode]) -> Dict[int, int]:
		stack: List[int] = []
		pairs: Dict[int, int] = {}
		for idx, node in enumerate(nodes):
			if isinstance(node, WordRef) and node.name == "begin":
				stack.append(idx)
			elif isinstance(node, WordRef) and node.name == "again":
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


def sanitize_label(name: str) -> str:
	parts: List[str] = []
	for ch in name:
		if ch.isalnum() or ch == "_":
			parts.append(ch)
		else:
			parts.append(f"_{ord(ch):02x}")
	safe = "".join(parts) or "anon"
	return f"word_{safe}"


def _is_identifier(text: str) -> bool:
	if not text:
		return False
	first = text[0]
	if not (first.isalpha() or first == "_"):
		return False
	return all(ch.isalnum() or ch == "_" for ch in text)


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
	def __init__(self, dictionary: Dictionary) -> None:
		self.dictionary = dictionary
		self.stack_bytes = 65536
		self.io_buffer_bytes = 128
		self._string_literals: Dict[str, Tuple[str, int]] = {}
		self._float_literals: Dict[float, str] = {}
		self._data_section: Optional[List[str]] = None

	def _emit_externs(self, text: List[str]) -> None:
		externs = sorted([w.name for w in self.dictionary.words.values() if getattr(w, "is_extern", False)])
		for name in externs:
			text.append(f"extern {name}")

	def emit(self, module: Module) -> Emission:
		emission = Emission()
		self._emit_externs(emission.text)
		emission.text.extend(self._runtime_prelude())
		self._string_literals = {}
		self._float_literals = {}
		self._data_section = emission.data

		valid_defs = (Definition, AsmDefinition)
		definitions = [form for form in module.forms if isinstance(form, valid_defs)]
		stray_forms = [form for form in module.forms if not isinstance(form, valid_defs)]
		if stray_forms:
			raise CompileError("top-level literals or word references are not supported yet")

		runtime_defs = [
			defn for defn in definitions if not getattr(defn, "compile_only", False)
		]
		if not any(defn.name == "main" for defn in runtime_defs):
			raise CompileError("missing 'main' definition")

		for definition in runtime_defs:
			self._emit_definition(definition, emission.text)

		if self._data_section is not None:
			if not self._data_section:
				self._data_section.append("data_start:")
			if not self._data_section or self._data_section[-1] != "data_end:":
				self._data_section.append("data_end:")
		emission.bss.extend(self._bss_layout())
		self._data_section = None
		return emission

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
			if isinstance(node.value, int):
				builder.push_literal(node.value)
				return
			if isinstance(node.value, float):
				label = self._intern_float_literal(node.value)
				builder.push_float(label)
				return
			if isinstance(node.value, str):
				label, length = self._intern_string_literal(node.value)
				builder.push_label(label)
				builder.push_literal(length)
				return
			raise CompileError(f"unsupported literal type {type(node.value)!r}")
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
		if word.compile_only:
			raise CompileError(f"word '{ref.name}' is compile-time only")
		if word.intrinsic:
			word.intrinsic(builder)
			return
		if getattr(word, "is_extern", False):
			inputs = getattr(word, "extern_inputs", 0)
			outputs = getattr(word, "extern_outputs", 0)
			signature = getattr(word, "extern_signature", None)

			if inputs > 0 or outputs > 0:
				regs = ["rdi", "rsi", "rdx", "rcx", "r8", "r9"]
				xmm_regs = [f"xmm{i}" for i in range(8)]

				arg_types = signature[0] if signature else []
				ret_type = signature[1] if signature else None

				if len(arg_types) != inputs and signature:
					raise CompileError(f"extern '{ref.name}' mismatch: {inputs} inputs vs {len(arg_types)} types")

				int_idx = 0
				xmm_idx = 0

				mapping: List[Tuple[str, str]] = [] # (type, target_reg)

				if not arg_types:
					# Legacy/Raw mode: assume all ints
					if inputs > 6:
						raise CompileError(f"extern '{ref.name}' has too many inputs ({inputs} > 6)")
					for i in range(inputs):
						mapping.append(("int", regs[i]))
				else:
					for type_name in arg_types:
						if type_name in ("float", "double"):
							if xmm_idx >= 8:
								raise CompileError(f"extern '{ref.name}' has too many float inputs")
							mapping.append(("float", xmm_regs[xmm_idx]))
							xmm_idx += 1
						else:
							if int_idx >= 6:
								raise CompileError(f"extern '{ref.name}' has too many int inputs")
							mapping.append(("int", regs[int_idx]))
							int_idx += 1

				for type_name, reg in reversed(mapping):
					if type_name == "float":
						builder.pop_to("rax")
						builder.emit(f"    movq {reg}, rax")
					else:
						builder.pop_to(reg)

				builder.emit("    push rbp")
				builder.emit("    mov rbp, rsp")
				builder.emit("    and rsp, -16")
				builder.emit(f"    mov al, {xmm_idx}")
				builder.emit(f"    call {ref.name}")
				builder.emit("    leave")

				# Handle Return Value
				if ret_type in ("float", "double"):
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
				builder.emit(f"    call {ref.name}")
		else:
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


def macro_immediate(ctx: MacroContext) -> Optional[List[ASTNode]]:
	parser = ctx.parser
	word = parser.most_recent_definition()
	if word is None:
		raise ParseError("'immediate' must follow a definition")
	word.immediate = True
	if word.definition is not None:
		word.definition.immediate = True
	return None


def macro_compile_only(ctx: MacroContext) -> Optional[List[ASTNode]]:
	parser = ctx.parser
	word = parser.most_recent_definition()
	if word is None:
		raise ParseError("'compile-only' must follow a definition")
	word.compile_only = True
	if word.definition is not None:
		word.definition.compile_only = True
	return None


def macro_compile_time(ctx: MacroContext) -> Optional[List[ASTNode]]:
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
	if isinstance(parser.context_stack[-1], Definition):
		parser.emit_node(WordRef(name=name))
	return None


def macro_begin_text_macro(ctx: MacroContext) -> Optional[List[ASTNode]]:
	parser = ctx.parser
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


def macro_end_text_macro(ctx: MacroContext) -> Optional[List[ASTNode]]:
	parser = ctx.parser
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


def _ct_drop(vm: CompileTimeVM) -> None:
	if not vm.stack:
		return
	vm.pop()


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

	register("lexer-new", _ct_lexer_new, compile_only=True)
	register("lexer-pop", _ct_lexer_pop, compile_only=True)
	register("lexer-peek", _ct_lexer_peek, compile_only=True)
	register("lexer-expect", _ct_lexer_expect, compile_only=True)
	register("lexer-collect-brace", _ct_lexer_collect_brace, compile_only=True)
	register("lexer-push-back", _ct_lexer_push_back, compile_only=True)




PY_EXEC_GLOBALS: Dict[str, Any] = {
	"MacroContext": MacroContext,
	"Token": Token,
	"Literal": Literal,
	"WordRef": WordRef,
	"BranchZero": BranchZero,
	"Jump": Jump,
	"Label": Label,
	"ForBegin": ForBegin,
	"ForNext": ForNext,
	"StructField": StructField,
	"Definition": Definition,
	"Module": Module,
	"ParseError": ParseError,
	"emit_definition": _struct_emit_definition,
	"is_identifier": _is_identifier,
}


def macro_struct_begin(ctx: MacroContext) -> Optional[List[ASTNode]]:
	parser = ctx.parser
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


def macro_struct_end(ctx: MacroContext) -> Optional[List[ASTNode]]:
	raise ParseError("';struct' must follow a 'struct:' block")


def bootstrap_dictionary() -> Dictionary:
	dictionary = Dictionary()
	dictionary.register(Word(name="immediate", immediate=True, macro=macro_immediate))
	dictionary.register(Word(name="compile-only", immediate=True, macro=macro_compile_only))
	dictionary.register(Word(name="compile-time", immediate=True, macro=macro_compile_time))
	dictionary.register(Word(name="macro:", immediate=True, macro=macro_begin_text_macro))
	dictionary.register(Word(name=";macro", immediate=True, macro=macro_end_text_macro))
	dictionary.register(Word(name="struct:", immediate=True, macro=macro_struct_begin))
	dictionary.register(Word(name=";struct", immediate=True, macro=macro_struct_end))
	_register_compile_time_primitives(dictionary)
	return dictionary


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class Compiler:
	def __init__(self) -> None:
		self.reader = Reader()
		self.dictionary = bootstrap_dictionary()
		self.parser = Parser(self.dictionary, self.reader)
		self.assembler = Assembler(self.dictionary)

	def compile_source(self, source: str) -> Emission:
		tokens = self.reader.tokenize(source)
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
		in_py_block = False
		for idx, line in enumerate(contents.splitlines()):
			stripped = line.strip()
			# Detect :py block start/end
			if stripped.startswith(":py") and "{" in stripped:
				in_py_block = True
			if in_py_block and "}" in stripped:
				in_py_block = False
			# Only process import as file import if not in :py block
			if not in_py_block and stripped.startswith("import "):
				target = stripped.split(None, 1)[1].strip()
				if not target:
					raise ParseError(f"empty import target in {path}:{idx + 1}")
				target_path = (path.parent / target).resolve()
				lines.append(self._load_with_imports(target_path, seen))
				continue
			lines.append(line)
		return "\n".join(lines) + "\n"


def run_nasm(asm_path: Path, obj_path: Path, debug: bool = False) -> None:
	cmd = ["nasm", "-f", "elf64"]
	if debug:
		cmd.append("-g")
	cmd += ["-o", str(obj_path), str(asm_path)]
	subprocess.run(cmd, check=True)


def run_linker(obj_path: Path, exe_path: Path, debug: bool = False, libs: Optional[List[str]] = None) -> None:
	cmd = ["ld", "-o", str(exe_path), str(obj_path)]
	if libs:
		cmd.extend(["-dynamic-linker", "/lib64/ld-linux-x86-64.so.2"])
		for lib in libs:
			# If the user passed a full .so name, use -l:libname.so, else -l<name>
			if lib.endswith('.so') or '.so.' in lib:
				cmd.append(f"-l:{lib}")
			else:
				cmd.append(f"-l{lib}")
	if debug:
		cmd.append("-g")
	subprocess.run(cmd, check=True)


def cli(argv: Sequence[str]) -> int:
	parser = argparse.ArgumentParser(description="L2 compiler driver")
	parser.add_argument("source", type=Path, nargs="?", default=None, help="input .sl file (optional when --clean is used)")
	parser.add_argument("-o", dest="output", type=Path, default=Path("a.out"))
	parser.add_argument("--emit-asm", action="store_true", help="stop after generating asm")
	parser.add_argument("--temp-dir", type=Path, default=Path("build"))
	parser.add_argument("--debug", action="store_true", help="compile with debug info")
	parser.add_argument("--run", action="store_true", help="run the built binary after successful build")
	parser.add_argument("--dbg", action="store_true", help="launch gdb on the built binary after successful build")
	parser.add_argument("--clean", action="store_true", help="remove the temp build directory and exit")
	parser.add_argument("-l", dest="libs", action="append", default=[], help="pass library to linker (e.g. -l m or -l libc.so.6)")

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

	if args.source is None:
		parser.error("the following arguments are required: source")

	compiler = Compiler()
	emission = compiler.compile_file(args.source)

	args.temp_dir.mkdir(parents=True, exist_ok=True)
	asm_path = args.temp_dir / (args.source.stem + ".asm")
	obj_path = args.temp_dir / (args.source.stem + ".o")
	compiler.assembler.write_asm(emission, asm_path)

	if args.emit_asm:
		print(f"[info] wrote {asm_path}")
		return 0

	run_nasm(asm_path, obj_path, debug=args.debug)
	run_linker(obj_path, args.output, debug=args.debug, libs=args.libs)
	print(f"[info] built {args.output}")
	exe_path = Path(args.output).resolve()
	if args.dbg:
		subprocess.run(["gdb", str(exe_path)])
	elif args.run:
		subprocess.run([str(exe_path)])
	return 0


def main() -> None:
	sys.exit(cli(sys.argv[1:]))


if __name__ == "__main__":
	main()
