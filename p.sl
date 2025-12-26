import stdlib/stdlib.sl
import stdlib/io.sl

: main
    12345 0 p!    # store 12345 at offset 0
    0 p@ puti cr  # read and print value at offset 0
    67890 8 p!    # store 67890 at offset 8
    8 p@ puti cr  # read and print value at offset 8
;
