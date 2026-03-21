import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/arr.sl

# Get element from static array, preserving the array pointer
# [*, arr | i] -> [*, arr | value]
word aget
    over swap arr_get
end

# Set element in static array, preserving the array pointer
# [*, arr, value | i] -> [* | arr]
word aset
    rot dup >r -rot arr_set r>
end

# Swap elements at indices i and j in a static array
# [*, arr, i | j] -> [* | arr]
word arr_swap
    >r >r
    0 rpick aget
    swap
    1 rpick aget
    0 rpick aset
    swap
    1 rpick aset
    rdrop rdrop
end

# Lomuto partition (ascending, signed comparison)
# [*, arr, lo | hi] -> [*, arr | pivot_index]
word partition
    >r >r
    1 rpick aget
    >r
    1 rpick dec
    1 rpick
    while dup 2 rpick < do
        2 pick over aget nip
        0 rpick <=
        if
            swap inc swap
            2 pick 2 pick 2 pick arr_swap drop
        end
        inc
    end
    drop inc
    over over 2 rpick arr_swap drop
    rdrop rdrop rdrop
end

# Recursive quicksort
# [*, arr, lo | hi] -> [* | arr]
word qsort_rec
    over over >= if
        drop drop
    else
        >r >r
        0 rpick 1 rpick
        partition
        over 0 rpick
        2 pick dec
        qsort_rec
        drop
        over swap inc
        1 rpick
        qsort_rec
        drop
        rdrop rdrop
    end
end

# Quicksort for static arrays (in-place, ascending)
# [* | arr] -> [* | arr]
word arr_qsort
    dup @ dec
    dup 0 < if
        drop
    else
        >r dup 0 r>
        qsort_rec
    end
end

# Print all elements of a static array, one per line
word print_arr
    dup @ 0
    while 2dup > do
        2 pick over arr_get puti cr
        1 +
    end
    2drop drop
end

word main
    [ 5 3 8 1 7 2 6 4 ] arr_qsort print_arr
    [ 1 2 3 4 5 ] arr_qsort print_arr
    [ 9 7 5 3 1 ] arr_qsort print_arr
    [ 42 ] arr_qsort print_arr
    [ 3 1 4 1 5 9 2 6 5 3 ] arr_qsort print_arr
    [ 20 10 ] arr_qsort print_arr
end
