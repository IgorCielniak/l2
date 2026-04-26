# Compile-time lambda helper.
#
# Usage:
#   lambda(
#       1 2 +
#   ) call
#
# Expands to:
#   - emit word pointer for generated lambdaN symbol at call site
#   - append generated top-level definition at end of token stream

word lambda-token-list->lexeme-list
    list-new swap
begin
    dup list-empty? if
        drop
        exit
    end
    list-pop-front
    token-lexeme
    swap >r
    list-append
    r>
again
end
compile-only

word lambda-append-generated-definition
    # name body_tokens template --
    >r
    lambda-token-list->lexeme-list
    list-new
    "word" list-append
    2 pick list-append
    swap list-extend
    "end" list-append
    swap drop
    r>
    ct-parser-pos ct-parser-remaining + ct-parser-set-pos
    >r
    inject-lexemes
    r> ct-parser-set-pos drop
end
compile-only

word lambda
    ct-current-token >r

    next-token dup nil? if
        drop
        rdrop
        "lambda requires '(...)' body" parse-error
    end
    dup token-lexeme "(" string= 0 == if
        drop
        rdrop
        "lambda expects '(' after keyword" parse-error
    end
    drop

    "(" ")" ct-parser-collect-balanced
    dup 0 == if
        drop drop
        rdrop
        "unterminated lambda body: missing ')'" parse-error
    end
    drop

    "lambda" ct-gensym
    "_" "" string-replace
    dup "word_ptr" swap ct-emit-op

    swap r>
    lambda-append-generated-definition
end
immediate
compile-only
