import stdlib.sl

# A macro that defines another macro writes the inner quotation with
# escapes: \` for its backticks and \$ for the inner macro's own parameters.
# $name splices the outer macro's parameters, and $( ) computed values.
macro def-adder (name n)
  `macro $name (x) \` \$x $n + \` emit ;` emit
;

# The generated macro's name is computed: def-scaled 3 defines times3.
macro def-scaled (n)
  `macro times$( $n token-lexeme ) (x) \` \$x $n * \` emit ;` emit
;

def-adder add5 5
def-adder add10 10
def-scaled 3

word main
  add5 1 puti cr
  add10 1 puti cr
  times3 7 puti cr
end
