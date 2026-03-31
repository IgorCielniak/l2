import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/arr.sl

word main
    [ 4 1 3 2 ] dup arr_sort
    dup 0 arr_get puti cr
    dup 1 arr_get puti cr
    dup 2 arr_get puti cr
    dup 3 arr_get puti cr
    dup list_len puti cr
    dup list_head puti cr
    dup list_last puti cr
    dup list_empty puti cr
    arr_free

    [ 9 5 7 ] dup arr_sorted
    dup 0 arr_get puti cr
    dup 1 arr_get puti cr
    dup 2 arr_get puti cr

    swap
    dup 0 arr_get puti cr
    dup 1 arr_get puti cr
    dup 2 arr_get puti cr

    arr_free
    arr_free
end
