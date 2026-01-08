# L2 IO Primitives

# : read_file ( path_addr path_len -- addr len | 0 0 )
#   Reads the file at the given path (pointer+length, not null-terminated),
#   returns (addr len) of mapped file, or (0 0) on error.

:asm read_file {
	; stack: path_addr (NOS), path_len (TOS)
	mov rdx, [r12]        ; path_len
	mov rsi, [r12 + 8]    ; path_addr
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
}
;

# : write_file ( path_ptr path_len buf_ptr buf_len -- bytes_written | neg_errno )
:asm write_file {
	; stack: path_addr, path_len, buf_addr, buf_len (TOS)
	mov r13, [r12]        ; buf_len
	mov r15, [r12 + 8]    ; buf_addr
	mov rdx, [r12 + 16]   ; path_len
	mov rsi, [r12 + 24]   ; path_addr
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
}
;

# : read_stdin ( max_len -- addr len | neg_errno 0 )
:asm read_stdin {
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
	xor r9, r9          ; bytes_read = 0

.read_loop:
	mov rax, 0            ; syscall: read
	mov rdi, 0            ; fd = stdin
	lea rsi, [rbx + r9]  ; buf + offset
	mov rdx, r14
	sub rdx, r9          ; remaining = max_len - bytes_read
	syscall
	cmp rax, 0
	je .done_read
	js .read_error
	add r9, rax
	jl .read_loop

.done_read:
	; push len (rcx) then addr (rbx)
	cmp r9, r14
	je .done_no_null
	mov byte [rbx + r9], 0
.done_no_null:
	sub r12, 16
	mov [r12], r9
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
}
;

:asm print {
	; detects string if top is len>=0 and next is a pointer in [data_start, data_end]
	mov rax, [r12]      ; len or int value
	mov rbx, [r12 + 8]  ; possible address
	cmp rax, 0
	jl .print_int
	lea r8, [rel data_start]
	lea r9, [rel data_end]
	cmp rbx, r8
	jl .print_int
	cmp rbx, r9
	jge .print_int
	; treat as string: (addr below len)
	mov rdx, rax        ; len
	mov rsi, rbx        ; addr
	add r12, 16         ; pop len + addr
	test rdx, rdx
	jz .str_newline_only
	mov rax, 1
	mov rdi, 1
	syscall
.str_newline_only:
	mov byte [rel print_buf], 10
	mov rax, 1
	mov rdi, 1
	lea rsi, [rel print_buf]
	mov rdx, 1
	syscall
	ret
.print_int:
	mov rax, [r12]
	add r12, 8
	mov rbx, rax
	mov r8, 0
	cmp rbx, 0
	jge .abs
	neg rbx
	mov r8, 1
.abs:
	lea rsi, [rel print_buf_end]
	mov rcx, 0
	mov r10, 10
	cmp rbx, 0
	jne .digits
	dec rsi
	mov byte [rsi], '0'
	inc rcx
	jmp .sign
.digits:
.loop:
	xor rdx, rdx
	mov rax, rbx
	div r10
	add dl, '0'
	dec rsi
	mov [rsi], dl
	inc rcx
	mov rbx, rax
	test rbx, rbx
	jne .loop
.sign:
	cmp r8, 0
	je .finish_digits
	dec rsi
	mov byte [rsi], '-'
	inc rcx
.finish_digits:
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

# : write_buf ( addr len -- )
:asm write_buf {
	; data_start (trigger string_mode in compile-time VM)
	mov rdx, [r12]        ; len
	mov rsi, [r12 + 8]    ; addr
	add r12, 16           ; pop len + addr
	mov rax, 1            ; syscall: write
	mov rdi, 1            ; fd = stdout
	syscall
	ret
}
;

# : ewrite_buf ( addr len -- )
:asm ewrite_buf {
	; data_start (trigger string_mode in compile-time VM)
	mov rdx, [r12]        ; len
	mov rsi, [r12 + 8]    ; addr
	add r12, 16           ; pop len + addr
	mov rax, 1            ; syscall: write
	mov rdi, 2            ; fd = stderr
	syscall
	ret
}
;

# : putc ( char -- )
:asm putc {
	mov rax, [r12]
	add r12, 8
	lea rsi, [rel print_buf]
	mov [rsi], al
	mov rax, 1
	mov rdi, 1
	mov rdx, 1
	syscall
	ret
}
;

# : puti ( int -- )
:asm puti {
	mov rax, [r12]      ; get int
	add r12, 8          ; pop
	mov rbx, rax
	mov r8, 0           ; sign flag
	cmp rbx, 0
	jge .puti_pos
	neg rbx
	mov r8, 1
.puti_pos:
	lea rsi, [rel print_buf_end]
	mov rcx, 0
	mov r10, 10
	cmp rbx, 0
	jne .puti_digits
	dec rsi
	mov byte [rsi], '0'
	inc rcx
	jmp .puti_sign
.puti_digits:
.puti_loop:
	xor rdx, rdx
	mov rax, rbx
	div r10
	add dl, '0'
	dec rsi
	mov [rsi], dl
	inc rcx
	mov rbx, rax
	test rbx, rbx
	jne .puti_loop
.puti_sign:
	cmp r8, 0
	je .puti_done
	dec rsi
	mov byte [rsi], '-'
	inc rcx
.puti_done:
	mov rax, 1          ; syscall: write
	mov rdi, 1          ; fd: stdout
	mov rdx, rcx        ; length
	syscall
	ret
}
;

word cr 10 putc end

word puts write_buf cr end

word eputs ewrite_buf cr end
