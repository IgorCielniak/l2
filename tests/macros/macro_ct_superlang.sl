import stdlib.sl

# CT helpers receive macro arguments explicitly on the data stack.
word token-number token-lexeme try-string>int drop end compile-only

# Emit one number token anchored at a template token.
# [*, template | n] -> [*]
word emit-number
  int>string token-with-lexeme list-new swap list-append list>syntax emit
end
compile-only

macro via_ct (head tail)
  $head
  $head token-number $tail token-number +
  emit-number
;

macro last_item (items)
  $items -1 syntax-get list-new swap list-append list>syntax emit
;

macro pm_simplify
  match
    [$x:int, +, 0] => `$x` emit
    [0, +, $x:int] => `$x` emit
  end
;

word main
  via_ct 40 2 puti cr
  last_item `5 7 9` puti cr
  pm_simplify 0 + 33 puti cr
  pm_simplify 44 + 0 puti cr
end
