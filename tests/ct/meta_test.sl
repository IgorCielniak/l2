import stdlib.sl
import meta.sl

word setup_meta
    "dupalias" "dup" meta-reader-alias drop
    "dupalias" 7 meta-reader-priority-set static_assert
    "dupalias" meta-reader-priority-get static_assert 7 == static_assert

    "sum2" "+" meta-macro-register-binary-op
    "forty_two" "42" meta-macro-register-alias

    "meta_tmp" "111" meta-define-const

    "forty_two_word" "forty_two" meta-define-alias

    "kw-rule" "kw" "9" meta-grammar-upsert-word drop

    list-new
    "aa" meta-token list-append
    "bb" meta-token list-append
    ";" meta-token list-append
    inject-tokens
    ";" meta-collect-until
    dup list-length 2 == static_assert
    dup 0 list-get token-lexeme "aa" string= static_assert
    1 list-get token-lexeme "bb" string= static_assert
end
compile-time setup_meta

word verify_meta_tmp
    "meta_tmp" meta-word-exists static_assert
    "meta_tmp" meta-word-drop-if-exists static_assert
    "meta_tmp" meta-word-exists 0 == static_assert
end
compile-time verify_meta_tmp

word kw_value kw end

word main
    10 dupalias + puti cr
    sum2(20, 22) puti cr
    forty_two_word puti cr
    kw_value puti cr
end
