extern eval_program 2 1
import stdlib.sl

# A program's own :asm word, run at compile time by main.c's VM, may use
# rbx like any L2 code: the call into it must save the C caller's registers.
word main
   ":asm swap-via-rbx {\n mov rax, [r12]\n mov rbx, [r12 + 8]\n mov [r12], rbx\n mov [r12 + 8], rax\n}\n;\nimport stdlib.sl word probe 1 2 swap-via-rbx - int>string ct-parser-current swap token-with-lexeme list-new swap list-append ct-parser-inject end immediate word main probe puti cr end" eval_program drop
   "c vm asm callee-saved ok" puts cr
   0
end
