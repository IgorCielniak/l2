import stdlib.sl

word setup-meta
  # Grammar rewrites with named rules and explicit priorities.
  "tw-low"
  list-new "tw" list-append
  list-new "2" list-append
  ct-add-grammar-rewrite-named drop

  "tw-high"
  list-new "tw" list-append
  list-new "3" list-append
  ct-add-grammar-rewrite-named drop
  "tw-high" 10 ct-set-grammar-rewrite-priority static_assert

  # Reader rewrite rule and rule controls.
  "coldup"
  list-new "::" list-append
  list-new "dup" list-append
  ct-add-reader-rewrite-named drop
  "coldup" 7 ct-set-reader-rewrite-priority static_assert

  # Programmatic text macro registration.
  "twice" 1
  list-new
  "$0" list-append
  "$0" list-append
  "+" list-append
  ct-register-text-macro

  # Introspection checks.
  "tw-high" ct-get-grammar-rewrite-enabled static_assert static_assert
  "tw-high" ct-get-grammar-rewrite-priority static_assert 10 == static_assert
  "coldup" ct-get-reader-rewrite-enabled static_assert static_assert
  "coldup" ct-get-reader-rewrite-priority static_assert 7 == static_assert

  ct-get-macro-expansion-limit dup 1 >= static_assert ct-set-macro-expansion-limit
  1 ct-set-macro-preview
  ct-get-macro-preview static_assert
  0 ct-set-macro-preview
end
compile-time setup-meta

word val_a tw end

word disable-high
  "tw-high" 0 ct-set-grammar-rewrite-enabled static_assert
end
compile-time disable-high

word val_b tw end

word enable-high
  "tw-high" 1 ct-set-grammar-rewrite-enabled static_assert
end
compile-time enable-high

word val_c tw end

word main
  val_a puti cr
  val_b puti cr
  val_c puti cr
  21 :: + puti cr
  twice 21 puti cr
end
