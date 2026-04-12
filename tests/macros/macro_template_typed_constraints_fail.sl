import stdlib.sl

macro typed_bad (a b)
  $a:int $b:int +
;

word main
  typed_bad(aa, 2) puti cr
end
