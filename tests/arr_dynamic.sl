import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/dyn_arr.sl

word main
    # arr_new / arr_len / arr_cap / arr_data
    0 arr_new

    dup arr_len puti cr
    dup arr_cap puti cr
    dup arr_data over 24 + == puti cr

    # arr_push
    dup 10 arr_push
    dup 20 arr_push
    dup 30 arr_push

    # arr_len / arr_cap after growth
    dup arr_len puti cr
    dup arr_cap puti cr

    # dyn_arr_get
    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr

    # dyn_arr_set
    dup 99 1 dyn_arr_set
    dup 1 dyn_arr_get puti cr

    # arr_reserve (with len > 0 so element copy path is exercised)
    dup 8 arr_reserve
    dup arr_cap puti cr
    dup arr_len puti cr
    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr

    # arr_pop (including empty pop)
    arr_pop puti cr
    arr_pop puti cr
    arr_pop puti cr
    arr_pop puti cr
    dup arr_len puti cr

    dyn_arr_free

    # arr_to_dyn (convert std list to dynamic array)
    [ 7 8 9 ] dup arr_to_dyn
    dup arr_len puti cr
    dup arr_cap puti cr
    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr
    dyn_arr_free

    # List literals may be static in compile-time execution; free only heap-backed ones.
    dup is_mmap_addr if
        arr_free
    else
        drop
    end

    # dyn_arr_sorted (copy) should not mutate source
    5 arr_new
    dup 3 arr_push
    dup 1 arr_push
    dup 2 arr_push

    dup dyn_arr_sorted
    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr
    dyn_arr_free

    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr

    # dyn_arr_sort (alias) sorts in place
    dyn_arr_sort
    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr
    dyn_arr_free

    # dyn_arr_sorted (alias) returns a sorted copy
    5 arr_new
    dup 4 arr_push
    dup 9 arr_push
    dup 6 arr_push

    dup dyn_arr_sorted
    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr
    dyn_arr_free

    dup 0 dyn_arr_get puti cr
    dup 1 dyn_arr_get puti cr
    dup 2 dyn_arr_get puti cr
    dyn_arr_free
end
