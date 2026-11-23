import stdlib.sl

macro simplify
  match
    [$x:int, +, 0] => `$x` emit
    [0, +, $x:int] => `$x` emit
    [$x:int, +, $x] => `$x 2 *` emit
  end
;

macro unwrap
  match
    [(, $a, $b, $c, $d, )] => `$a $b $c $d` emit
  end
;

word main
  simplify 9 + 0 puti cr
  simplify 0 + 11 puti cr
  simplify 7 + 7 puti cr
  unwrap ( 1 2 3 4 ) + + + puti cr
end
