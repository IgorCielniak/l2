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
word_mem_2dslot:
    lea rax, [rel print_buf]
    sub r12, 8
    mov [r12], rax
    ret
word_MAGIC:
    ; push 99
    sub r12, 8
    mov qword [r12], 99
    ret
word_add13:
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 8
    sub r12, 8
    mov qword [r12], 8
    call word__2b
    ret
word_Point_2esize:
    ; push 16
    sub r12, 8
    mov qword [r12], 16
    ret
word_Point_2ex_2esize:
    ; push 8
    sub r12, 8
    mov qword [r12], 8
    ret
word_Point_2ex_2eoffset:
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    ret
word_Point_2ex_40:
    call word_Point_2ex_2eoffset
    call word__2b
    call word__40
    ret
word_Point_2ex_21:
    call word_Point_2ex_2eoffset
    call word__2b
    call word__21
    ret
word_Point_2ey_2esize:
    ; push 8
    sub r12, 8
    mov qword [r12], 8
    ret
word_Point_2ey_2eoffset:
    ; push 8
    sub r12, 8
    mov qword [r12], 8
    ret
word_Point_2ey_40:
    call word_Point_2ey_2eoffset
    call word__2b
    call word__40
    ret
word_Point_2ey_21:
    call word_Point_2ey_2eoffset
    call word__2b
    call word__21
    ret
word_fancy_add:
    call word__3er
    call word__3er
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    call word_rpick
    ; push 1
    sub r12, 8
    mov qword [r12], 1
    call word_rpick
    call word__2b
    ; push 1
    sub r12, 8
    mov qword [r12], 1
    call word_rpick
    call word__2a
    call word_rdrop
    call word_rdrop
    ret
word_test_2dadd:
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 7
    sub r12, 8
    mov qword [r12], 7
    call word__2b
    call word_puts
    ret
word_test_2dsub:
    ; push 10
    sub r12, 8
    mov qword [r12], 10
    ; push 3
    sub r12, 8
    mov qword [r12], 3
    call word__2d
    call word_puts
    ret
word_test_2dmul:
    ; push 6
    sub r12, 8
    mov qword [r12], 6
    ; push 7
    sub r12, 8
    mov qword [r12], 7
    call word__2a
    call word_puts
    ret
word_test_2ddiv:
    ; push 84
    sub r12, 8
    mov qword [r12], 84
    ; push 7
    sub r12, 8
    mov qword [r12], 7
    call word__2f
    call word_puts
    ret
word_test_2dmod:
    ; push 85
    sub r12, 8
    mov qword [r12], 85
    ; push 7
    sub r12, 8
    mov qword [r12], 7
    call word__25
    call word_puts
    ret
word_test_2ddrop:
    ; push 10
    sub r12, 8
    mov qword [r12], 10
    ; push 20
    sub r12, 8
    mov qword [r12], 20
    call word_drop
    call word_puts
    ret
word_test_2ddup:
    ; push 11
    sub r12, 8
    mov qword [r12], 11
    call word_dup
    call word__2b
    call word_puts
    ret
word_test_2dswap:
    ; push 2
    sub r12, 8
    mov qword [r12], 2
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word_swap
    call word__2d
    call word_puts
    ret
word_test_2dstore:
    call word_mem_2dslot
    call word_dup
    ; push 123
    sub r12, 8
    mov qword [r12], 123
    call word_swap
    call word__21
    call word__40
    call word_puts
    ret
word_test_2dmmap:
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    ; push 4096
    sub r12, 8
    mov qword [r12], 4096
    ; push 3
    sub r12, 8
    mov qword [r12], 3
    ; push 34
    sub r12, 8
    mov qword [r12], 34
    ; push -1
    sub r12, 8
    mov qword [r12], -1
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    call word_mmap
    call word_dup
    ; push 1337
    sub r12, 8
    mov qword [r12], 1337
    call word_swap
    call word__21
    call word_dup
    call word__40
    call word_puts
    ; push 4096
    sub r12, 8
    mov qword [r12], 4096
    call word_munmap
    call word_drop
    ret
word_test_2dmacro:
    ; push 9
    sub r12, 8
    mov qword [r12], 9
    call word_dup
    call word__2a
    call word_puts
    call word_MAGIC
    call word_puts
    call word_add13
    call word_puts
    ret
word_test_2dif:
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3d_3d
    mov rax, [r12]
    add r12, 8
    test rax, rax
    jz L_if_false_34
    ; push 111
    sub r12, 8
    mov qword [r12], 111
    call word_puts
    jmp L_if_end_35
L_if_false_34:
    ; push 222
    sub r12, 8
    mov qword [r12], 222
    call word_puts
L_if_end_35:
    ret
word_test_2delse_2dif:
    ; push 2
    sub r12, 8
    mov qword [r12], 2
    call word_dup
    ; push 1
    sub r12, 8
    mov qword [r12], 1
    call word__3d_3d
    mov rax, [r12]
    add r12, 8
    test rax, rax
    jz L_if_false_36
    ; push 50
    sub r12, 8
    mov qword [r12], 50
    call word_puts
    jmp L_if_end_37
L_if_false_36:
    call word_dup
    ; push 2
    sub r12, 8
    mov qword [r12], 2
    call word__3d_3d
    mov rax, [r12]
    add r12, 8
    test rax, rax
    jz L_if_false_38
    ; push 60
    sub r12, 8
    mov qword [r12], 60
    call word_puts
    jmp L_if_end_39
L_if_false_38:
    ; push 70
    sub r12, 8
    mov qword [r12], 70
    call word_puts
L_if_end_39:
L_if_end_37:
    call word_drop
    ret
word_test_2dfor:
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    mov rax, [r12]
    add r12, 8
    cmp rax, 0
    jle L_for_end_41
    sub r13, 8
    mov [r13], rax
L_for_loop_40:
    ; push 1
    sub r12, 8
    mov qword [r12], 1
    call word__2b
    mov rax, [r13]
    dec rax
    mov [r13], rax
    jg L_for_loop_40
    add r13, 8
L_for_end_41:
    call word_puts
    ret
word_test_2dfor_2dzero:
    ; push 123
    sub r12, 8
    mov qword [r12], 123
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    mov rax, [r12]
    add r12, 8
    cmp rax, 0
    jle L_for_end_43
    sub r13, 8
    mov [r13], rax
L_for_loop_42:
    call word_drop
    mov rax, [r13]
    dec rax
    mov [r13], rax
    jg L_for_loop_42
    add r13, 8
L_for_end_43:
    call word_puts
    ret
word_test_2dstruct:
    call word_mem_2dslot
    call word_dup
    ; push 111
    sub r12, 8
    mov qword [r12], 111
    call word_swap
    call word_Point_2ex_21
    call word_dup
    ; push 222
    sub r12, 8
    mov qword [r12], 222
    call word_swap
    call word_Point_2ey_21
    call word_dup
    call word_Point_2ex_40
    call word_puts
    call word_Point_2ey_40
    call word_puts
    call word_Point_2esize
    call word_puts
    ret
word_test_2dcmp:
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3d_3d
    call word_puts
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 4
    sub r12, 8
    mov qword [r12], 4
    call word__3d_3d
    call word_puts
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 4
    sub r12, 8
    mov qword [r12], 4
    call word__21_3d
    call word_puts
    ; push 4
    sub r12, 8
    mov qword [r12], 4
    ; push 4
    sub r12, 8
    mov qword [r12], 4
    call word__21_3d
    call word_puts
    ; push 3
    sub r12, 8
    mov qword [r12], 3
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3c
    call word_puts
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 3
    sub r12, 8
    mov qword [r12], 3
    call word__3c
    call word_puts
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 3
    sub r12, 8
    mov qword [r12], 3
    call word__3e
    call word_puts
    ; push 3
    sub r12, 8
    mov qword [r12], 3
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3e
    call word_puts
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3c_3d
    call word_puts
    ; push 6
    sub r12, 8
    mov qword [r12], 6
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3c_3d
    call word_puts
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3e_3d
    call word_puts
    ; push 4
    sub r12, 8
    mov qword [r12], 4
    ; push 5
    sub r12, 8
    mov qword [r12], 5
    call word__3e_3d
    call word_puts
    ret
word_test_2dc_2dfn:
    ; push 3
    sub r12, 8
    mov qword [r12], 3
    ; push 7
    sub r12, 8
    mov qword [r12], 7
    call word_fancy_add
    call word_puts
    ret
word_main:
    call word_test_2dadd
    call word_test_2dsub
    call word_test_2dmul
    call word_test_2ddiv
    call word_test_2dmod
    call word_test_2ddrop
    call word_test_2ddup
    call word_test_2dswap
    call word_test_2dstore
    call word_test_2dmmap
    call word_test_2dmacro
    call word_test_2dif
    call word_test_2delse_2dif
    call word_test_2dfor
    call word_test_2dfor_2dzero
    call word_test_2dcmp
    call word_test_2dstruct
    call word_test_2dc_2dfn
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