extern eval_env 3 0

import stdlib.sl
import gvars.sl

# Built with --no-dce --word-table, eval runs the program's own words as
# native code: `bump` changes the runtime global `counter` and `show` prints
# through the runtime stdlib, which an interpreted copy could not do.
global counter

preserve word some 1 2 end
preserve word bump counter @ 1 + counter swap ! end
preserve word show "native show" puts end

word main
    counter 40 !
    "some 3 + +" get_stack_top eval_env
    puti cr
    "bump bump show" get_stack_top eval_env
    counter @ puti cr
    0
end
