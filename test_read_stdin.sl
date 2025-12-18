import stdlib/stdlib.sl
import stdlib/io.sl

: main
    1024
    read_stdin   # returns (len addr)
    dup 0 > if
        write_buf
        0 exit
    then
    "read_stdin failed" puts
    exit
;
