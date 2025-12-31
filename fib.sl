import stdlib/stdlib.sl
import stdlib/io.sl

: main
    1 1 2dup 2dup puti cr puti cr
    +
    dup puti cr
    rot
    22 dup >r for
        2dup + dup puti cr
        rot
    next
    "-------" puts
    r> 3 + puti
    " numbers printed from the fibonaci sequence" puts
;

: main2
    1 2 while over 100 < do
        over puti cr
        swap over +
    repeat
;

