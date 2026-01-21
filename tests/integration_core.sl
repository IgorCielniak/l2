import ../stdlib/stdlib.sl
import ../stdlib/io.sl

:asm mem-slot {
    lea rax, [rel print_buf]
    sub r12, 8
    mov [r12], rax
}
;

macro square
    dup *
;

macro defconst 2
    word $1
        $2
    end
;

macro defadder 3
    word $1
        $2 $3 +
    end
;

defconst MAGIC 99
defadder add13 5 8

struct Point
    field x 8
    field y 8
end

word test-add
    5 7 + puti cr
end

word test-sub
    10 3 - puti cr
end

word test-mul
    6 7 * puti cr
end

word test-div
    84 7 / puti cr
end

word test-mod
    85 7 % puti cr
end

word test-drop
    10 20 drop puti cr
end

word test-dup
    11 dup + puti cr
end

word test-swap
    2 5 swap - puti cr
end

word test-store
    mem-slot dup
    123 swap !
    @ puti cr
end


word test-mmap
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
end

word test-macro
    9 square puti cr
    MAGIC puti cr
    add13 puti cr
end

word test-if
    5 5 == if
        111 puti cr
    else
        222 puti cr
    end
end

word test-else-if
    2
    dup 1 == if
        50 puti cr
    else
        dup 2 == if
            60 puti cr
        else
            70 puti cr
        end
    end
    drop
end

word test-for
    0
    5 for
        1 +
    end
    puti cr
end

word test-for-zero
    123
    0 for
        drop
    end
    puti cr
end

word test-struct
    mem-slot
    dup 111 swap Point.x!
    dup 222 swap Point.y!
    dup Point.x@ puti cr
    Point.y@ puti cr
    Point.size puti cr
end

word test-cmp
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
end

word main
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
    0
end
