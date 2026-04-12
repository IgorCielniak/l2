import stdlib/stdlib.sl

word main
    { 1 2 3 } dup @ puti cr
    dup 8 + @ puti cr
    dup 16 + @ puti cr
    dup 24 + @ puti cr
    drop

    { 7 8 }:5 dup @ puti cr
    dup 8 + @ puti cr
    dup 16 + @ puti cr
    dup 24 + @ puti cr
    dup 32 + @ puti cr
    dup 40 + @ puti cr
    drop

    {}:4 dup @ puti cr
    drop
end
