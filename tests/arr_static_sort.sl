import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/arr.sl

word free_static
    dup @ 1 + 8 * free
end

word main
    [ 4 1 3 2 ] dup arr_sort
    dup 0 swap arr_get_static puti cr
    dup 1 swap arr_get_static puti cr
    dup 2 swap arr_get_static puti cr
    dup 3 swap arr_get_static puti cr
    free_static

    [ 9 5 7 ] dup arr_sorted
    dup 0 swap arr_get_static puti cr
    dup 1 swap arr_get_static puti cr
    dup 2 swap arr_get_static puti cr

    swap
    dup 0 swap arr_get_static puti cr
    dup 1 swap arr_get_static puti cr
    dup 2 swap arr_get_static puti cr

    free_static
    free_static
end
