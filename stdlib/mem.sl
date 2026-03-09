import stdlib.sl

#alloc [* | size] -> [* | addr]
word alloc
    0      # addr hint (NULL)
    swap   # size
    3      # prot (PROT_READ | PROT_WRITE)
    34     # flags (MAP_PRIVATE | MAP_ANON)
    -1     # fd
    0      # offset
    mmap
    nip
end

#free [*, addr | size] -> [*]
word free
    munmap drop
end

#memcpy [*, dst_addr, src_addr | len] -> [*, dst_addr | len]
word memcpy
    dup
    >r
    swap
    dup c@
    3 pick swap
    c!
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
        swap
    end
    drop
    r> dup -rot - swap
end

#memset [*, value, len | addr] -> [*]
word memset
    swap
    0 swap for
        -rot swap 2 pick + 2dup swap ! 1 + -rot swap
    end
    2drop drop
end

#memdump [*, len | addr] -> [* | addr]
word memdump
    for
        dup @ puti cr 8 +
    end
end

#realloc [*, addr, old_len | new_len] -> [* | new_addr]
word realloc
    2 pick swap alloc
    rot rot swap
    memcpy
    swap -rot free
end
