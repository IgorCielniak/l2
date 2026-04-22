import stdlib.sl
import gvars.sl

sized_global defer_buf 16

word defer
    defer_buf @ 0 == if
        defer_buf 1 !
        8 alloc dup defer_buf 8 + swap !
        swap !
    else
        defer_buf dup @ 1 + !
        defer_buf 8 + @
        defer_buf @ 1 - 8 *
        dup 8 +
        realloc dup
        2 pick swap defer_buf @ 1 - 8 * +
        swap ! nip
        defer_buf 8 + swap !
    end
end
        
word run_defers
    0
    while dup defer_buf @ swap > do
        defer_buf 8 + @
        over 8 * +
        @ call
        1 +
    end
    defer_buf 8 + @
    swap 8 * free
end

