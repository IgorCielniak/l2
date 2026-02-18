import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/arr.sl

word main
    # arr_new / arr_len / arr_cap / arr_data
    0 arr_new

    dup arr_len puti cr
    dup arr_cap puti cr
    dup arr_data over 24 + == puti cr

    # arr_push
    10 swap arr_push
    20 swap arr_push
    30 swap arr_push

    # arr_len / arr_cap after growth
    dup arr_len puti cr
    dup arr_cap puti cr

    # arr_get
    dup 0 swap arr_get puti cr
    dup 1 swap arr_get puti cr
    dup 2 swap arr_get puti cr

    # arr_set
    dup 99 swap 1 swap arr_set
    dup 1 swap arr_get puti cr

    # arr_reserve (with len > 0 so element copy path is exercised)
    8 swap arr_reserve
    dup arr_cap puti cr
    dup arr_len puti cr
    dup 0 swap arr_get puti cr
    dup 1 swap arr_get puti cr
    dup 2 swap arr_get puti cr

    # arr_pop (including empty pop)
    arr_pop puti cr
    arr_pop puti cr
    arr_pop puti cr
    arr_pop puti cr
    dup arr_len puti cr

    arr_free

    # arr_to_dyn (convert std list to dynamic array)
    [ 7 8 9 ] dup arr_to_dyn
    dup arr_len puti cr
    dup arr_cap puti cr
    dup 0 swap arr_get puti cr
    dup 1 swap arr_get puti cr
    dup 2 swap arr_get puti cr
    arr_free

    # free list allocation: bytes = (len + 1) * 8
    dup @ 1 + 8 * free
end
