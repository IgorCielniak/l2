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

word lambda-append-generated-definition
    # name body_tokens template --
    # Appends `word <name> <body> end` at the end of the token stream. The
    # body keeps its own tokens; the name is anchored at the 'lambda' token.
    >r swap r> swap token-with-lexeme           # body nameTok
    `word $(dup) $(over) end` syntax>list       # body nameTok tokens
    nip nip
    ct-parser-get-pos ct-parser-remaining + ct-parser-set-pos
    >r
    ct-parser-inject
    r> ct-parser-set-pos drop
end
compile-only

word lambda
    ct-parser-current >r

    ct-parser-eof? if
        rdrop
        "lambda requires '(...)' body" ct-parser-error
    end
    ct-parser-consume token-lexeme "(" string-equal? 0 == if
        rdrop
        "lambda expects '(' after keyword" ct-parser-error
    end

    "(" ")" ct-parser-collect-balanced
    dup 0 == if
        drop drop
        rdrop
        "unterminated lambda body: missing ')'" ct-parser-error
    end
    drop

    "lambda" gensym
    "_" "" string-replace
    dup "word_ptr" swap ct-op-emit

    swap r>
    lambda-append-generated-definition
end
immediate
compile-only
