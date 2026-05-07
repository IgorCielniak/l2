extern void eval_env(const char *source, long source_len, long stack_top_addr)

import stdlib.sl
import debug.sl

:asm get_stack_top {
    mov rax, r12
    sub r12, 8
    mov [r12], rax
    ret
}
;

word main
    1 "1 2 + +" get_stack_top eval_env
    depth dump
    0 exit
end