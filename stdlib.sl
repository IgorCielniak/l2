:asm puts {
	mov rax, [r12]
	add r12, 8
	mov rbx, rax
	mov r8, 0
	cmp rbx, 0
	jge puts_abs
	neg rbx
	mov r8, 1
puts_abs:
	lea rsi, [rel print_buf_end]
	mov rcx, 0
	mov r10, 10
	cmp rbx, 0
	jne puts_digits
	dec rsi
	mov byte [rsi], '0'
	inc rcx
	jmp puts_sign
puts_digits:
puts_loop:
	xor rdx, rdx
	mov rax, rbx
	div r10
	add dl, '0'
	dec rsi
	mov [rsi], dl
	inc rcx
	mov rbx, rax
	test rbx, rbx
	jne puts_loop
puts_sign:
	cmp r8, 0
	je puts_finish_digits
	dec rsi
	mov byte [rsi], '-'
	inc rcx
puts_finish_digits:
	mov byte [rsi + rcx], 10
	inc rcx
	mov rax, 1
	mov rdi, 1
	mov rdx, rcx
	mov r9, rsi
	mov rsi, r9
	syscall
}
;

: extend-syntax
	enable-call-syntax
;
immediate
compile-only

:py fn {
	FN_SPLIT_CHARS = set("(),{};+-*/%,")

	def split_token(token):
		lex = token.lexeme
		parts = []
		idx = 0
		while idx < len(lex):
			char = lex[idx]
			if char in FN_SPLIT_CHARS:
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
			while idx < len(lex) and lex[idx] not in FN_SPLIT_CHARS:
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
		return [part for part in parts if part.lexeme]

	class FnLexer:
		def __init__(self, parser):
			self.parser = parser
			self.buffer = []

		def _fill(self):
			while not self.buffer:
				if self.parser._eof():
					raise ParseError("unexpected EOF inside fn definition")
				token = self.parser.next_token()
				split = split_token(token)
				if not split:
					continue
				self.buffer.extend(split)

		def peek(self):
			self._fill()
			return self.buffer[0]

		def pop(self):
			token = self.peek()
			self.buffer.pop(0)
			return token

		def expect(self, lexeme):
			token = self.pop()
			if token.lexeme != lexeme:
				raise ParseError(f"expected '{lexeme}' but found '{token.lexeme}'")
			return token

		def push_back_remaining(self):
			if not self.buffer:
				return
			self.parser.tokens[self.parser.pos:self.parser.pos] = self.buffer
			self.buffer = []

		def collect_block_tokens(self):
			depth = 1
			collected = []
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

	OP_PRECEDENCE = {}
	OP_PRECEDENCE["+"] = 1
	OP_PRECEDENCE["-"] = 1
	OP_PRECEDENCE["*"] = 2
	OP_PRECEDENCE["/"] = 2
	OP_PRECEDENCE["%"] = 2

	def parse_fn_body(tokens):
		if not tokens:
			raise ParseError("empty function body")
		lexemes = [tok.lexeme for tok in tokens if tok.lexeme]
		if not lexemes or lexemes[0] != "return":
			raise ParseError("function body must start with 'return'")
		if lexemes[-1] != ";":
			raise ParseError("function body must terminate with ';'")
		extra = lexemes[1:-1]
		if not extra:
			raise ParseError("missing return expression")
		return extra

	def shunting_yard(tokens):
		output = []
		stack = []
		for token in tokens:
			if token == "(":
				stack.append(token)
				continue
			if token == ")":
				while stack and stack[-1] != "(":
					output.append(stack.pop())
				if not stack:
					raise ParseError("mismatched parentheses in return expression")
				stack.pop()
				continue
			if token in OP_PRECEDENCE:
				while stack and stack[-1] in OP_PRECEDENCE and OP_PRECEDENCE[stack[-1]] >= OP_PRECEDENCE[token]:
					output.append(stack.pop())
				stack.append(token)
				continue
			output.append(token)
		while stack:
			top = stack.pop()
			if top == "(":
				raise ParseError("mismatched parentheses in return expression")
			output.append(top)
		return output

	def is_int_literal(text):
		try:
			int(text, 0)
			return True
		except ValueError:
			return False

	def translate_postfix(postfix, params):
		indices = {name: idx for idx, name in enumerate(params)}
		translated = []
		for token in postfix:
			if token in indices:
				translated.append(str(indices[token]))
				translated.append("rpick")
				continue
			if is_int_literal(token):
				translated.append(token)
				continue
			translated.append(token)
		return translated

	def macro(ctx):
		parser = ctx.parser
		if not isinstance(parser.context_stack[-1], Module):
			raise ParseError("'fn' definitions must be top-level")
		lexer = FnLexer(parser)
		name_token = lexer.pop()
		name = name_token.lexeme
		if not is_identifier(name):
			raise ParseError("invalid function name for 'fn'")
		lexer.expect("(")
		params = []
		if lexer.peek().lexeme != ")":
			while True:
				type_token = lexer.pop()
				if type_token.lexeme != "int":
					raise ParseError("only 'int' parameters are supported in fn definitions")
				param_token = lexer.pop()
				if not is_identifier(param_token.lexeme):
					raise ParseError("invalid parameter name in fn definition")
				params.append(param_token.lexeme)
				if lexer.peek().lexeme == ",":
					lexer.pop()
					continue
				break
		lexer.expect(")")
		lexer.expect("{")
		body_tokens = lexer.collect_block_tokens()
		lexer.push_back_remaining()
		if len(params) != len(set(params)):
			raise ParseError("duplicate parameter names in fn definition")
		return_tokens = parse_fn_body(body_tokens)
		postfix = shunting_yard(return_tokens)
		body_words = []
		for _ in reversed(params):
			body_words.append(">r")
		body_words.extend(translate_postfix(postfix, params))
		for _ in params:
			body_words.append("rdrop")
		generated = []
		emit_definition(generated, name_token, name, body_words)
		ctx.inject_token_objects(generated)
}
;

:asm dup {
	mov rax, [r12]
	sub r12, 8
	mov [r12], rax
}
;

:asm drop {
	add r12, 8
}
;

:asm swap {
	mov rax, [r12]
	mov rbx, [r12 + 8]
	mov [r12], rbx
	mov [r12 + 8], rax
}
;

:asm + {
	mov rax, [r12]
	add r12, 8
	add qword [r12], rax
}
;

:asm - {
	mov rax, [r12]
	add r12, 8
	sub qword [r12], rax
}
;

:asm * {
	mov rax, [r12]
	add r12, 8
	imul qword [r12]
	mov [r12], rax
}
;

:asm / {
	mov rbx, [r12]
	add r12, 8
	mov rax, [r12]
	cqo
	idiv rbx
	mov [r12], rax
}
;

:asm % {
	mov rbx, [r12]
	add r12, 8
	mov rax, [r12]
	cqo
	idiv rbx
	mov [r12], rdx
}
;

:asm == {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	sete bl
	mov [r12], rbx
}
;

:asm != {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setne bl
	mov [r12], rbx
}
;

:asm < {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setl bl
	mov [r12], rbx
}
;

:asm > {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setg bl
	mov [r12], rbx
}
;

:asm <= {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setle bl
	mov [r12], rbx
}
;

:asm >= {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setge bl
	mov [r12], rbx
}
;

:asm @ {
	mov rax, [r12]
	mov rax, [rax]
	mov [r12], rax
}
;

:asm ! {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	mov [rax], rbx
	add r12, 8
}
;

:asm mmap {
	mov r9, [r12]
	add r12, 8
	mov r8, [r12]
	add r12, 8
	mov r10, [r12]
	add r12, 8
	mov rdx, [r12]
	add r12, 8
	mov rsi, [r12]
	add r12, 8
	mov rdi, [r12]
	mov rax, 9
	syscall
	mov [r12], rax
}
;

:asm munmap {
	mov rsi, [r12]
	add r12, 8
	mov rdi, [r12]
	mov rax, 11
	syscall
	mov [r12], rax
}
;

:asm exit {
	mov rdi, [r12]
	add r12, 8
	mov rax, 60
	syscall
}
;

:asm >r {
	mov rax, [r12]
	add r12, 8
	sub r13, 8
	mov [r13], rax
}
;

:asm r> {
	mov rax, [r13]
	add r13, 8
	sub r12, 8
	mov [r12], rax
}
;

:asm rdrop {
	add r13, 8
}
;

:asm rpick {
	mov rcx, [r12]
	add r12, 8
	mov rax, [r13 + rcx * 8]
	sub r12, 8
	mov [r12], rax
}
;
