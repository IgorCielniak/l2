# Repetition groups in match patterns and quotations.
import stdlib.sl

# sum-all 1 2 3 ;  ->  0 1 + 2 + 3 +
macro sum-all
  match
    [ $( $x:int )*, \; ] => `0 $( $x + )*` emit
  end
;

# pairs a = 1 , b = 2 end  ->  defines words a and b
macro pairs
  match
    [ $( $k, =, $v:int ),+, end ] => `$( word $k $v end )*` emit
  end
;

# list-of 1 , 2 , 3 ;  -> separator form
macro count-items
  match
    [ $( $x:int ),*, \; ] => `0 $( $x drop 1 + )*` emit
  end
;

pairs a = 10 , b = 32 end

word main
  sum-all 1 2 3 ; puti cr
  sum-all ; puti cr
  a b + puti cr
  count-items 7 , 8 , 9 ; puti cr
end
