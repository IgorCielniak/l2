extern eval_program 2 1

import stdlib.sl

word main
   "import stdlib.sl word main 1 2 + puti cr end" eval_program drop
   "import stdlib.sl word main 99 puti cr end" eval_program drop
   "import stdlib.sl word main 5 5 * 3 + puti cr end" eval_program drop
   "import stdlib.sl word triple 3 * end word main 7 triple puti cr end" eval_program drop
   "import stdlib.sl word main 10 20 30 + + puti cr end" eval_program drop
   "import stdlib.sl word main 2 3 + 4 * 5 - puti cr end" eval_program drop
   "import stdlib.sl word main 1 2 3 4 5 + + + + puti cr end" eval_program drop
   "import stdlib.sl word main 42 puti cr end" eval_program drop
   "import stdlib.sl word main 777 puti cr end" eval_program drop
   "import stdlib.sl word main 888 puti cr end" eval_program drop
   "eval_program ok" puts cr
   0
end