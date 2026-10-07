import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    "/tmp/l2_write_file_test.txt"  # path
    "hello from write_file test\n" # buffer
    write_file
    dup 0 > if
        "write_file bytes:" puts
        puti cr
        "/tmp/l2_write_file2_test.txt"
        "hello from write_file test\n"
        write_file2
        dup 0 > if
            "write_file2 bytes:" puts
            puti cr
            0 exit
        end
        "write_file2 failed errno=" puts
        puti cr
        1 exit
    end
    "write_file failed errno=" puts
    puti cr
    1
end
