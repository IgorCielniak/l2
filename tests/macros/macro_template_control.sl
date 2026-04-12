import stdlib.sl

macro sum_tail (head *tail)
  $head
  ct-for part in tail do
    $part +
  end
;

macro maybe_add_one (x *rest)
  $x
  ct-if has rest then
    1 +
  else
    0 +
  end
;

macro tagged_sum (*items)
  ct-for item in items do
    $item
    ct-if not last then
      1
    end
  end
;

word main
  sum_tail(1, 2, 3, 4) puti cr
  sum_tail(9) puti cr
  maybe_add_one(5, 99) puti cr
  maybe_add_one(5) puti cr
  tagged_sum(2, 3, 4) + + + + puti cr
end
