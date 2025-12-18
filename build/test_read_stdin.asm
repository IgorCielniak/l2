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
word_c_40:
	mov rax, [r12]
	movzx rax, byte [rax]
	mov [r12], rax
	ret
    ret
word_c_21:
	mov rax, [r12]
	add r12, 8
	mov rbx, [r12]
	mov [rbx], al
	ret
    ret
word_r_40:
	mov rax, [r13]
	sub r12, 8
	mov [r12], rax
	ret
    ret
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
word_write_buf:
	mov rdx, [r12]        ; len
	mov rsi, [r12 + 8]    ; addr
	add r12, 16           ; pop len + addr
	mov rax, 1            ; syscall: write
	mov rdi, 1            ; fd = stdout
	syscall
	ret
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
word_read_file:
	; stack: path_ptr (top), path_len (next)
	mov rsi, [r12]        ; path_ptr
	mov rdx, [r12 + 8]    ; path_len
	add r12, 16           ; pop args

	; open(path_ptr, O_RDONLY=0, mode=0)
	mov rax, 2            ; syscall: open
	mov rdi, rsi          ; filename
	xor rsi, rsi          ; flags = O_RDONLY
	xor rdx, rdx          ; mode = 0
	syscall
	mov r10, rax          ; save open() result
	cmp rax, 0
	jl .fail_open
	mov r8, rax           ; fd

	; use lseek to determine file size: lseek(fd, 0, SEEK_END)
	mov rax, 8            ; syscall: lseek
	mov rdi, r8           ; fd
	xor rsi, rsi          ; offset = 0
	mov rdx, 2            ; SEEK_END
	syscall
	mov r11, rax          ; save lseek() result
	cmp rax, 0
	jl .close_fail_lseek
	mov rsi, rax          ; length = size

	; mmap(NULL, size, PROT_READ=1, MAP_PRIVATE=2, fd, 0)
	mov rax, 9            ; syscall: mmap
	xor rdi, rdi          ; addr = NULL
	; rsi already holds length
	mov rdx, 1            ; PROT_READ
	mov r10, 2            ; MAP_PRIVATE
	mov r8, r8            ; fd
	xor r9, r9            ; offset = 0
	syscall
	mov rbx, rax          ; addr
	mov r12, r12          ; (no-op, for debug)
	mov rax, 3            ; syscall: close
	mov rdi, r8           ; fd
	syscall
	cmp rbx, -4095
	jae .fail_mmap
	sub r12, 16
	mov [r12], rsi        ; len (rsi held length across syscall)
	mov [r12 + 8], rbx    ; addr
	ret

.close_fail_lseek:
	mov rax, 3
	mov rdi, r8
	syscall
	mov rax, r11          ; return lseek() error code
	sub r12, 16
	mov [r12], rax
	mov qword [r12 + 8], -1
	ret

.fail_open:
	mov rax, r10          ; return open() error code
	sub r12, 16
	mov [r12], rax
	mov qword [r12 + 8], -2
	ret

.fail_mmap:
	mov rax, -1           ; return mmap() error
	sub r12, 16
	mov [r12], rax
	mov qword [r12 + 8], -3
	ret
    ret
word_write_file:
	; stack: path_ptr (top), path_len, buf_ptr, buf_len
	mov rsi, [r12]        ; path_ptr
	mov rdx, [r12 + 8]    ; path_len
	mov r15, [r12 + 16]   ; buf_ptr (save in callee-saved r15)
	mov r13, [r12 + 24]   ; buf_len (save in callee-saved r13)
	add r12, 32           ; pop 4 args (we saved buf info)

	; open(path_ptr, O_WRONLY|O_CREAT|O_TRUNC, 0666)
	mov rdi, rsi          ; filename
	mov rsi, 577          ; flags = O_WRONLY|O_CREAT|O_TRUNC
	mov rdx, 438          ; mode = 0o666
	mov rax, 2            ; syscall: open
	syscall
	cmp rax, 0
	jl .fail_open
	mov r9, rax           ; save fd

	; write(fd, buf_ptr, buf_len) -- use preserved r15/r13 which survive syscalls
	mov rax, 1            ; syscall: write
	mov rdi, r9           ; fd
	mov rsi, r15          ; buf_ptr
	mov rdx, r13          ; buf_len
	syscall
	mov r10, rax          ; save write result
	cmp r10, 0
	jl .fail_write

	; close(fd)
	mov rax, 3            ; syscall: close
	mov rdi, r9
	syscall

	sub r12, 8
	mov [r12], r10
	ret

.fail_write:
	mov rax, 3
	mov rdi, r9
	syscall
	sub r12, 8
	mov [r12], r10
	ret

.fail_open:
	sub r12, 8
	mov [r12], rax
	ret
    ret
word_read_stdin:
	; stack: max_len
	mov r14, [r12]        ; max_len
	add r12, 8            ; pop max_len

	; mmap(NULL, max_len, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0)
	mov rax, 9            ; syscall: mmap
	xor rdi, rdi          ; addr = NULL
	mov rsi, r14          ; length
	mov rdx, 3            ; PROT_READ|PROT_WRITE
	mov r10, 34           ; MAP_PRIVATE|MAP_ANONYMOUS
	mov r8, -1            ; fd = -1
	xor r9, r9            ; offset = 0
	syscall
	cmp rax, -4095
	jae .fail_mmap
	mov rbx, rax          ; buffer addr
	xor rcx, rcx          ; bytes_read = 0

.read_loop:
	mov rax, 0            ; syscall: read
	mov rdi, 0            ; fd = stdin
	lea rsi, [rbx + rcx]  ; buf + offset
	mov rdx, r14
	sub rdx, rcx          ; remaining = max_len - bytes_read
	syscall
	cmp rax, 0
	je .done_read
	js .read_error
	add rcx, rax
	cmp rcx, r14
	jl .read_loop

.done_read:
	; push len (rcx) then addr (rbx)
	sub r12, 16
	mov [r12], rcx
	mov [r12 + 8], rbx
	ret

.read_error:
	; return negative errno in rax, addr = 0
	sub r12, 16
	mov [r12], rax
	mov qword [r12 + 8], 0
	ret

.fail_mmap:
	sub r12, 16
	mov qword [r12], -1
	mov qword [r12 + 8], 0
	ret
    ret
word_main:
    ; push 1024
    sub r12, 8
    mov qword [r12], 1024
    call word_read_stdin
    call word_dup
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    call word__3e
    mov rax, [r12]
    add r12, 8
    test rax, rax
    jz L_if_false_0
    call word_write_buf
    ; push 0
    sub r12, 8
    mov qword [r12], 0
    call word_exit
L_if_false_0:
    ; push str_0
    sub r12, 8
    mov qword [r12], str_0
    ; push 17
    sub r12, 8
    mov qword [r12], 17
    call word_puts
    call word_exit
    ret
section .data
data_start:
str_0: db 114, 101, 97, 100, 95, 115, 116, 100, 105, 110, 32, 102, 97, 105, 108, 101, 100, 0
str_0_len equ 17
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