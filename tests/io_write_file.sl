import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    "/tmp/l2_write_file_test.txt"  # path
    "hello from write_file test\n" # buffer
    write_file
    dup 0 > if
        "wrote bytes: " puts
        puti cr
        0
        exit
    end
    "write failed errno=" puts
    puti cr
    exit
end
