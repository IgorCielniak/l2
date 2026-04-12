import stdlib.sl

macro simplify
  $x:int + 0 => $x ;
  0 + $x:int => $x ;
  $x:int + $x:int => $x 2 * ;
;

macro unwrap
  ( $x $*rest ) => $x $*rest ;
;

word main
  simplify 9 + 0 puti cr
  simplify 0 + 11 puti cr
  simplify 7 + 7 puti cr
  unwrap ( 1 2 3 4 ) + + + puti cr
end
