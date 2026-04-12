import stdlib.sl
import meta.sl

word pd_emit_x
  "captures" map-get static_assert
  "x" map-get static_assert
end
use-l2-ct
compile-only

word pd_fail
  "part-d-ct-call-failure" parse-error
end
use-l2-ct
compile-only

macro pd_text (x y)
  $x $y +
;

macro pd_identity (x)
  ct-call pd_emit_x
;

macro pd_fail_soft
  9
  ct-call pd_fail
;

word setup_part_d
  "pd_text" meta-macro-is-text static_assert

  "pd_text" meta-macro-expansion-get
  static_assert
  dup list-length 3 == static_assert
  drop

  list-new "$x" list-append "$y" list-append "*" list-append
  "pd_text" swap meta-macro-expansion-set static_assert

  "pd_text" "pd_text_clone" meta-macro-clone static_assert
  "pd_text_clone" "pd_text_renamed" meta-macro-rename static_assert
  "pd_text_renamed" meta-word-exists static_assert
  "pd_text_clone" meta-word-exists 0 == static_assert

  "pd_text_renamed" "part d doc" meta-macro-doc-set static_assert
  "pd_text_renamed" meta-macro-doc-get
  static_assert
  "part d doc" string= static_assert

  map-new
  "tier" "core" map-set
  "stable" 1 map-set
  "pd_text_renamed" swap meta-macro-attrs-set static_assert
  "pd_text_renamed" meta-macro-attrs-get
  static_assert
  dup "tier" map-get static_assert
  swap drop
  "core" string= static_assert
  drop

  map-new
  "arg_kind" "capture-context" map-set
  "result_shape" "single-token" map-set
  "pd_emit_x" swap meta-ct-call-contract-set static_assert
  "pd_emit_x" meta-ct-call-contract-get
  static_assert
  dup "result_shape" map-get static_assert
  swap drop
  "single-token" string= static_assert
  drop

  "warn" meta-ct-call-exception-policy-set
  meta-ct-call-exception-policy-get "warn" string= static_assert

  "allowlist" meta-ct-call-sandbox-mode-set
  list-new "pd_emit_x" list-append "pd_fail" list-append
  meta-ct-call-sandbox-allowlist-set
  dup 2 == static_assert
  drop
  meta-ct-call-sandbox-allowlist-get "pd_emit_x" list-contains? static_assert

  12345 meta-ct-rand-seed
  100 meta-ct-rand-int
  12345 meta-ct-rand-seed
  100 meta-ct-rand-int
  == static_assert

  12345 meta-ct-rand-seed
  1 10 meta-ct-rand-range
  12345 meta-ct-rand-seed
  1 10 meta-ct-rand-range
  == static_assert

  1 meta-ct-call-memo-set
  meta-ct-call-memo-get static_assert
  meta-ct-call-memo-clear drop
  meta-ct-call-memo-size 0 == static_assert

  1 meta-ct-call-side-effects-set
  meta-ct-call-side-effects-get static_assert
  meta-ct-call-side-effects-clear drop

  4 meta-ct-call-recursion-limit-set
  meta-ct-call-recursion-limit-get 4 == static_assert

  50 meta-ct-call-timeout-ms-set
  meta-ct-call-timeout-ms-get 50 == static_assert
  0 meta-ct-call-timeout-ms-set

  "empty" meta-ct-call-exception-policy-set
  "allowlist" meta-ct-call-sandbox-mode-set
end
compile-time setup_part_d

word main
  pd_text(3, 4) puti cr
  pd_text_renamed(2, 5) puti cr
  pd_identity(7) puti cr
  pd_identity(7) puti cr
  pd_identity(7) puti cr
  1 puti cr
end

word verify_part_d
  meta-ct-call-memo-size 1 >= static_assert
  meta-ct-call-side-effects-log dup list-length 1 >= static_assert drop
  meta-ct-call-side-effects-clear dup 1 >= static_assert drop

  "raise" meta-ct-call-exception-policy-set
  "off" meta-ct-call-sandbox-mode-set
  0 meta-ct-call-memo-set
  0 meta-ct-call-side-effects-set
end
compile-time verify_part_d
