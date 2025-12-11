import stdlib.sl
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

: test-macro
    9 square puts
    MAGIC puts
    add13 puts
;

: test-if
    5 5 == if
        111 puts
    else
        222 puts
    then
;

: test-else-if
    2
    dup 1 == if
        50 puts
    else
        dup 2 == if
            60 puts
        else
            70 puts
        then
    then
    drop
;

: test-for
    0
    5 for
        1 +
    next
    puts
;

: test-for-zero
    123
    0 for
        drop
    next
    puts
;

: test-struct
    mem-slot
    dup 111 swap Point.x!
    dup 222 swap Point.y!
    dup Point.x@ puts
    Point.y@ puts
    Point.size puts
;

: test-cmp
    5 5 == puts
    5 4 == puts
    5 4 != puts
    4 4 != puts
    3 5 < puts
    5 3 < puts
    5 3 > puts
    3 5 > puts
    5 5 <= puts
    6 5 <= puts
    5 5 >= puts
    4 5 >= puts
;

: test-c-fn
    3
    7
    fancy_add()
    puts
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
