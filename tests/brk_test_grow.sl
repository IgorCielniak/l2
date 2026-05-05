import stdlib/mem.sl

:asm brk {
    mov rdi, [r12]
    add r12, 8
    mov rax, 12
    syscall
    sub r12, 8
    mov [r12], rax
} ;

word main
    brk_current dup puti cr
    4096 + brk puti cr
end
