import stdlib.sl

macro classify_num (x)
  ct-switch $x do
    ct-case 0 do
      100
    end
    ct-case 1 do
      200
    end
    ct-default do
      300
    end
  end
;

macro classify_pair (a b)
  ct-switch $a $b do
    ct-case 1 2 do
      12
    end
    ct-case 3 4 do
      34
    end
    ct-default do
      99
    end
  end
;

macro classify_shadow (x)
  ct-let v $x do
    ct-switch $v do
      ct-case 7 do
        ct-let v 2 do
          $v
        end
      end
      ct-default do
        $v
      end
    end
  end
;

word main
  classify_num(0) puti cr
  classify_num(5) puti cr
  classify_pair(1, 2) puti cr
  classify_pair(5, 6) puti cr
  classify_shadow(7) puti cr
  classify_shadow(9) puti cr
end
