import stdlib.sl

:asm _start {
    mov rdi, [rsp]
    lea rsi, [rsp+8]
    mov [rel sys_argc], rdi
    mov [rel sys_argv], rsi
    lea r12, [rel dstack_top]
    mov r15, r12
    lea r13, [rel rstack_top]
    ; print "hello world\n" before calling main using runtime `print_buf`
    mov byte [rel print_buf], 'h'
    mov byte [rel print_buf + 1], 'e'
    mov byte [rel print_buf + 2], 'l'
    mov byte [rel print_buf + 3], 'l'
    mov byte [rel print_buf + 4], 'o'
    mov byte [rel print_buf + 5], ' '
    mov byte [rel print_buf + 6], 'w'
    mov byte [rel print_buf + 7], 'o'
    mov byte [rel print_buf + 8], 'r'
    mov byte [rel print_buf + 9], 'l'
    mov byte [rel print_buf + 10], 'd'
    mov byte [rel print_buf + 11], 10
    lea rsi, [rel print_buf]
    mov rdx, 12
    mov rax, 1
    mov rdi, 1
    syscall
    call main
    mov rax, 0
    cmp r12, r15
    je .no_exit_value
    mov rax, [r12]
    add r12, 8
    .no_exit_value:
    mov rdi, rax
    mov rax, 60
    syscall
};

word main
    24 puti cr
end
 