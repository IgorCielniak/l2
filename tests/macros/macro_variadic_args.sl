import stdlib.sl
import macro_variadic.sl

# Variadic macro arguments.
# sum-of 1 2 3 end-sum  ->  1 2 + 3 +
macro sum-of ()
  "end-sum" variadic-rest syntax>list
  list-pop-front drop `$( )` emit
  dup list-empty? 0 == while do
    list-pop-front drop `$( ) +` emit
    dup list-empty? 0 ==
  end
  drop
;

# count( ... ) -> the number of tokens between the outer parentheses
macro count ()
  "(" ")" variadic-balanced variadic-count `$( )` emit
;

word main
  sum-of 1 2 3 end-sum puti cr
  sum-of 40 2 end-sum puti cr
  count( x ( y z ) w ) puti cr
end
