#sleep [* | ts_ptr] -> [*]
:asm sleep {
    mov r14, [r12]
    add r12, 8

    ; nanosleep(ts_ptr, NULL)
    mov rax, 35
    mov rdi, r14
    xor rsi, rsi
    syscall
}
;
