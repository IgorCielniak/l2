import stdlib/stdlib.sl
import stdlib/io.sl

:asm persistent-size {
    lea rax, [rel persistent_end]
    lea rbx, [rel persistent]
    sub rax, rbx
    sub r12, 8
    mov [r12], rax
}
;

word ss
    # Override BSS to grow the persistent buffer.
    bss-clear
    "align 16" bss-append
    "dstack: resb DSTK_BYTES" bss-append
    "dstack_top:" bss-append
    "align 16" bss-append
    "rstack: resb RSTK_BYTES" bss-append
    "rstack_top:" bss-append
    "align 16" bss-append
    "print_buf: resb PRINT_BUF_BYTES" bss-append
    "print_buf_end:" bss-append
    "align 16" bss-append
    "persistent: resb 256" bss-append
    "persistent_end:" bss-append
    "align 16" bss-append
    "list_capture_sp: resq 1" bss-append
    "list_capture_tmp: resq 1" bss-append
    "list_capture_stack: resq 1024" bss-append
end

word main
    persistent-size
    print
end
compile-time ss