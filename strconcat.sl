import stdlib/stdlib.sl
import stdlib/mem.sl
import stdlib/io.sl

word strconcat
    0 pick 3 pick +
    dup
    >r >r >r >r >r >r
    5 rpick
    alloc
    r> r>
    dup >r
    memcpy
    swap
    r> dup -rot +
    r> r>
    memcpy
    swap
    3 pick
    -
    swap
    drop
    swap
    0 rpick
    nip
    rot
    drop
    rdrop rdrop rdrop
end

word main
    "hello world hello world hello " "world hello world hello world"
    strconcat
    puts
end
