:asm int3 {
	int3
}
;

# : c@ ( addr -- byte )
:asm c@ {
	mov rax, [r12]
	movzx rax, byte [rax]
	mov [r12], rax
	ret
}
;

# : c! ( byte addr -- )
:asm c! {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	mov [rbx], al
	ret
}
;

# : r@ ( -- x )
:asm r@ {
	mov rax, [r13]
	sub r12, 8
	mov [r12], rax
	ret
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

:asm over {
	mov rax, [r12 + 8]
	sub r12, 8
	mov [r12], rax
}
;

:asm swap {
	mov rax, [r12]
	mov rbx, [r12 + 8]
	mov [r12], rbx
	mov [r12 + 8], rax
}
;

:asm rot {
	mov rax, [r12]       ; x3
	mov rbx, [r12 + 8]   ; x2
	mov rcx, [r12 + 16]  ; x1
	mov [r12], rcx       ; top = x1
	mov [r12 + 8], rax   ; next = x3
	mov [r12 + 16], rbx  ; third = x2
}
;

:asm -rot {
	mov rax, [r12]       ; x3
	mov rbx, [r12 + 8]   ; x2
	mov rcx, [r12 + 16]  ; x1
	mov [r12], rbx       ; top = x2
	mov [r12 + 8], rcx   ; next = x1
	mov [r12 + 16], rax  ; third = x3
}
;

:asm nip {
	mov rax, [r12]
	add r12, 8           ; drop lower element
	mov [r12], rax       ; keep original top
}
;

:asm tuck {
	mov rax, [r12]       ; x2
	mov rbx, [r12 + 8]   ; x1
	sub r12, 8           ; make room
	mov [r12], rax       ; x2
	mov [r12 + 8], rbx   ; x1
	mov [r12 + 16], rax  ; x2
}
;

:asm 2dup {
	mov rax, [r12]       ; b
	mov rbx, [r12 + 8]   ; a
	sub r12, 8
	mov [r12], rbx       ; push a
	sub r12, 8
	mov [r12], rax       ; push b
}
;

:asm 2drop {
	add r12, 16
}
;

:asm 2swap {
	mov rax, [r12]        ; d
	mov rbx, [r12 + 8]    ; c
	mov rcx, [r12 + 16]   ; b
	mov rdx, [r12 + 24]   ; a
	mov [r12], rcx        ; top = b
	mov [r12 + 8], rdx    ; next = a
	mov [r12 + 16], rax   ; third = d
	mov [r12 + 24], rbx   ; fourth = c
}
;

:asm 2over {
	mov rax, [r12 + 16]   ; b
	mov rbx, [r12 + 24]   ; a
	sub r12, 8
	mov [r12], rbx        ; push a
	sub r12, 8
	mov [r12], rax        ; push b
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

:asm and {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	test rax, rax
	setz cl
	test rbx, rbx
	setz dl
	movzx rcx, cl
	movzx rdx, dl
	and rcx, rdx
	mov [r12], rcx
}
;

:asm or {
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	test rax, rax
	setz cl
	test rbx, rbx
	setz dl
	movzx rcx, cl
	movzx rdx, dl
	or rcx, rdx
	mov [r12], rcx
}
;

:asm not {
	mov rax, [r12]
	test rax, rax
	setz al
	movzx rax, al
	mov [r12], rax
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

:asm pick {
	mov rcx, [r12]
	add r12, 8
	mov rax, [r12 + rcx * 8]
	sub r12, 8
	mov [r12], rax
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
