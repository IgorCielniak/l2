extern eval_env 3 0
extern l2_eval_last_failed 0 1

import stdlib.sl

# `no-such-word` is defined nowhere, so the eval fails.  The failed eval
# reports it, leaves the stack as it was, and the program carries on;
# l2_eval_last_failed tells it what happened.

word main
    7
    "no-such-word 3 + +" get_stack_top eval_env
    l2_eval_last_failed puti cr
    puti cr
    "40 2 +" get_stack_top eval_env
    l2_eval_last_failed puti cr
    puti cr
    0
end
