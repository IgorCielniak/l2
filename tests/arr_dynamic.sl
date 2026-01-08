import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/arr.sl

word main
    0 arr_new

    dup arr_cap puti cr

    10 swap arr_push
    20 swap arr_push
    30 swap arr_push

    dup arr_len puti cr
    dup arr_cap puti cr

    # print elements via explicit offsets: data[i] = @ (arr_data + i*8)
    dup arr_data 0 8 * + @ puti cr
    dup arr_data 1 8 * + @ puti cr
    dup arr_data 2 8 * + @ puti cr

    arr_free
end
