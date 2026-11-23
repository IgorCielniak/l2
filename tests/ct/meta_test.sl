import stdlib.sl
import meta.sl

word setup_meta
    "dupalias" "dup" meta-reader-alias
    dup 7 ct-rewrite-rule-set-priority static-assert
    ct-rewrite-rule-get-priority static-assert 7 == static-assert
    "reader" "dupalias" ct-rewrite-rule-get-priority static-assert 7 == static-assert

    "sum2" "+" meta-macro-register-binary-op
    `const forty_two 42` emit

    "meta_tmp" "111" meta-define-const

    "forty_two_word" "forty_two" meta-define-alias

    nil "kw-rule" "kw" "9" meta-grammar-upsert-word drop

    list-new
    "aa" meta-token list-append
    "bb" meta-token list-append
    ";" meta-token list-append
    ct-parser-inject
    ";" meta-collect-until
    dup list-length 2 == static-assert
    dup 0 list-get token-lexeme "aa" string-equal? static-assert
    1 list-get token-lexeme "bb" string-equal? static-assert
end
compile-time setup_meta

word verify_meta_tmp
    "meta_tmp" ct-definition-exists? static-assert
    "meta_tmp" meta-word-drop-if-exists static-assert
    "meta_tmp" ct-definition-exists? 0 == static-assert
end
compile-time verify_meta_tmp

word kw_value kw end

word main
    10 dupalias + puti cr
    sum2 20 22 puti cr
    forty_two_word puti cr
    kw_value puti cr
end
