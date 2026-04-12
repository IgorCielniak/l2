import stdlib.sl

macro match_num (x)
  ct-match $x do
    ct-case 0 then
      100
    ct-case 1 then
      200
    ct-default then
      300
  end
;

macro match_pair (a b)
  ct-match $a $b do
    ct-case 1 2 then
      12
    ct-case 3 4 then
      34
    ct-default then
      99
  end
;

macro match_nested (x *rest)
  ct-match $x do
    ct-case 7 then
      ct-let y 5 do
        $y
      end
    ct-case 8 then
      88
    ct-default then
      ct-if has rest then
        1
      else
        0
      end
  end
;

word main
  match_num(0) puti cr
  match_num(9) puti cr
  match_pair(1, 2) puti cr
  match_pair(9, 9) puti cr
  match_nested(7) puti cr
  match_nested(6, 1) puti cr
  match_nested(6) puti cr
end
