import stdlib.sl
import meta.sl

# Emitted definitions are processed after the compile-time word that emits
# them, so the "if missing" checks run in a later step.
word define_meta_helpers
    "meta_const_x" "77" meta-define-const-if-missing
    "meta_alias_x" "meta_const_x" meta-define-alias-if-missing
end
compile-time define_meta_helpers

word setup_meta_helpers
    "meta_const_x" "88" meta-define-const-if-missing
    "meta_alias_x" "missing_symbol" meta-define-alias-if-missing

    # Upserts replace the previous registration through its handle.
    nil "kwx_rule"
    list-new "kwx" list-append
    list-new "41" list-append
    meta-grammar-upsert-seq
    "kwx_rule"
    list-new "kwx" list-append
    list-new "42" list-append
    meta-grammar-upsert-seq drop
    ct-rewrite-list-grammar list-length 1 == static-assert

    nil "rw_rule"
    list-new "ra" list-append
    list-new "rb" list-append
    meta-reader-upsert-seq
    dup ct-rewrite-remove static-assert
    ct-rewrite-remove 0 == static-assert

    "twiceX" "dup" meta-macro-register-unary-op
    `const macro_const_val 123` emit

    "left" "right" meta-inject-lexeme-pair
    meta-next-lexeme "left" string-equal? static-assert
    meta-next-lexeme "right" string-equal? static-assert

    list-new
    "alpha" meta-token list-append
    "beta" meta-token list-append
    ct-parser-inject
    meta-next-ident-lexeme "alpha" string-equal? static-assert
    "beta" meta-expect-lexeme

    meta-current-line 0 >= static-assert
    meta-current-column 0 >= static-assert
end
compile-time setup_meta_helpers

word kw_value kwx end

word main
    meta_alias_x puti cr
    kw_value puti cr
    macro_const_val puti cr
    twiceX 9 + puti cr
end
