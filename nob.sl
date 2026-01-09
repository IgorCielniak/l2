# Minimal nob-style helper: run a shell command via /bin/sh -c

# : sh ( cmd_addr cmd_len -- exit_code )
# Runs `/bin/sh -c <cmd>` and returns a waitpid-style exit code
# (WIFEXITED ? status>>8 : 128+signal). Returns neg errno on fork/exec failure.
:asm sh {
    ; stack: cmd_len (TOS), cmd_addr (NOS)
    push r15              ; preserve callee-saved
    push r14
    push r13
    push rbx

    mov rbx, [r12]        ; len
    mov r13, [r12 + 8]    ; addr (preserve across syscalls)
    add r12, 16           ; pop args

    mov r14, rbx          ; len
    inc r14               ; size = len + 1

    ; mmap buffer for C-string command
    mov rax, 9            ; mmap
    xor rdi, rdi          ; NULL addr
    mov rsi, r14          ; size
    mov rdx, 3            ; PROT_READ | PROT_WRITE
    mov r10, 34           ; MAP_PRIVATE | MAP_ANON
    mov r8, -1            ; fd = -1
    xor r9, r9            ; offset = 0
    syscall
    cmp rax, -4095
    jae .mmap_fail
    mov r15, rax          ; cmd buffer

    ; copy cmd into buffer and add NUL
    mov rcx, rbx          ; len
    mov rdi, r15          ; dst
    mov rsi, r13          ; src
    rep movsb
    mov byte [r15 + rbx], 0

    ; fork
    mov rax, 57           ; fork
    syscall
    cmp rax, 0
    jl .fork_fail
    cmp rax, 0
    jne .parent

.child:
    ; child: argv = "/bin/sh" "-c" cmd NULL
    sub rsp, 56
    lea rbx, [rel .sh_path]
    mov [rsp], rbx            ; argv[0]
    lea rbx, [rel .dash_c]
    mov [rsp + 8], rbx        ; argv[1]
    mov [rsp + 16], r15       ; argv[2]
    mov qword [rsp + 24], 0   ; argv[3] = NULL
    lea rsi, [rsp]            ; argv
    mov qword [rsp + 32], 0   ; envp[0] = NULL
    lea rdx, [rsp + 32]       ; envp
    lea rdi, [rel .sh_path]   ; filename
    mov rax, 59               ; execve
    syscall
    mov rdi, 127
    mov rax, 60               ; exit
    syscall

.parent:
    ; rax holds child pid
    mov rbx, rax              ; child pid
    sub rsp, 8
    lea rsi, [rsp]            ; status*
    xor rdx, rdx              ; options = 0
    xor r10, r10              ; rusage = NULL
    mov rdi, rbx              ; pid
    mov rax, 61               ; wait4
    syscall
    mov eax, [rsp]
    add rsp, 8

    ; decode exit status: if signaled -> 128+signal, else (status >> 8) & 0xff
    mov ebx, eax
    and ebx, 0x7f
    cmp ebx, 0
    jne .got_signal
    shr eax, 8
    and eax, 0xff
    jmp .status_ready
.got_signal:
    mov eax, ebx
    add eax, 128
.status_ready:
    mov edi, eax              ; save for return after unmap

    ; munmap command buffer
    mov rax, 11               ; munmap
    mov rdi, r15              ; addr
    mov rsi, r14              ; size
    syscall

    mov eax, edi
    sub r12, 8
    mov [r12], rax
    pop rbx
    pop r13
    pop r14
    pop r15
    ret

.fork_fail:
    mov rax, rax              ; rax holds neg errno
    jmp .cleanup_return

.mmap_fail:
    mov rax, -12              ; -ENOMEM
.cleanup_return:
    sub r12, 8
    mov [r12], rax
    pop rbx
    pop r13
    pop r14
    pop r15
    ret

.sh_path: db "/bin/sh", 0
.dash_c:  db "-c", 0
}
;
