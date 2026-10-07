import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    1024 alloc with buffer in
        buffer 1024 input
        dup 0 == if
            drop
            "input eof" puts
            0
        else
            dup 0 > if
                buffer swap write_buf
                0
            else
                "input failed errno=" puts
                puti
                1
            end
        end
        buffer 1024 free
        exit
    end
end
