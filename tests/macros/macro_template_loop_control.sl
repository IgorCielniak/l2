import stdlib.sl

macro add_optional (x *rest)
  $x
  ct-when has rest
    100 +
  end
  ct-unless has rest
    1 +
  end
;

macro sum_skip_first (*items)
  0
  ct-each item in items do
    ct-if first then
      ct-continue
    end
    $item +
  end
;

macro sum_plus_indices (*items)
  0
  ct-each idx item in items do
    $item +
    $idx +
  end
;

macro last_index_or_neg1 (*items)
  ct-if empty items then
    -1
  else
    ct-each idx item in items sep drop do
      $idx
    end
  end
;

macro first_only_or_zero (*items)
  ct-if empty items then
    0
  else
    ct-for item in items do
      $item
      ct-break
      999
    end
  end
;

word main
  add_optional(5, 9) puti cr
  add_optional(5) puti cr
  sum_skip_first(4, 7, 11) puti cr
  sum_plus_indices(4, 7, 11) puti cr
  last_index_or_neg1(8, 9, 10) puti cr
  last_index_or_neg1() puti cr
  first_only_or_zero(8, 9, 10) puti cr
  first_only_or_zero() puti cr
end
