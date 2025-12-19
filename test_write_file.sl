import stdlib/stdlib.sl
import stdlib/io.sl

: main
    "/tmp/l2_test_write.txt" # push path (addr len)
    "hello from write_file test\n" # push buf (addr len)
    write_file
    dup 0 > if
        "wrote bytes: " puts
        puti cr
        0
        exit
    then
    "write failed errno=" puts
    puti cr
    exit
;