import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word dup
    6
end
compile-only

word emit-overridden
    "dup" ct-definition-prefer-l2-by-name drop
    42
    dup
    int>string
    ct-parser-current swap
    token-with-lexeme
    list-new
    swap
    list-append
    ct-parser-inject
end
immediate
compile-only

word main
    emit-overridden
    puti cr
    0
end
