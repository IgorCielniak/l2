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
