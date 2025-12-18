import stdlib/stdlib.sl
import stdlib/io.sl

: main
    "/tmp/l2_test_write.txt" # push path (len addr)
    swap                      # -> (addr len) = path_ptr path_len
    "hello from write_file test\n" # push buf (len addr)
    swap                      # -> (addr len) = buf_ptr buf_len
    2swap                     # reorder pairs -> path_ptr path_len buf_ptr buf_len
    write_file
    dup 0 > if
        "wrote bytes: " puts
        puts
        0
        exit
    then
    "write failed errno=" puts
    puts
    exit
;
