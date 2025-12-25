import stdlib/stdlib.sl
import stdlib/io.sl

: strconcat
    0 pick 3 pick +
    dup
    >r >r >r >r >r >r
    5 rpick
    alloc
    r> r>
    dup >r
    strcpy
    swap
    r> dup -rot +
    r> r>
    strcpy
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
;

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

: strcpy #(dst_addr src_addr len -- dst_addr len)
    dup
    >r
    swap
    dup c@
    3 pick swap
    c!
    drop
    swap
    for
        1 + dup
        c@
        swap
        -rot
        swap
        1 +
        dup
        rot
        c!
        drop
        swap
    next
    swap
    nip
    r> dup -rot - swap
;

: main
    "hello world hello world hello " "world hello world hello world"
    strconcat
    puts
;