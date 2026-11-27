extern eval_env 3 0

import stdlib.sl

# `inline preserve` keeps a native copy of `some` (a dead-code root), so
# --word-table alone lets eval call it natively; inlined uses still work.
inline preserve word some 1 2 end

word main
    "some 3 + +" get_stack_top eval_env
    puti cr
    some + puti cr
    0
end
