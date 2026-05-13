import stdlib.sl
import debug.sl

extern eval_env 3 0

word aa
    12
    34
end

word main
    "aa + puti cr" get_stack_top eval_env
    10 dump
end
