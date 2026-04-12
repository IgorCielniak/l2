import stdlib.sl

macro fold_sum (*items)
  ct-fold acc item in items with 0 do
    $acc $item +
  end
;

macro fold_skip_first (*items)
  ct-fold acc item in items with 0 do
    ct-if first then
      ct-continue
    end
    $acc $item +
  end
;

macro fold_first_only (*items)
  ct-fold acc item in items with 0 do
    ct-if not first then
      ct-break
    end
    $acc $item +
  end
;

macro fold_with_head (head *tail)
  ct-fold acc item in tail with $head do
    $acc $item +
  end
;

macro fold_with_match (*items)
  ct-fold acc item in items with 0 do
    ct-match $item do
      ct-case 0 then
        $acc
      ct-default then
        $acc $item +
    end
  end
;

word main
  fold_sum(2, 3, 4) puti cr
  fold_sum() puti cr
  fold_skip_first(4, 7, 11) puti cr
  fold_first_only(9, 8, 7) puti cr
  fold_with_head(1, 2, 3, 4) puti cr
  fold_with_match(1, 0, 2, 0, 3) puti cr
end
