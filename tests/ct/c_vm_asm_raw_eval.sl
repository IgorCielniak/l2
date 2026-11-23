extern eval_program 2 1

import stdlib.sl

# In main.c an :asm body is raw text, so a `}` or quotes inside an
# assembler comment neither end the body nor start a string.
word main
   ":asm seven {\n ; it's \"}\" -- not the end }\n sub r12, 8\n mov qword [r12], 7\n}\n;\nimport stdlib.sl word main seven puti cr end" eval_program drop
   "c vm raw asm ok" puts cr
   0
end
