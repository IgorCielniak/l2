import stdlib.sl
import meta.sl

word setup_meta_helpers
    "meta_const_x" "77" meta-define-const-if-missing
    "meta_alias_x" "meta_const_x" meta-define-alias-if-missing
    "meta_const_x" "88" meta-define-const-if-missing
    "meta_alias_x" "missing_symbol" meta-define-alias-if-missing

    "kwx_rule"
    list-new "kwx" list-append
    list-new "42" list-append
    meta-grammar-upsert-seq drop
    "kwx_rule" meta-grammar-remove-if-exists static_assert
    "kwx_rule"
    list-new "kwx" list-append
    list-new "42" list-append
    meta-grammar-upsert-seq drop

    "rw_rule"
    list-new "ra" list-append
    list-new "rb" list-append
    meta-reader-upsert-seq drop
    "rw_rule" meta-reader-remove-if-exists static_assert
    "rw_rule" meta-reader-remove-if-exists 0 == static_assert

    "twiceX" "dup" meta-macro-register-unary-op
    "macro_const_val" "123" meta-macro-register-const

    "left" "right" meta-inject-lexeme-pair
    meta-next-lexeme "left" string= static_assert
    meta-next-lexeme "right" string= static_assert

    list-new
    "alpha" meta-token list-append
    "beta" meta-token list-append
    inject-tokens
    meta-next-ident-lexeme "alpha" string= static_assert
    "beta" meta-expect-lexeme

    meta-current-line 0 >= static_assert
    meta-current-column 0 >= static_assert
end
compile-time setup_meta_helpers

word kw_value kwx end

word main
    meta_alias_x puti cr
    kw_value puti cr
    macro_const_val puti cr
    twiceX(9) + puti cr
end
