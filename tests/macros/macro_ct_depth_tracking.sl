import stdlib.sl

# Sum the numbers in a quotation (0 when it is empty).
macro ct_sum_or_zero (items)
  $items syntax-length 0 == if
    `0` emit
  else
    $items 0 1 syntax-slice emit
    $items syntax>list list-pop-front drop drop
    begin
      dup list-empty? if drop break end
      list-pop-front drop list-new swap list-append list>syntax emit
      `+` emit
    again
  end
;

# Define a word returning the body's value, or 0 for an empty body.
macro ct_define_runtime_word (name body)
  $body syntax-length 0 == if
    `word $name 0 end` emit
  else
    `word $name 1 if $body else 0 end end` emit
  end
;

ct_define_runtime_word dyn_a `7`
ct_define_runtime_word dyn_b ``

word main
  ct_sum_or_zero `2 3 4` puti cr
  ct_sum_or_zero `` puti cr
  dyn_a puti cr
  dyn_b puti cr
end
