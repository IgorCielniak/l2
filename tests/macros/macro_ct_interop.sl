import stdlib.sl
import meta.sl

word pd_check token-lexeme "7" string-equal? static-assert end
compile-only

macro pd_text (x y) `$x $y +` emit ;

# A macro body is ordinary CT code: it can call helpers with its bindings.
macro pd_identity (x)
  $x pd_check
  `$x` emit
;

word setup_part_d
  "pd_text" ct-macro-get-by-name static-assert
  "body" map-get drop syntax-length 6 == static-assert

  "pd_text" map-new "body" `\` \$x \$y * \` emit` map-set ct-macro-set-by-name static-assert

  "pd_text" "pd_text_clone" ct-definition-clone-by-name static-assert drop
  "pd_text_clone" "pd_text_renamed" ct-definition-rename-by-name static-assert
  "pd_text_renamed" ct-definition-exists? static-assert
  "pd_text_clone" ct-definition-exists? 0 == static-assert

  "pd_text_renamed"
  map-new "metadata"
    map-new "doc" "part d doc" map-set "tier" "core" map-set
  map-set
  ct-macro-set-by-name static-assert
  "pd_text_renamed" ct-macro-get-by-name static-assert
  "metadata" map-get drop
  dup "doc" map-get drop "part d doc" string-equal? static-assert
  "tier" map-get drop "core" string-equal? static-assert

  12345 ct-rng-seed
  100 ct-rng-int
  12345 ct-rng-seed
  100 ct-rng-int
  == static-assert

  12345 ct-rng-seed
  1 10 ct-rng-range
  12345 ct-rng-seed
  1 10 ct-rng-range
  == static-assert
end
compile-time setup_part_d

word main
  pd_text 3 4 puti cr
  pd_text_renamed 2 5 puti cr
  pd_identity 7 puti cr
  pd_identity 7 puti cr
  pd_identity 7 puti cr
  1 puti cr
end
