extern eval_env 3 0

import stdlib.sl

word main
    "1 2 3" get_stack_top eval_env

    dup puti cr
    swap puti cr
    swap puti cr
    "eval_env ok" puts cr
    0
end
