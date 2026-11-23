extern eval_program 2 1

import stdlib.sl

# Each eval_program body is parsed and run by main.c's compile-time VM.  The
# immediate `probe` words run while the body is parsed and inject a literal
# that `main` prints at run time.
word main
   # ct-parser-consume returns the next token and makes it the current token:
   # both checks pass, so the probe injects 20 and main prints 21
   "import stdlib.sl word probe ct-parser-consume token-lexeme \"x\" string-equal? ct-parser-current token-lexeme \"x\" string-equal? + 10 * ct-parser-current swap int>string token-with-lexeme list-new swap list-append ct-parser-inject end immediate word main probe x 1 + puti cr end" eval_program drop
   # ct-parser-peek looks ahead without consuming; nil past the end; ct-parser-eof? is 0: 3
   "import stdlib.sl word probe ct-parser-current 0 ct-parser-peek token-lexeme \"puti\" string-equal? 1000 ct-parser-peek nil? + ct-parser-eof? 0 == + int>string token-with-lexeme list-new swap list-append ct-parser-inject end immediate word main probe puti cr end" eval_program drop
   # Failing bodies make eval_program fail, so each of these prints 0.
   # ct-parser-consume at end of input: eval_program appends a `main` call,
   # so the probe's first consume takes it and the second finds no token
   "import stdlib.sl word probe ct-parser-consume drop ct-parser-consume drop end immediate word main end probe" eval_program 0 == puti cr
   # ct-parser-inject accepts only tokens
   "import stdlib.sl word probe list-new \"7\" list-append ct-parser-inject end immediate word main probe end" eval_program 0 == puti cr
   # negative ct-parser-peek offsets
   "import stdlib.sl word probe -1 ct-parser-peek drop end immediate word main probe end" eval_program 0 == puti cr
   "c vm parser stream ok" puts cr
   0
end
