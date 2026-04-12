import stdlib.sl

macro twice_legacy 1
  $0 $0 +
;

macro add_named (lhs rhs)
  $lhs $rhs +
;

macro sum3 (a b c)
  $a $b + $c +
;

macro emit_tail (head *tail)
  $head $*tail
;

word setup_ct_macros
  "sum2ct"
  list-new "x" list-append "y" list-append
  list-new "$x" list-append "$y" list-append "+" list-append
  ct-register-text-macro-signature
end
compile-time setup_ct_macros

word main
  twice_legacy 21 puti cr
  add_named 40 2 puti cr
  add_named(10, 32) puti cr
  sum3(20 1 +, 10, 11) puti cr

  emit_tail 1 2 3 4
  + + + puti cr

  emit_tail(5, 6, 7, 8) + + + puti cr
  sum2ct 20 22 puti cr
end
