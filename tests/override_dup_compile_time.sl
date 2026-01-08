import ../stdlib/stdlib.sl
import ../stdlib/io.sl

: dup
    6
;
compile-only

: emit-overridden
    "dup" use-l2-ct
    42
    dup
    int>string
    nil
    token-from-lexeme
    list-new
    swap
    list-append
    inject-tokens
;
immediate
compile-only

: main
    emit-overridden
    puti cr
    0
;
