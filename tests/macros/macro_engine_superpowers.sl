import stdlib.sl

macro twice (x) `$x $x +` emit ;

macro add_named (lhs rhs) `$lhs $rhs +` emit ;

macro sum3 (a b c) `$a $b + $c +` emit ;

# A quotation argument is one value however many tokens it holds.
macro emit_all (code) $code emit ;

word setup_ct_macros
  `macro sum2ct (x y) \` \$x \$y + \` emit ;` emit
end
compile-time setup_ct_macros

word main
  twice 21 puti cr
  add_named 40 2 puti cr
  add_named 10 32 puti cr
  sum3 `20 1 +` 10 11 puti cr

  emit_all `1 2 3 4`
  + + + puti cr

  emit_all `5 6 7 8` + + + puti cr
  sum2ct 20 22 puti cr
end
