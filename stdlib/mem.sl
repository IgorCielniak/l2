import stdlib.sl

#alloc [* | size] -> [* | addr]
word alloc
    0      # addr hint (NULL)
    swap   # size
    3      # prot (PROT_READ | PROT_WRITE)
    34     # flags (MAP_PRIVATE | MAP_ANON)
    -1     # fd
    0      # offset
    mmap
    nip
end

#free [*, addr | size] -> [*]
word free
    munmap drop
end

#memcpy [*, dst_addr, src_addr | len] -> [*, dst_addr | len]
word memcpy
    dup
    >r
    swap
    dup c@
    3 pick swap
    c!
    swap
    for
        1 + dup
        c@
        swap
        -rot
        swap
        1 +
        dup
        rot
        c!
        swap
    end
    drop
    r> dup -rot - swap
end

#memset [*, addr, len | value] -> [*]
word memset
    swap
    0 swap for
        -rot swap 2 pick + 2dup swap ! 1 + -rot swap
    end
    2drop drop
end

# memset_bytes [*, addr, len | value] -> [*]
word memset_bytes
    swap
    0 swap for
        -rot swap 2 pick + 2dup swap c! 1 + -rot swap
    end
    2drop drop
end

#memdump [*, addr | len] -> [* | addr]
word memdump
    for
        dup @ puti cr 8 +
    end
end

#memdump_bytes [*, addr | len] -> [* | addr]
word memdump_bytes
    for
        dup c@ puti cr 1 +
    end
end

#realloc [*, addr, old_len | new_len] -> [* | new_addr]
word realloc
    2 pick swap alloc
    rot rot swap
    memcpy
    swap -rot free
end

#stack_pointer [*] -> [* | rsp]
:asm stack_pointer {
    mov rax, rsp
    sub r12, 8
    mov [r12], rax
}
;

#brk_current [*] -> [* | brk]
:asm brk_current {
    xor rdi, rdi
    mov rax, 12
    syscall
    sub r12, 8
    mov [r12], rax
}
;

#stack_soft_limit_bytes [*] -> [* | bytes_or_neg_errno]
:asm stack_soft_limit_bytes {
    sub rsp, 16
    mov rax, 97             ; getrlimit
    mov rdi, 3              ; RLIMIT_STACK
    mov rsi, rsp
    syscall
    cmp rax, 0
    jl .rlim_fail
    mov rax, [rsp]
    add rsp, 16
    sub r12, 8
    mov [r12], rax
    ret
.rlim_fail:
    add rsp, 16
    sub r12, 8
    mov [r12], rax
}
;

#stack_bounds_estimate [*] -> [*, low | high]
word stack_bounds_estimate
    stack_soft_limit_bytes
    stack_pointer
    over + swap
end

#is_stack_addr [* | addr] -> [* | flag]
:asm is_stack_addr {
    mov rbx, [r12]          ; addr
    add r12, 8

    ; r15 = current stack top estimate (saved rsp before temp frame)
    mov r15, rsp

    ; getrlimit(RLIMIT_STACK, &rlim)
    sub rsp, 16
    mov rax, 97
    mov rdi, 3
    mov rsi, rsp
    syscall
    cmp rax, 0
    jl .false

    mov rcx, [rsp]          ; soft stack limit
    mov rdx, r15            ; high
    sub rdx, rcx            ; low approx = high - soft_limit

    cmp rbx, rdx
    jb .false
    cmp rbx, r15
    ja .false

    add rsp, 16
    sub r12, 8
    mov qword [r12], 1
    ret

.false:
    add rsp, 16
    sub r12, 8
    mov qword [r12], 0
}
;

#is_brk_heap_addr [* | addr] -> [* | flag]
:asm is_brk_heap_addr {
    mov rbx, [r12]
    add r12, 8

    ; brk(0) -> current program break
    xor rdi, rdi
    mov rax, 12
    syscall
    mov r14, rax

    lea r15, [rel data_end] ; conservative low bound for classic brk heap

    cmp rbx, r15
    jb .false
    cmp rbx, r14
    jae .false

    sub r12, 8
    mov qword [r12], 1
    ret

.false:
    sub r12, 8
    mov qword [r12], 0
}
;

#is_mmap_addr [* | addr] -> [* | flag]
word is_mmap_addr
    dup is_stack_addr if
        drop 0
    else
        dup is_brk_heap_addr if
            drop 0
        else
            brk_current >=
        end
    end
end
