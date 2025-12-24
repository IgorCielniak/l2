import stdlib/stdlib.sl
import stdlib/io.sl

: alloc
    0      # addr hint (NULL)
    swap   # size
    3      # prot (PROT_READ | PROT_WRITE)
    34     # flags (MAP_PRIVATE | MAP_ANON)
    -1     # fd
    0      # offset
    mmap
;

: free
    munmap drop
;

: test-mem-alloc
    4096 alloc dup 1337 swap !   # allocate 4096 bytes, store 1337 at start
    dup @ puti cr                # print value at start
    4096 free                    # free the memory
;

struct: Point
    field x 8
    field y 8
;struct

: main
    32 alloc           # allocate 32 bytes (enough for a Point struct)
    dup 111 swap Point.x!
    dup 222 swap Point.y!
    dup Point.x@ puti cr
    Point.y@ puti cr
    32 free            # free the memory
;