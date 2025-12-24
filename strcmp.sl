import stdlib/stdlib.sl
import stdlib/io.sl

: strcmp
    3 pick 2 pick @ swap @ ==
;

: main
    "g" "g"
    strcmp
    puti cr
    puts
    puts
;