extern eval_program 2 1

import stdlib.sl

# Each eval_program body is parsed and run by main.c's compile-time VM.
word main
   # ct-definition-prefer-l2-by-name marks an existing definition and
   # pushes 1; the probe injects that flag, so this prints 1.
   "import stdlib.sl word myword 6 end word probe \"myword\" ct-definition-prefer-l2-by-name int>string ct-parser-current swap token-with-lexeme list-new swap list-append ct-parser-inject end immediate word main probe puti cr end" eval_program drop
   # A name with no definition pushes 0.
   "import stdlib.sl word probe \"no_such_word\" ct-definition-prefer-l2-by-name int>string ct-parser-current swap token-with-lexeme list-new swap list-append ct-parser-inject end immediate word main probe puti cr end" eval_program drop
   # use-l2-ct is retired: the body fails, printing 0
   "import stdlib.sl word myword 6 end word probe \"myword\" use-l2-ct end immediate word main probe end" eval_program 0 == puti cr
   "c vm definitions ok" puts cr
   0
end
