import stdlib.sl

macro let_double (x)
  ct-let y $x do
    $y $y +
  end
;

macro let_shadow_mix (x)
  ct-let v $x do
    ct-let v 3 do
      $v
    end
    $v +
  end
;

macro let_variadic_alias (head *tail)
  $head
  ct-let rest $*tail do
    ct-for item in rest do
      $item +
    end
  end
;

macro let_expr_tokens (x)
  ct-let expr $x 1 + do
    $expr
  end
;

word main
  let_double(9) puti cr
  let_shadow_mix(7) puti cr
  let_variadic_alias(1, 2, 3, 4) puti cr
  let_expr_tokens(4) puti cr
end
