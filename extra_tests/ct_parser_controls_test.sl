import stdlib/stdlib.sl
import stdlib/io.sl

# pc-tokens [*, lexemes | template] -> [* | tokens]
word pc-tokens
    >r list-new swap
    begin
        dup list-empty? if drop rdrop exit end
        list-pop-front drop
        r@ swap token-with-lexeme
        rot swap list-append swap
    again
end
compile-only

word ct_parser_controls_checks
    ct-parser-get-pos >r
    ct-parser-checkpoint >r

    ct-parser-tail list-length 1 >= static-assert
    ct-parser-eof? 0 == static-assert

    0 ct-parser-peek dup nil? 0 == static-assert
    token-lexeme >r
    ct-parser-consume token-lexeme r> string-equal? static-assert
    r@ ct-parser-restore static-assert
    ct-parser-get-pos 1 rpick == static-assert

    ct-parser-session-begin
    ct-parser-consume drop
    ct-parser-session-rollback static-assert

    ct-parser-session-begin >r
    list-new "(" list-append
    "1" list-append
    "(" list-append
    "2" list-append
    ")" list-append
    ")" list-append
    0 ct-parser-peek pc-tokens ct-parser-inject
    ct-parser-consume drop
    "(" ")" ct-parser-collect-balanced
    static-assert
    list-length 4 == static-assert
    r> ct-parser-session-rollback static-assert

    0 ct-parser-peek "renamed_word" token-with-lexeme token-lexeme "renamed_word" string-equal? static-assert

    0 ct-parser-peek token-column
    0 ct-parser-peek "renamed_word" token-with-lexeme token-column
    == static-assert

    ct-parser-session-begin >r
    ct-parser-get-pos
    ct-parser-consume drop
    ct-parser-consume drop
    ct-parser-get-pos ct-parser-slice
    list-length 2 == static-assert
    r> ct-parser-session-rollback static-assert

    0 ct-parser-peek token-lexeme
    dup >r
    list-new swap list-append
    ct-parser-expected static-assert
    token-lexeme r> string-equal? static-assert
    "no_such_lexeme" ct-parser-expected 0 == static-assert
    nil? static-assert

    ct-rewrite-scope-push drop
    "ct.parser.tmp.rule"
    list-new "kwx" list-append
    list-new "123" list-append
    ct-rewrite-add-grammar-named
    "ct.parser.tmp" ct-rewrite-rule-set-pipeline static-assert
    "grammar" "ct.parser.tmp" 1 ct-rewrite-set-pipeline-active
    "grammar" list-new "kwx" list-append ct-rewrite-run-on-list
    dup list-length 1 >= static-assert
    drop
    dup list-length 1 == static-assert
    0 list-get "123" string-equal? static-assert
    ct-rewrite-scope-pop static-assert

    r> ct-parser-restore static-assert
    ct-parser-get-pos r> == static-assert
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
