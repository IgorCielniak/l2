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

#sleep_ns [* | nanoseconds] -> [*]
:asm sleep_ns {
    mov rax, [r12]
    add r12, 8

    mov rcx, 1000000000
    xor rdx, rdx
    div rcx                 ; rax = sec, rdx = nsec

    sub rsp, 16
    mov [rsp], rax
    mov [rsp + 8], rdx

    mov rax, 35             ; nanosleep
    mov rdi, rsp
    xor rsi, rsi
    syscall

    add rsp, 16
}
;

#sleep_seconds [* | seconds] -> [*]
:asm sleep_seconds {
    mov rax, [r12]
    add r12, 8

    sub rsp, 16
    mov [rsp], rax
    mov qword [rsp + 8], 0

    mov rax, 35             ; nanosleep
    mov rdi, rsp
    xor rsi, rsi
    syscall

    add rsp, 16
}
;

#monotonic_ns [*] -> [* | nanoseconds]
:asm monotonic_ns {
    sub rsp, 16

    mov rax, 228            ; clock_gettime
    mov rdi, 1              ; CLOCK_MONOTONIC
    mov rsi, rsp
    syscall

    cmp rax, 0
    jl .clock_fail

    mov rax, [rsp]
    imul rax, rax, 1000000000
    add rax, [rsp + 8]
    add rsp, 16
    sub r12, 8
    mov [r12], rax
    ret

.clock_fail:
    add rsp, 16
    sub r12, 8
    mov [r12], rax
}
;

#wait [* | nanoseconds] -> [*]
word wait
    sleep_ns
end

#sleep_one_second [*] -> [*]
word sleep_one_second
    1 sleep_seconds
end
