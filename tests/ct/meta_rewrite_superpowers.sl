import stdlib.sl

# The handle of the only grammar rule with this name, found through the
# rule listing.
word grammar-handle   # [* | name] -> [* | handle]
  >r ct-rewrite-list-grammar
  begin
    list-pop-front drop
    dup "name" map-get drop r@ string-equal? if
      "handle" map-get drop nip rdrop exit
    end
    drop
  again
end

word setup-meta
  # Grammar rewrites with named rules and explicit priorities.
  "tw-low"
  list-new "tw" list-append
  list-new "2" list-append
  ct-rewrite-add-grammar-named drop

  "tw-high"
  list-new "tw" list-append
  list-new "3" list-append
  ct-rewrite-add-grammar-named
  10 ct-rewrite-rule-set-priority static-assert

  # Reader rewrite rule and rule controls.
  "coldup"
  list-new "::" list-append
  list-new "dup" list-append
  ct-rewrite-add-reader-named
  7 ct-rewrite-rule-set-priority static-assert

  # A macro generated with ordinary macro syntax.
  `macro twice (x) \` \$x \$x + \` emit ;` emit

  # Introspection checks, by handle and by unique name.
  "tw-high" grammar-handle ct-rewrite-rule-get-enabled static-assert static-assert
  "grammar" "tw-high" ct-rewrite-rule-get-priority static-assert 10 == static-assert
  "reader" "coldup" ct-rewrite-rule-get-enabled static-assert static-assert
  "reader" "coldup" ct-rewrite-rule-get-priority static-assert 7 == static-assert
  "reader" "missing" ct-rewrite-rule-get-priority 0 == static-assert nil? static-assert

  ct-expansion-get-depth-limit dup 1 >= static-assert ct-expansion-set-depth-limit
end
compile-time setup-meta

word val_a tw end

word disable-high
  "tw-high" grammar-handle 0 ct-rewrite-rule-set-enabled static-assert
end
compile-time disable-high

word val_b tw end

word enable-high
  "tw-high" grammar-handle 1 ct-rewrite-rule-set-enabled static-assert
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
