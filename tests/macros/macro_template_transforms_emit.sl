import stdlib.sl

macro emit_list_sum (*items)
  ct-emit-list $items
;

macro emit_block_square (x)
  ct-emit-block do
    $x
    dup
    *
  end
;

macro transform_lower_call (fn value)
  $value
  $fn|lower
;

macro transform_expr_check (txt)
  ct-if $txt|upper == "HELLO" then
    1
  else
    0
  end
;

macro comment_ignored (x)
  ct-comment
    $x 1000 +
  ct-endcomment
  $x
;

word main
  emit_list_sum(3, 5, +) puti cr
  emit_block_square(6) puti cr
  transform_lower_call(PUTI, 9) cr
  transform_expr_check(hello) puti cr
  transform_expr_check(world) puti cr
  comment_ignored(12) puti cr
end
