import stdlib.sl

macro ct_sum_or_zero (*items)
  ct-if has items then
    ct-for x in items do
      ct-if first then
        $x
      else
        $x +
      end
    end
  else
    0
  end
;

macro ct_define_runtime_word (name *body)
  ct-if has body then
    word $name
      1 if
        $*body
      else
        0
      end
    end
  else
    word $name
      0
    end
  end
;

ct_define_runtime_word dyn_a 7
ct_define_runtime_word dyn_b

word main
  ct_sum_or_zero(2, 3, 4) puti cr
  ct_sum_or_zero() puti cr
  dyn_a puti cr
  dyn_b puti cr
end
