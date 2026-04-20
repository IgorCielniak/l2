import ../stdlib/hmm.sl

word main
    16 halloc
    dup 111 !
    dup 8 + 222 !

    32 hrealloc

    dup @ puti cr
    dup 8 + @ puti cr

    dup 16 + 333 !
    dup 24 + 444 !

    dup 16 + @ puti cr
    dup 24 + @ puti cr

    hfree
end
