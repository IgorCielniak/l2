extern eval_program 2 1

import stdlib.sl

# Each eval_program body is parsed and run by main.c's compile-time VM.
word main
   # The fixed backtick rule: x`\` tokenizes as x, `, and \` (one escaped
   # backtick token).  The probe checks all three lexemes and injects 3.
   "import stdlib.sl word probe ct-parser-consume token-lexeme \"x\" string-equal? ct-parser-consume token-lexeme \"`\" string-equal? + ct-parser-consume token-lexeme \"\\\\`\" string-equal? + ct-parser-current swap int>string token-with-lexeme list-new swap list-append ct-parser-inject end immediate word main probe x`\\` puti cr end" eval_program drop
   # ; is a separator before letters too: ;end is ; then end, so the probe
   # sees ; and injects 1
   "import stdlib.sl word probe ct-parser-consume token-lexeme \";\" string-equal? ct-parser-consume drop ct-parser-current swap int>string token-with-lexeme list-new swap list-append ct-parser-inject end immediate word main probe ;end puti cr end" eval_program drop
   # add-token and add-token-chars are retired: each body fails, printing 0
   "import stdlib.sl word probe \"@\" add-token end immediate word main probe end" eval_program 0 == puti cr
   "import stdlib.sl word probe \"@\" add-token-chars end immediate word main probe end" eval_program 0 == puti cr
   # a frontend header is a clear error in the C compiler: prints 0
   "frontend \"lang.sl\"\nimport stdlib.sl word main end" eval_program 0 == puti cr
   "c vm tokenizer ok" puts cr
   0
end
