import ../stdlib/debug.sl
import ../stdlib/hma.sl

word main
    64 alloc dup >r

    # A
    32 halloc dup r@ swap ! drop

    # B
    64 halloc dup r@ 8 + swap ! drop
    r@ 8 + @ hfree

    # D (split)
    16 halloc dup r@ 16 + swap ! drop
    r@ 16 + @ r@ 8 + @ == "split reuse" swap assert_msg

    # E (remainder)
    16 halloc dup r@ 24 + swap ! drop
    r@ 24 + @ r@ 16 + @ 32 + == "split remainder" swap assert_msg

    # free D/E
    r@ 16 + @ hfree
    r@ 24 + @ hfree

    # F (coalesce)
    64 halloc dup r@ 32 + swap ! drop
    r@ 32 + @ r@ 8 + @ == "coalesce reuse" swap assert_msg
    r@ 32 + @ hfree

    # realloc test
    32 halloc dup r@ 40 + swap ! drop
    r@ 40 + @ dup 111 ! dup 8 + 222 ! drop

    r@ 40 + @ 64 hrealloc dup r@ 40 + swap ! drop
    r@ 40 + @ @ 111 == "realloc grow v0" swap assert_msg
    r@ 40 + @ 8 + @ 222 == "realloc grow v1" swap assert_msg

    r@ 40 + @ 16 hrealloc dup r@ 40 + swap ! drop
    r@ 40 + @ @ 111 == "realloc shrink v0" swap assert_msg
    r@ 40 + @ 8 + @ 222 == "realloc shrink v1" swap assert_msg
    r@ 40 + @ hfree

    # free A
    r@ @ hfree

    r@ 64 free
    rdrop
end
