
# L2 IO Primitives

# : read_file ( path_ptr path_len -- len addr | 0 0 )
#   Reads the file at the given path (pointer+length, not null-terminated),
#   returns (len addr) of mapped file, or (0 0) on error.

:asm read_file {
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
}
;

# : write_file ( path_ptr path_len buf_ptr buf_len -- bytes_written | neg_errno )
:asm write_file {
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
}
;

# : read_stdin ( max_len -- len addr | neg_errno 0 )
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