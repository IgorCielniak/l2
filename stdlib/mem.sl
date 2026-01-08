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