import stdlib.sl

macro fn_fold_sum (*items)
  ct-fn add do
    $acc $item +
  end
  ct-fold acc item in items with 0 do
    ct-call add
  end
;

macro fn_rebind (x *rest)
  ct-fn choose do
    $x
  end
  ct-if has rest then
    ct-fn choose do
      99
    end
  end
  ct-call choose
;

word main
  fn_fold_sum(2, 3, 4) puti cr
  fn_fold_sum(7) puti cr
  fn_rebind(5) puti cr
  fn_rebind(5, 1) puti cr
end
