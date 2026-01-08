import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word dup
    6
end
compile-only

word emit-overridden
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
end
immediate
compile-only

word main
    emit-overridden
    puti cr
    0
end
