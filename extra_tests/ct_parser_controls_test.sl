import stdlib/stdlib.sl
import stdlib/io.sl

word ct_parser_controls_checks
    ct-parser-checkpoint >r

    r@ dup "pos" map-get static_assert
    swap drop
    ct-parser-pos == static_assert

    ct-parser-tail list-length 1 >= static_assert
    ct-parser-eof? 0 == static_assert

    0 ct-parser-peek dup nil? 0 == static_assert
    token-lexeme >r
    next-token token-lexeme r> string= static_assert
    r@ ct-parser-restore static_assert

    ct-parser-session-begin drop
    next-token drop
    ct-parser-session-rollback static_assert

    ct-parser-session-begin drop
    list-new "(" list-append
    "1" list-append
    "(" list-append
    "2" list-append
    ")" list-append
    ")" list-append
    ct-current-token inject-lexemes
    next-token drop
    "(" ")" ct-parser-collect-balanced
    dup static_assert
    swap
    dup list-length 4 == static_assert
    drop
    drop
    ct-parser-session-rollback static_assert

    0 ct-parser-peek dup token-lexeme swap token-clone token-lexeme string= static_assert
    0 ct-parser-peek "renamed_word" token-with-lexeme token-lexeme "renamed_word" string= static_assert

    0 ct-parser-peek token-column
    0 ct-parser-peek 3 token-shift-column token-column
    swap 3 + == static_assert

    ct-parser-session-begin drop
    "m0" ct-parser-mark drop drop
    next-token drop
    next-token drop
    "m1" ct-parser-mark drop drop
    "m0" "m1" ct-parser-diff
    dup dup "delta" map-get static_assert
    swap drop
    2 == static_assert
    dup "count" map-get static_assert
    swap drop
    2 == static_assert
    drop
    ct-parser-session-rollback static_assert

    0 ct-parser-peek token-lexeme
    dup >r
    list-new swap list-append
    ct-parser-expected token-lexeme
    r> string= static_assert

    ct-rewrite-scope-push drop
    "ct.parser.tmp.rule"
    list-new "kwx" list-append
    list-new "123" list-append
    ct-add-grammar-rewrite-named drop
    "grammar" "ct.parser.tmp.rule" "ct.parser.tmp" ct-set-rewrite-pipeline drop
    "grammar" "ct.parser.tmp" 1 ct-set-rewrite-pipeline-active
    "grammar" ct-rebuild-rewrite-index drop
    "grammar" list-new "kwx" list-append ct-rewrite-run-on-list
    dup list-length 1 >= static_assert
    drop
    dup list-length 1 == static_assert
    0 list-get "123" string= static_assert
    drop
    ct-rewrite-scope-pop static_assert

    r> ct-parser-restore static_assert
end
compile-time ct_parser_controls_checks

word ct_parser_sentinel
    7
end

word ct_parser_injected
    99
end

word main
    ct_parser_injected puti cr
    ct_parser_sentinel puti cr
end
