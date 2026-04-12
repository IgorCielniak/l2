import stdlib.sl
import meta.sl

word ctx_schema_validate
  ct-capture-schema-validate drop
end
use-l2-ct
compile-only

word ctx_rest_count
  "rest" ct-capture-get
  static_assert
  ct-capture-count
end
use-l2-ct
compile-only

word ctx_rest_join
  "rest" ct-capture-get
  static_assert
  "," ct-capture-join
end
use-l2-ct
compile-only

word ctx_lifetime_ok
  dup ct-capture-lifetime-live? static_assert
  ct-capture-lifetime
  dup 0 > static_assert
  drop
  1
end
use-l2-ct
compile-only

word ctx_origin_depth
  ct-capture-origin
  "expansion_depth" map-get static_assert
  swap drop
  drop
  1
end
use-l2-ct
compile-only

word ctx_namespace_probe
  dup ct-capture-args map-length 1 >= static_assert
  dup ct-capture-locals map-length 0 >= static_assert
  ct-capture-globals map-length 1 >= static_assert
end
use-l2-ct
compile-only

word ctx_taint_probe
  "rest" ct-capture-tainted? static_assert
end
use-l2-ct
compile-only

macro typed_add (a b)
  ct-call ctx_schema_validate
  $a:int $b:int +
;

macro rest_stats (head *rest)
  ct-call ctx_namespace_probe
  ct-call ctx_taint_probe
  ct-call ctx_rest_count
;

macro rest_join_csv (head *rest)
  ct-call ctx_rest_join
;

macro lifetime_ok (x)
  ct-call ctx_lifetime_ok
;

macro origin_depth (x)
  ct-call ctx_origin_depth
;

word setup_capture_hygiene
  "g" ct-gensym
  dup "g" string-starts-with? static_assert
  "g" ct-gensym
  swap string= 0 == static_assert

  "typed_add" "a" "single" "int" 1 ct-capture-schema-put
  "typed_add" "b" "single" "int" 1 ct-capture-schema-put

  "typed_add" "a" ct-capture-freeze
  "typed_add" "a" ct-capture-mutable? 0 == static_assert
  "typed_add" "a" ct-capture-thaw drop
  "typed_add" "a" ct-capture-mutable? static_assert

  "rest_stats" "rest" 1 ct-capture-taint-set

  "gkey" list-new "99" list-append ct-capture-global-set
  "gkey" ct-capture-global-get
  static_assert
  dup ct-capture-count 1 == static_assert
  drop

  list-new
  "a" list-append
  "b" list-append
  dup ct-capture-shape "tokens" string= static_assert
  dup ct-capture-count 2 == static_assert
  dup 0 1 ct-capture-slice
  dup ct-capture-count 1 == static_assert
  drop
  dup "upper" ct-capture-map
  dup 0 list-get "A" string= static_assert
  drop
  dup "identifier" ct-capture-filter
  ct-capture-count 2 == static_assert

  list-new
  list-new "x" list-append list-append
  list-new "y" list-append list-append
  "|" ct-capture-separate
  dup ct-capture-count 3 == static_assert
  drop

  list-new
  list-new "x" list-append list-append
  list-new "y" list-append list-append
  "," ct-capture-join
  "x,y" string= static_assert

  list-new "123" list-append
  ct-capture-coerce-number
  static_assert
  123 == static_assert

  list-new
  "10" list-append
  "20" list-append
  dup ct-capture-clone
  ct-capture-equal? static_assert

  dup ct-capture-hash
  string-length 64 == static_assert

  dup ct-capture-serialize
  dup dup ct-capture-compress ct-capture-decompress
  string= static_assert
  ct-capture-deserialize
  ct-capture-count 2 == static_assert
  drop

  list-new "x" list-append
  list-new "y" list-append
  ct-capture-diff
  list-empty? 0 == static_assert

  ct-capture-replay-clear drop
end
compile-time setup_capture_hygiene

word main
  typed_add(20, 22) puti cr
  rest_stats(0, 7, 8, 9) puti cr
  lifetime_ok(1) puti cr
  origin_depth(1) puti cr
end

word verify_capture_replay
  ct-capture-replay-log
  dup list-length 5 >= static_assert
  dup 0 list-get "origin" map-get static_assert
  swap drop
  drop
end
compile-time verify_capture_replay
