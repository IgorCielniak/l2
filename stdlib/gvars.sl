import stdlib.sl

word global
    next-token token-lexeme
    dup "_global" string-append
    dup ": dq 0" string-append data-append

    list-new
    ":asm" list-append
    2 pick list-append
    "{" list-append
    "lea" list-append
    "rax" list-append
    "," list-append
    "[" list-append
    "rel" list-append
    1 pick list-append
    "]" list-append
    "sub" list-append
    "r12" list-append
    "," list-append
    "8" list-append
    "mov" list-append
    "[" list-append
    "r12" list-append
    "]" list-append
    "," list-append
    "rax" list-append
    "}" list-append
    ";" list-append

    ct-current-token inject-lexemes
    drop
    drop
end immediate

word sized_global
    next-token token-lexeme
    dup "_global" string-append
    next-token token-lexeme

    1 pick
    ": times " string-append
    swap string-append
    " db 0" string-append
    data-append

    list-new
    ":asm" list-append
    2 pick list-append
    "{" list-append
    "lea" list-append
    "rax" list-append
    "," list-append
    "[" list-append
    "rel" list-append
    1 pick list-append
    "]" list-append
    "sub" list-append
    "r12" list-append
    "," list-append
    "8" list-append
    "mov" list-append
    "[" list-append
    "r12" list-append
    "]" list-append
    "," list-append
    "rax" list-append
    "}" list-append
    ";" list-append

    ct-current-token inject-lexemes
    drop
    drop
end immediate
