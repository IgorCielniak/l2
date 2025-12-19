import stdlib/stdlib.sl
import stdlib/io.sl

: main
    "/etc/hostname" # (addr len)
    read_file                 # (file_addr file_len)
    dup 0 > if                # if file_len > 0, success
        write_buf             # print file contents (file_len file_addr)
        0
        exit
    then
    dup -2 == if              # open() failed
        drop
        "open() failed: errno=" puts
        swap puti cr
        exit
    then
    dup -1 == if              # fstat() failed
        drop
        "fstat() failed: errno=" puts
        swap puti cr
        exit
    then
    dup -3 == if              # mmap() failed
        drop
        "mmap() failed" puts
        exit
    then
    "unknown read_file failure" puts
    dup                       # file_len file_len file_addr
    exit                       # Exit with returned file_len as the program exit code (debug)
;
