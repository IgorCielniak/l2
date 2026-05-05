extern eval 2 1
extern eval_program 2 1

import stdlib.sl

word main
   "eval_program:" puts cr
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

   "eval:" puts cr
   "word square dup * end" eval drop
   "7 square" eval puti cr
   "word twice dup + end" eval drop
   "9 twice" eval puti cr
   "word combo square twice + end" eval drop
   "3 combo" eval puti cr
   "word remix dup square swap twice + end" eval drop
   "4 remix" eval puti cr
   "word inc 1 + end" eval drop
   "41 inc" eval puti cr
   "word composed square twice + inc + end" eval drop
   "2 composed" eval puti cr

   "test complete" puts cr
   0
end

