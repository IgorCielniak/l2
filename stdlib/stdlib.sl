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

# : strlen ( addr len -- len )
:asm strlen {
	mov rax, [r12]      ; addr
	mov rcx, [r12 + 8]  ; len
	add r12, 16         ; pop len and addr
	mov [r12], rcx      ; push len
	ret
}
;

:asm puts {
	; detects string if top is len>=0 and next is a pointer in [data_start, data_end]
	mov rax, [r12]      ; len or int value
	mov rbx, [r12 + 8]  ; possible address
	cmp rax, 0
	jl puts_print_int
	lea r8, [rel data_start]
	lea r9, [rel data_end]
	cmp rbx, r8
	jl puts_print_int
	cmp rbx, r9
	jge puts_print_int
	; treat as string: (addr below len)
	mov rdx, rax        ; len
	mov rsi, rbx        ; addr
	add r12, 16         ; pop len + addr
	test rdx, rdx
	jz puts_str_newline_only
	mov rax, 1
	mov rdi, 1
	syscall
puts_str_newline_only:
	mov byte [rel print_buf], 10
	mov rax, 1
	mov rdi, 1
	lea rsi, [rel print_buf]
	mov rdx, 1
	syscall
	ret

puts_print_int:
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

# : write_buf ( len addr -- )
:asm write_buf {
	mov rdx, [r12]        ; len
	mov rsi, [r12 + 8]    ; addr
	add r12, 16           ; pop len + addr
	mov rax, 1            ; syscall: write
	mov rdi, 1            ; fd = stdout
	syscall
	ret
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
    ; Save rsp and align to 16 bytes for syscall ABI
    mov rax, rsp
    and rsp, -16
    mov rdi, [r12+40]   ; addr
    mov rsi, [r12+32]   ; length
    mov rdx, [r12+24]   ; prot
    mov r10, [r12+16]   ; flags
    mov r8,  [r12+8]    ; fd
    mov r9,  [r12]      ; offset
    add r12, 48         ; pop 6 args
    mov rax, 9          ; syscall: mmap
    syscall
    mov rsp, rax        ; restore rsp
    sub r12, 8
    mov [r12], rax      ; push result
    ret
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
