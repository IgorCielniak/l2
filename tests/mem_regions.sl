import ../stdlib/stdlib.sl

word main
    stack_pointer is_stack_addr puti cr
    stack_pointer is_brk_heap_addr puti cr

    4096 alloc
    dup is_mmap_addr puti cr
    dup is_stack_addr puti cr
    4096 free

    brk_current 1 - is_brk_heap_addr puti cr
end
