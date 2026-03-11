import stdlib.sl

# Test cimport: extract struct definitions and extern functions from a C header.
cimport "cimport_structs.h"

word main
    # Verify that cstruct Point was generated with correct layout
    Point.size puti cr       # 16 bytes (two i64 = 8+8)

    # Allocate a Point, set fields, read them back
    Point.size alloc dup >r
    r@ 10 Point.x!
    r@ 20 Point.y!
    r@ Point.x@ puti cr      # 10
    r@ Point.y@ puti cr      # 20

    # Call C helper that takes a pointer (simple scalar ABI)
    r@ point_sum_ptr puti cr  # 30
    r> Point.size free

    # Verify Pair struct layout
    Pair.size puti cr         # 16

    Pair.size alloc dup >r
    r@ 100 Pair.a!
    r@ 200 Pair.b!
    r@ pair_sum_ptr puti cr   # 300
    r> Pair.size free

    0
end
