extern eval_env 3 0

import stdlib.sl

# eval can define a word and push a pointer to it with `&name`; `call` on
# that pointer runs the word inside eval.
word main
    "word seven 7 end" get_stack_top eval_env
    "&seven call" get_stack_top eval_env
    puti cr
    "&seven" get_stack_top eval_env
    0 != puti cr
    0
end
