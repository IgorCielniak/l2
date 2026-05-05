import ../stdlib/hma.sl
import ../stdlib/debug.sl

word hp_puts
    swap write_buf 10 putc
end

word t_mini_hinit
    hinit
    "hinit done\n" write_buf
    hshutdown
end

word t_mini_halloc_simple
    10 halloc
    drop
    hshutdown
end

word t_mini_halloc
    1048576 halloc hfree
    hshutdown
end

word t_mini_halloc_io
    "hello heap" dup halloc -rot memcpy
    2dup hp_puts
    drop hfree
end

word t_mini_halloc_verify
    "hello heap" dup halloc -rot memcpy
    2dup hp_puts
    drop hfree

    10 halloc
    20 halloc
    30 halloc

    hfree
    hfree
    hfree

    hshutdown
end

word t_hma_realloc
    16 halloc
    dup 111 !
    dup 8 + 222 !

    32 hrealloc

    dup @ puti cr
    dup 8 + @ puti cr

    dup 16 + 333 !
    dup 24 + 444 !

    dup 16 + @ puti cr
    dup 24 + @ puti cr

    hfree
end

word t_halloc_io
    "hello heap" dup halloc -rot memcpy
    2dup hp_puts

    over 6 + 72 c!
    over 7 + 69 c!
    over 8 + 65 c!
    over 9 + 80 c!
    2dup hp_puts

    drop hfree
end

word t_halloc_simple_unmap
    32 halloc hfree
    "test complete" puts
end

word t_halloc_with_shutdown
    32 halloc hfree
    hshutdown
    "test complete" puts
end

word main
    t_mini_hinit
    t_mini_halloc_simple
    t_mini_halloc
    t_mini_halloc_io
    t_mini_halloc_verify
    t_hma_realloc
    t_halloc_io
    t_halloc_simple_unmap
    t_halloc_with_shutdown
end
