extern l2_jit_from_asm 2 1
extern l2_jit_release 1 0

import stdlib.sl

# JIT-assemble an L2-ABI asm snippet at runtime and invoke it via
# `call` from stdlib/core.sl.  The snippet reads top-of-stack from
# r12, adds 100, writes back, and returns.

word main
   "mov rax, [r12]\nadd rax, 100\nmov [r12], rax\nret\n" l2_jit_from_asm
   dup                          # keep the pointer for release
   42                           # push 42
   swap                         # ( fn 42 fn' )
   call                         # runs JIT'd asm: 42 -> 142
   puti cr                      # prints 142
   l2_jit_release
   0
end
