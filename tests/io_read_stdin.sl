import ../stdlib/stdlib.sl
import ../stdlib/io.sl

: main
    1024
    read_stdin   # returns (addr len)
    dup 0 > if
        write_buf
        0 exit
    end
    "read_stdin failed" puts
    exit
;
