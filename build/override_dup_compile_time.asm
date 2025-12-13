section .text
%define DSTK_BYTES 65536
%define RSTK_BYTES 65536
%define PRINT_BUF_BYTES 128
global _start
_start:
    ; initialize data/return stack pointers
    lea r12, [rel dstack_top]
    mov r15, r12
    lea r13, [rel rstack_top]
    call word_main
    mov rax, 0
    cmp r12, r15
    je .no_exit_value
    mov rax, [r12]
    add r12, 8
.no_exit_value:
    mov rdi, rax
    mov rax, 60
    syscall
word_puts:
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
    ret
word_dup:
	mov rax, [r12]
	sub r12, 8
	mov [r12], rax
    ret
word_drop:
	add r12, 8
    ret
word_over:
	mov rax, [r12 + 8]
	sub r12, 8
	mov [r12], rax
    ret
word_swap:
	mov rax, [r12]
	mov rbx, [r12 + 8]
	mov [r12], rbx
	mov [r12 + 8], rax
    ret
word_rot:
	mov rax, [r12]       ; x3
	mov rbx, [r12 + 8]   ; x2
	mov rcx, [r12 + 16]  ; x1
	mov [r12], rcx       ; top = x1
	mov [r12 + 8], rax   ; next = x3
	mov [r12 + 16], rbx  ; third = x2
    ret
word__2drot:
	mov rax, [r12]       ; x3
	mov rbx, [r12 + 8]   ; x2
	mov rcx, [r12 + 16]  ; x1
	mov [r12], rbx       ; top = x2
	mov [r12 + 8], rcx   ; next = x1
	mov [r12 + 16], rax  ; third = x3
    ret
word_nip:
	mov rax, [r12]
	add r12, 8           ; drop lower element
	mov [r12], rax       ; keep original top
    ret
word_tuck:
	mov rax, [r12]       ; x2
	mov rbx, [r12 + 8]   ; x1
	sub r12, 8           ; make room
	mov [r12], rax       ; x2
	mov [r12 + 8], rbx   ; x1
	mov [r12 + 16], rax  ; x2
    ret
word_2dup:
	mov rax, [r12]       ; b
	mov rbx, [r12 + 8]   ; a
	sub r12, 8
	mov [r12], rbx       ; push a
	sub r12, 8
	mov [r12], rax       ; push b
    ret
word_2drop:
	add r12, 16
    ret
word_2swap:
	mov rax, [r12]        ; d
	mov rbx, [r12 + 8]    ; c
	mov rcx, [r12 + 16]   ; b
	mov rdx, [r12 + 24]   ; a
	mov [r12], rcx        ; top = b
	mov [r12 + 8], rdx    ; next = a
	mov [r12 + 16], rax   ; third = d
	mov [r12 + 24], rbx   ; fourth = c
    ret
word_2over:
	mov rax, [r12 + 16]   ; b
	mov rbx, [r12 + 24]   ; a
	sub r12, 8
	mov [r12], rbx        ; push a
	sub r12, 8
	mov [r12], rax        ; push b
    ret
word__2b:
	mov rax, [r12]
	add r12, 8
	add qword [r12], rax
    ret
word__2d:
	mov rax, [r12]
	add r12, 8
	sub qword [r12], rax
    ret
word__2a:
	mov rax, [r12]
	add r12, 8
	imul qword [r12]
	mov [r12], rax
    ret
word__2f:
	mov rbx, [r12]
	add r12, 8
	mov rax, [r12]
	cqo
	idiv rbx
	mov [r12], rax
    ret
word__25:
	mov rbx, [r12]
	add r12, 8
	mov rax, [r12]
	cqo
	idiv rbx
	mov [r12], rdx
    ret
word__3d_3d:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	sete bl
	mov [r12], rbx
    ret
word__21_3d:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setne bl
	mov [r12], rbx
    ret
word__3c:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setl bl
	mov [r12], rbx
    ret
word__3e:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setg bl
	mov [r12], rbx
    ret
word__3c_3d:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setle bl
	mov [r12], rbx
    ret
word__3e_3d:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	cmp rbx, rax
	mov rbx, 0
	setge bl
	mov [r12], rbx
    ret
word__40:
	mov rax, [r12]
	mov rax, [rax]
	mov [r12], rax
    ret
word__21:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	mov [rax], rbx
	add r12, 8
    ret
word_mmap:
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
    ret
word_munmap:
	mov rsi, [r12]
	add r12, 8
	mov rdi, [r12]
	mov rax, 11
	syscall
	mov [r12], rax
    ret
word_exit:
	mov rdi, [r12]
	add r12, 8
	mov rax, 60
	syscall
    ret
word_and:
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
    ret
word_or:
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
    ret
word_not:
	mov rax, [r12]
	test rax, rax
	setz al
	movzx rax, al
	mov [r12], rax
    ret
word__3er:
	mov rax, [r12]
	add r12, 8
	sub r13, 8
	mov [r13], rax
    ret
word_r_3e:
	mov rax, [r13]
	add r13, 8
	sub r12, 8
	mov [r12], rax
    ret
word_rdrop:
	add r13, 8
    ret
word_pick:
	mov rcx, [r12]
	add r12, 8
	mov rax, [r12 + rcx * 8]
	sub r12, 8
	mov [r12], rax
    ret
word_rpick:
	mov rcx, [r12]
	add r12, 8
	mov rax, [r13 + rcx * 8]
	sub r12, 8
	mov [r12], rax
    ret
word_main:
    ; push 6
    sub r12, 8
    mov qword [r12], 6
    call word_puts
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    ret
section .data
data_start:
data_end:
section .bss
align 16
dstack: resb DSTK_BYTES
dstack_top:
align 16
rstack: resb RSTK_BYTES
rstack_top:
align 16
print_buf: resb PRINT_BUF_BYTES
print_buf_end: