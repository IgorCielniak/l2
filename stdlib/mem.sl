import stdlib.sl

word alloc
    0      # addr hint (NULL)
    swap   # size
    3      # prot (PROT_READ | PROT_WRITE)
    34     # flags (MAP_PRIVATE | MAP_ANON)
    -1     # fd
    0      # offset
    mmap
end

word free
    munmap drop
end

word memcpy #(dst_addr src_addr len -- dst_addr len)
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
    end
    swap
    nip
    r> dup -rot - swap
end