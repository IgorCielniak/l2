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

# for qword values, for byte by byte seting see memset_bytes
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

# for qword values, for byte by byte dumping see memdump_bytes
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

#stack_pointer [*] -> [* | r12]
:asm stack_pointer {
    mov rax, r12
    sub r12, 8
    mov [r12], rax
}
;

#get_stack_top [*] -> [* | r12]
:asm get_stack_top {
    mov rax, r12
    sub r12, 8
    mov [r12], rax
}
;

#native_stack_pointer [*] -> [* | rsp]
:asm native_stack_pointer {
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

#stack_soft_limit_bytes [*] -> [* | bytes]
# Exact program data-stack capacity (dstack..dstack_top).
:asm stack_soft_limit_bytes {
    lea rax, [rel dstack_top]
    lea rbx, [rel dstack]
    sub rax, rbx
    sub r12, 8
    mov [r12], rax
}
;

#native_stack_soft_limit_bytes [*] -> [* | bytes] || [* | neg_errno]
:asm native_stack_soft_limit_bytes {
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

#stack_bounds [*] -> [*, low | high]
:asm stack_bounds {
    lea rax, [rel dstack]       ; low
    lea rbx, [rel dstack_top]   ; high
    sub r12, 8
    mov [r12], rax
    sub r12, 8
    mov [r12], rbx
}
;

#native_stack_bounds_estimate [*] -> [*, low | high]
:asm native_stack_bounds_estimate {
    mov r15, rsp
    sub rsp, 16
    mov rax, 97
    mov rdi, 3
    mov rsi, rsp
    syscall
    cmp rax, 0
    jl .fail

    mov rcx, [rsp]          ; soft stack limit
    add rsp, 16
    mov rdx, r15            ; high
    cmp rcx, -1             ; RLIM_INFINITY => unknown low bound
    je .infinite
    mov rax, rdx
    sub rax, rcx            ; low estimate = high - soft_limit
    jmp .push

.infinite:
    xor rax, rax
    jmp .push

.fail:
    add rsp, 16
    xor rax, rax
    xor rdx, rdx

.push:
    sub r12, 8
    mov [r12], rax
    sub r12, 8
    mov [r12], rdx
}
;

#is_stack_addr [* | addr] -> [* | flag]
# Exact check against L2 program data-stack allocation.
:asm is_stack_addr {
    mov rbx, [r12]          ; addr
    add r12, 8

    lea rcx, [rel dstack]       ; low
    lea rdx, [rel dstack_top]   ; high

    cmp rbx, rcx
    jb .false
    cmp rbx, rdx
    ja .false

    sub r12, 8
    mov qword [r12], 1
    ret

.false:
    sub r12, 8
    mov qword [r12], 0
}
;

#is_native_stack_addr [* | addr] -> [* | flag]
# Uses RLIMIT_STACK, so this is a best-effort native stack check.
:asm is_native_stack_addr {
    mov rbx, [r12]          ; addr
    add r12, 8

    mov r15, rsp            ; high estimate
    sub rsp, 16
    mov rax, 97
    mov rdi, 3
    mov rsi, rsp
    syscall
    cmp rax, 0
    jl .fail

    mov rcx, [rsp]          ; soft stack limit
    add rsp, 16
    cmp rcx, -1
    je .false

    mov rdx, r15
    sub rdx, rcx            ; low estimate

    cmp rbx, rdx
    jb .false
    cmp rbx, r15
    ja .false

    sub r12, 8
    mov qword [r12], 1
    ret

.fail:
    add rsp, 16
.false:
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

    ; Place the floor above the runtime .bss area so data/return stacks and
    ; static buffers are not misclassified as brk-heap addresses.
    lea r15, [rel list_capture_stack]
    add r15, 8192             ; list_capture_stack: resq 1024

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
        dup is_native_stack_addr if
            drop 0
        else
            dup is_brk_heap_addr if
                drop 0
            else
                brk_current >=
            end
        end
    end
end
