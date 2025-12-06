import stdlib.sl

:asm mem-slot {
    lea rax, [rel print_buf]
    sub r12, 8
    mov [r12], rax
}
;

: test-add
    5 7 + puts
;

: test-sub
    10 3 - puts
;

: test-mul
    6 7 * puts
;

: test-div
    84 7 / puts
;

: test-mod
    85 7 % puts
;

: test-drop
    10 20 drop puts
;

: test-dup
    11 dup + puts
;

: test-swap
    2 5 swap - puts
;

: test-store
    mem-slot dup
    123 swap !
    @ puts
;


: test-mmap
    0      # addr hint (NULL)
    4096   # length (page)
    3      # prot (PROT_READ | PROT_WRITE)
    34     # flags (MAP_PRIVATE | MAP_ANON)
    -1     # fd (ignored for MAP_ANON)
    0      # offset
    mmap
    dup
    1337 swap !
    dup
    @ puts
    4096 munmap drop
;

: main
    test-add
    test-sub
    test-mul
    test-div
    test-mod
    test-drop
    test-dup
    test-swap
    test-store
    test-mmap
    0
;
