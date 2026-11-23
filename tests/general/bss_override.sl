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
    # Full replacement of the runtime BSS buffer (ct-output-set-bss) to grow
    # the persistent buffer.  A replacement must itself provide every
    # runtime symbol: dstack/dstack_top, rstack/rstack_top,
    # print_buf/print_buf_end, persistent, and the list_capture_* storage.
    # (To add storage while keeping the standard layout, use
    # ct-output-append-bss instead.)
    list-new
    "align 16" list-append
    "dstack: resb DSTK_BYTES" list-append
    "dstack_top:" list-append
    "align 16" list-append
    "rstack: resb RSTK_BYTES" list-append
    "rstack_top:" list-append
    "align 16" list-append
    "print_buf: resb PRINT_BUF_BYTES" list-append
    "print_buf_end:" list-append
    "align 16" list-append
    "persistent: resb 256" list-append
    "persistent_end:" list-append
    "align 16" list-append
    "list_capture_sp: resq 1" list-append
    "list_capture_tmp: resq 1" list-append
    "list_capture_stack: resq 1024" list-append
    ct-output-set-bss
end

word main
    persistent-size
    print
end
compile-time ss