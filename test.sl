import stdlib/stdlib.sl
import stdlib/io.sl
import fn.sl

:asm mem-slot {
    lea rax, [rel print_buf]
    sub r12, 8
    mov [r12], rax
}
;

macro: square
    dup *
;macro

macro: defconst 2
    : $1
        $2
    ;
;macro

macro: defadder 3
    : $1
        $2 $3 +
    ;
;macro

defconst MAGIC 99
defadder add13 5 8

struct: Point
    field x 8
    field y 8
;struct

extend-syntax

fn fancy_add(int a, int b){
    return (a + b) * b;
}

: test-add
    5 7 + puti cr
;

: test-sub
    10 3 - puti cr
;

: test-mul
    6 7 * puti cr
;

: test-div
    84 7 / puti cr
;

: test-mod
    85 7 % puti cr
;

: test-drop
    10 20 drop puti cr
;

: test-dup
    11 dup + puti cr
;

: test-swap
    2 5 swap - puti cr
;

: test-store
    mem-slot dup
    123 swap !
    @ puti cr
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
    @ puti cr
    4096 munmap drop
;

: test-macro
    9 square puti cr
    MAGIC puti cr
    add13 puti cr
;

: test-if
    5 5 == if
        111 puti cr
    else
        222 puti cr
    then
;

: test-else-if
    2
    dup 1 == if
        50 puti cr
    else
        dup 2 == if
            60 puti cr
        else
            70 puti cr
        then
    then
    drop
;

: test-for
    0
    5 for
        1 +
    next
    puti cr
;

: test-for-zero
    123
    0 for
        drop
    next
    puti cr
;

: test-struct
    mem-slot
    dup 111 swap Point.x!
    dup 222 swap Point.y!
    dup Point.x@ puti cr
    Point.y@ puti cr
    Point.size puti cr
;

: test-cmp
    5 5 == puti cr
    5 4 == puti cr
    5 4 != puti cr
    4 4 != puti cr
    3 5 < puti cr
    5 3 < puti cr
    5 3 > puti cr
    3 5 > puti cr
    5 5 <= puti cr
    6 5 <= puti cr
    5 5 >= puti cr
    4 5 >= puti cr
;

: test-c-fn
    3
    7
    fancy_add()
    puti cr
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
    test-macro
    test-if
    test-else-if
    test-for
    test-for-zero
    test-cmp
    test-struct
    test-c-fn
    0
;
