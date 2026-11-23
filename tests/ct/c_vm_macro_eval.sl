extern eval_program 2 1

import stdlib.sl

# Each eval_program body is parsed and run by main.c's compiler.
word main
   # A quotation macro with parameters (the form stdlib uses).
   "import stdlib.sl macro add3 (a b c) `$a $b + $c +` emit ; word main add3 1 2 39 puti cr end" eval_program drop
   # A zero-parameter constant macro.
   "import stdlib.sl macro lit `42` emit ; word main lit puti cr end" eval_program drop
   # A general macro body: compile-time code that decides what to emit.
   "import stdlib.sl macro pick-one (x) $x token-lexeme \"0\" string-equal? if `100` emit else `$x 1 +` emit end ; word main pick-one 0 puti cr pick-one 6 puti cr end" eval_program drop
   # A quotation argument splices all its tokens.
   "import stdlib.sl macro twice (code) `$code $code` emit ; word main twice `3 4 +` + puti cr end" eval_program drop
   # $( ) splices computed values, with pasting.
   "import stdlib.sl macro getter (n) `word get-$( $n token-lexeme ) $( 6 7 * ) end` emit ; getter x word main get-x puti cr end" eval_program drop
   # A macro called from a macro body adds to the caller's output.
   "import stdlib.sl macro one `1` emit ; macro one-plus (x) one `$x +` emit ; word main one-plus 41 puti cr end" eval_program drop
   # An unbound splice is an error: the program fails, printing 1.
   "import stdlib.sl macro bad `$nope` emit ; word main bad end" eval_program 0 != puti cr
   "c vm macros ok" puts cr
   0
end
