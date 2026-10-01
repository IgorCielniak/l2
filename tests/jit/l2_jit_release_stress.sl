extern l2_jit_from_asm 2 1
extern l2_jit_release 1 0

import stdlib/stdlib.sl
import stdlib/control.sl

# Stress test: repeatedly install a runtime-assembled asm snippet,
# invoke it, and release it.  Exercises `l2_jit_from_asm` +
# `l2_jit_release` (and the underlying mmap / munmap paths) many
# times in a row.

word main
    0
    50 for
        drop
        "mov rax, [r12]\nadd rax, 1\nmov [r12], rax\nret\n" l2_jit_from_asm
        dup                    # ( fn fn )
        41                     # ( fn fn 41 )
        swap                   # ( fn 41 fn )
        call                   # ( fn 42 )
        drop                   # ( fn )
        l2_jit_release         # ( )
        0
    end
    drop
    "jit release stress ok" puts cr
    0
end
