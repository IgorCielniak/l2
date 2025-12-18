
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