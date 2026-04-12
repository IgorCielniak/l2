import stdlib.sl

word ct_builtin_checks
    list-new
    "a" list-append
    "c" list-append
    1 "b" list-insert
    dup list-length 3 == static_assert
    dup 1 list-get "b" string= static_assert
    dup 2 list-remove drop
    dup list-last "b" string= static_assert
    dup "a" list-find static_assert 0 == static_assert
    dup "z" list-find 0 == static_assert drop
    dup "b" list-contains? static_assert
    dup "," list-join "a,b" string= static_assert
    dup 0 2 list-slice "." list-join "a.b" string= static_assert
    drop

    map-new
    "x" 1 map-set
    "y" 2 map-set
    dup map-length 2 == static_assert
    dup map-empty? 0 == static_assert
    dup map-keys list-length 2 == static_assert
    dup map-values list-length 2 == static_assert
    dup map-clone map-length 2 == static_assert

    map-new
    "z" 3 map-set
    swap map-update

    dup "z" map-get static_assert 3 == static_assert drop
    dup map-clear map-empty? static_assert
    drop

    "alpha-beta-gamma" "-" string-split
    ":" string-join "alpha:beta:gamma" string= static_assert
    "alpha:beta:gamma" "beta" string-contains? static_assert
    "alpha:beta:gamma" "alpha:" string-starts-with? static_assert
    "alpha:beta:gamma" ":gamma" string-ends-with? static_assert
    "  hi  " string-strip "hi" string= static_assert
    "x_x_x" "_" "-" string-replace "x-x-x" string= static_assert
    "MiXeD" string-upper "MIXED" string= static_assert
    "MiXeD" string-lower "mixed" string= static_assert

    "probe" ct-current-token token-from-lexeme
    dup token-line 0 >= static_assert
    token-column 0 >= static_assert
end
compile-time ct_builtin_checks

word main
    1 puti cr
end