import stdlib.sl
import meta.sl

word mctx-head
  "captures" map-get static_assert
  swap drop
  "head" map-get static_assert
  swap drop
end
use-l2-ct
compile-only

word mctx-tail-first
  "captures" map-get static_assert
  swap drop
  "tail" map-get static_assert
  swap drop
  0 list-get
end
use-l2-ct
compile-only

word setup-superlang-patterns
  "pm_simplify"
  list-new
  list-new "$x:int" list-append "+" list-append "0" list-append
  list-new "$x" list-append
  meta-macro-clauses-append
  list-new "0" list-append "+" list-append "$x:int" list-append
  list-new "$x" list-append
  meta-macro-clauses-append
  meta-macro-pattern-register
end
compile-time setup-superlang-patterns

macro via_ct (head *tail)
  ct-call mctx-head
  ct-call mctx-tail-first
  +
;

macro last_item (*items)
  ct-for item in items sep drop do
    $item
  end
;

word main
  via_ct(40, 2) puti cr
  last_item(5, 7, 9) puti cr
  pm_simplify 0 + 33 puti cr
  pm_simplify 44 + 0 puti cr
end
