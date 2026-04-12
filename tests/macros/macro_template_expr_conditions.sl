import stdlib.sl

macro expr_guard (x y)
  ct-if ($x < $y) and not ($x == 0) then
    1
  else
    0
  end
;

macro expr_when_unless (x)
  0
  ct-when $x >= 10 then
    100 +
  end
  ct-unless ($x == 7) then
    1 +
  end
;

macro expr_with_let (x)
  ct-let n $x do
    ct-if n > 5 and n < 9 then
      42
    else
      24
    end
  end
;

macro expr_const_branch
  ct-if (1 < 2) and (3 == 3) then
    9
  else
    0
  end
;

word main
  expr_guard(2, 3) puti cr
  expr_guard(0, 3) puti cr
  expr_guard(4, 1) puti cr
  expr_when_unless(10) puti cr
  expr_when_unless(7) puti cr
  expr_when_unless(3) puti cr
  expr_with_let(7) puti cr
  expr_with_let(9) puti cr
  expr_const_branch puti cr
end
