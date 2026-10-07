# L2 IO Primitives

import linux.sl

#read [*, fd, buf | len] -> [* | bytes_read] || [* | neg_errno]
word read
    syscall.read
end

#input [*, buf | len] -> [* | bytes_read] || [* | neg_errno]
word input
    with buf len in
        FD_STDIN buf len read
    end
end

#read_file_size [*, path_addr | path_len] -> [* | size_or_neg_errno]
# path_addr must point to a NUL-terminated path; path_len is retained for API symmetry.
:asm read_file_size {
    mov rdi, [r12 + 8]
    add r12, 16
    mov rax, 2
    xor rsi, rsi
    xor rdx, rdx
    syscall
    test rax, rax
    js .open_error
    mov r8, rax
    mov rax, 8
    mov rdi, r8
    xor rsi, rsi
    mov rdx, 2
    syscall
    mov r10, rax
    mov rax, 3
    mov rdi, r8
    syscall
    mov rax, r10
    sub r12, 8
    mov [r12], rax
    ret
.open_error:
    sub r12, 8
    mov [r12], rax
    ret
}
;

#read_file [*, path_addr, path_len, buf_addr | buf_len] -> [* | bytes_read] || [* | neg_errno]
# path_addr must point to a NUL-terminated path.
:asm read_file {
    mov r14, [r12]
    mov r15, [r12 + 8]
    mov rdi, [r12 + 24]
    add r12, 32
    mov rax, 2
    xor rsi, rsi
    xor rdx, rdx
    syscall
    test rax, rax
    js .return
    mov r8, rax
    mov rax, 0
    mov rdi, r8
    mov rsi, r15
    mov rdx, r14
    syscall
    mov r10, rax
    mov rax, 3
    mov rdi, r8
    syscall
    mov rax, r10
.return:
    sub r12, 8
    mov [r12], rax
    ret
}
;

#write_file [*, path_addr, path_len, buf_addr | buf_len] -> [* | bytes_written] || [* | neg_errno]
# path_addr must point to a NUL-terminated path.
:asm write_file {
    mov r14, [r12]
    mov r15, [r12 + 8]
    mov rdi, [r12 + 24]
    add r12, 32
    mov rax, 2
    mov rsi, 577
    mov rdx, 438
    syscall
    test rax, rax
    js .return
    mov r8, rax
    mov rax, 1
    mov rdi, r8
    mov rsi, r15
    mov rdx, r14
    syscall
    mov r10, rax
    mov rax, 3
    mov rdi, r8
    syscall
    mov rax, r10
.return:
    sub r12, 8
    mov [r12], rax
    ret
}
;

#read_file2 [*, path_addr, path_len, buf_addr | buf_len] -> [* | bytes_read_or_neg_errno]
word read_file2
    with path path_len buf len in
        path O_RDONLY 0 syscall.open
        dup 0 < if
        else
            with fd in
                fd buf len syscall.read
                with count in
                    fd syscall.close drop
                    count
                end
            end
        end
    end
end

#write_file2 [*, path_addr, path_len, buf_addr | buf_len] -> [* | bytes_written_or_neg_errno]
word write_file2
    with path path_len buf len in
        path 577 438 syscall.open
        dup 0 < if
        else
            with fd in
                fd buf len syscall.write
                with count in
                    fd syscall.close drop
                    count
                end
            end
        end
    end
end

#getc_buffer [*] -> [* | addr]
:asm getc_buffer {
    lea rax, [rel print_buf]
    sub r12, 8
    mov [r12], rax
    ret
}
;

#getc [*] -> [* | char] [* | neg_error]
word getc
    getc_buffer 1 input
    dup 1 == if
        drop getc_buffer c@
    else
        drop -1
    end
end

#geti [*] -> [* | integer]
word geti
    1 >r
    getc
    dup 45 == if
        drop rdrop -1 >r getc
    end
    0 swap
    while dup 0 >= do
        dup 10 == if
            drop -1
        else
            dup 48 >= if
                dup 57 <= if
                    48 - swap 10 * +
                else
                    drop
                end
            else
                drop
            end
            getc
        end
    end
    drop
    r> *
end

#print [* | x] -> [*]
:asm print (effects string-io) {
    mov rax, [r12]      ; len or int value
    lea r8, [rel dstack_top]
    lea r9, [r12 + 16]
    cmp r9, r8
    ja .print_int
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
    dec rsi
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

#write_buf [*, addr | len] -> [*]
:asm write_buf (effects string-io) {
    mov rdx, [r12]        ; len
    mov rsi, [r12 + 8]    ; addr
    add r12, 16           ; pop len + addr
    mov rax, 1            ; syscall: write
    mov rdi, 1            ; fd = stdout
    syscall
    ret
}
;

#ewrite_buf [*, addr | len] -> [*]
:asm ewrite_buf (effects string-io) {
    mov rdx, [r12]        ; len
    mov rsi, [r12 + 8]    ; addr
    add r12, 16           ; pop len + addr
    mov rax, 1            ; syscall: write
    mov rdi, 2            ; fd = stderr
    syscall
    ret
}
;

#putc [* | char] -> [*]
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

#puti [* | int] -> [*]
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

#putu [* | uint] -> [*]
:asm putu {
    mov rbx, [r12]      ; get uint
    add r12, 8          ; pop
    lea rsi, [rel print_buf_end]
    mov rcx, 0
    mov r10, 10
    cmp rbx, 0
    jne .putu_digits
    dec rsi
    mov byte [rsi], '0'
    inc rcx
    jmp .putu_done
.putu_digits:
.putu_loop:
    xor rdx, rdx
    mov rax, rbx
    div r10
    add dl, '0'
    dec rsi
    mov [rsi], dl
    inc rcx
    mov rbx, rax
    test rbx, rbx
    jne .putu_loop
.putu_done:
    mov rax, 1          ; syscall: write
    mov rdi, 1          ; fd: stdout
    mov rdx, rcx        ; length
    syscall
    ret
}
;

#cr [*] -> [*]
inline word cr 10 putc end

#puts [*, addr | len] -> [*]
inline word puts write_buf cr end

#eputs [*, addr | len] -> [*]
inline word eputs ewrite_buf cr end
