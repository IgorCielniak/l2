extern eval_env 3 0

import stdlib.sl

# lambda.sl inside eval: the eval runtime has the parser words it needs.
word main
    "import lambda.sl lambda( 1 2 + ) call lambda( 20 ) call +" get_stack_top eval_env
    puti cr
    "import lambda.sl 5 lambda( lambda( 2 * ) call 1 + ) call" get_stack_top eval_env
    puti cr
    0
end
