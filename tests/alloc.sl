import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/mem.sl

word test-mem-alloc
    4096 alloc dup 1337 !   # allocate 4096 bytes, store 1337 at start
    dup @ puti cr                # print value at start
    4096 free                    # free the memory
end

struct Point
    field x 8
    field y 8
end

word main
    32 alloc           # allocate 32 bytes (enough for a Point struct)
    dup
    dup 111 Point.x!     # store 111 at offset 0 (Point.x)
    dup 222 Point.y! # store 222 at offset 8 (Point.y)
    dup Point.x@ puti cr      # print x
    dup Point.y@ puti cr  # print y
    32 free            # free the memory
end
