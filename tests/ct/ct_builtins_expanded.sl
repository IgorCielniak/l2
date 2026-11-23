import stdlib.sl

word ct_builtin_checks
    list-new
    "a" list-append
    "c" list-append
    1 "b" list-insert
    dup list-length 3 == static-assert
    dup 1 list-get "b" string-equal? static-assert
    dup 2 list-remove drop
    dup list-last "b" string-equal? static-assert
    dup "a" list-find static-assert 0 == static-assert
    dup "z" list-find 0 == static-assert drop
    dup "b" list-contains? static-assert
    dup "," string-join "a,b" string-equal? static-assert
    dup 0 2 list-slice "." string-join "a.b" string-equal? static-assert
    drop

    map-new
    "x" 1 map-set
    "y" 2 map-set
    dup map-length 2 == static-assert
    dup map-empty? 0 == static-assert
    dup map-keys list-length 2 == static-assert
    dup map-values list-length 2 == static-assert
    dup map-clone map-length 2 == static-assert

    map-new
    "z" 3 map-set
    swap map-merge

    dup "z" map-get static-assert 3 == static-assert
    dup map-clear map-empty? static-assert
    drop

    "alpha-beta-gamma" "-" string-split
    ":" string-join "alpha:beta:gamma" string-equal? static-assert
    "alpha:beta:gamma" "beta" string-contains? static-assert
    "alpha:beta:gamma" "alpha:" string-starts-with? static-assert
    "alpha:beta:gamma" ":gamma" string-ends-with? static-assert
    "  hi  " string-strip "hi" string-equal? static-assert
    "x_x_x" "_" "-" string-replace "x-x-x" string-equal? static-assert
    "MiXeD" string-upper "MIXED" string-equal? static-assert
    "MiXeD" string-lower "mixed" string-equal? static-assert

    ct-parser-current "probe" token-with-lexeme
    dup token-line 0 >= static-assert
    token-column 0 >= static-assert
end
compile-time ct_builtin_checks

word main
    1 puti cr
end