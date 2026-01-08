import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    [ 1 2 3 4 ]
    # element i is at: list_ptr + 8 + i*8
    dup 8 2 * 8 + + @ puti cr   # index 2 = 3
    dup 8 1 * 8 + + @ puti cr   # index 1 = 2
end