import stdlib/stdlib.sl
import stdlib/io.sl

: main
    0 argc for
        dup
        argv@ dup strlen puts
        1 +
    next
;