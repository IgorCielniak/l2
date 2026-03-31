# Dynamic arrays (qword elements)
#
# Layout at address `arr`:
#   [arr + 0]  len   (qword)
#   [arr + 8]  cap   (qword)
#   [arr + 16] data  (qword)  = arr + 24
#   [arr + 24] elements (cap * 8 bytes)
#
# Allocation: heap alloc/free.
# Growth: allocate new block, copy elements, free old block.

import arr.sl

#arr_to_dyn [* | std_arr] -> [* | dyn_arr]
word arr_to_dyn
    dup @ dup dup 2 * 3 + alloc dup rot !
    dup rot 2 * swap 8 + swap ! dup 16 +
    dup 8 + dup -rot ! 0 swap 3 pick dup @
    8 * swap 8 + swap memcpy 2drop drop nip
end

#arr_new [* | cap] -> [* | arr]
# Create a new array with given initial capacity (minimum 1)
word arr_new
    dup 1 < if drop 1 end
    dup 8 * 24 + alloc
    dup 0 !                        # len = 0
    over over 8 + swap !           # cap = requested cap
    dup 24 + over 16 + swap !      # data = arr + 24
    nip
end

#arr_len [* | arr] -> [* | len]
word arr_len @ end

#arr_cap [* | arr] -> [* | cap]
word arr_cap 8 + @ end

#arr_data [* | arr] -> [* | ptr]
word arr_data 16 + @ end

#dyn_arr_free [* | arr] -> [*]
word dyn_arr_free
    dup arr_cap 8 * 24 + free
end

#arr_reserve [*, arr | cap] -> [* | arr]
# Ensures capacity >= cap; returns (possibly moved) arr pointer.
word arr_reserve
    dup 1 < if drop 1 end swap   # stack: [*, reqcap | arr]

    # Check: if arr_cap >= reqcap, do nothing
    over over arr_cap swap
    >= if
        nip
    else
        # Allocate new block
        over 8 * 24 + alloc

        # Copy header
        over arr_len over swap !       # len
        2 pick over 8 + swap !         # cap = reqcap
        dup 24 + over 16 + swap !      # data = newarr + 24

        # Copy elements
        dup arr_data
        2 pick arr_data
        3 pick arr_len
        arr_copy_elements

        # Free old and return new
        swap dyn_arr_free
        nip
    end
end

#arr_push [*, arr | x] -> [* | arr]
# Push element onto array, growing if needed
word arr_push
    swap
    dup arr_len over arr_cap >= if
        dup arr_cap dup 1 < if drop 1 end 2 *
        arr_reserve
    end

    # Store x at data[len]
    dup arr_data over arr_len 8 * +
    rot over swap ! drop

    # Increment len
    dup @ 1 + over swap !
end

#arr_pop [* | arr] -> [*, arr | x]
# Pop element from array (returns 0 if empty)
word arr_pop
    dup arr_len 0 == if
        0
    else
        # Decrement len
        dup @ 1 - over swap !
        # Get element at new len position
        dup arr_data over arr_len 8 * + @
    end
end

#dyn_arr_get [*, arr | i] -> [* | x]
# Get element at index i
word dyn_arr_get
    swap arr_data swap 8 * + @
end

#dyn_arr_set [*, arr, x | i] -> [*]
# Set element at index i to x
word dyn_arr_set
    rot arr_data swap 8 * + swap !
end

#dyn_arr_clone [* | dyn_arr] -> [* | dyn_arr_copy]
word dyn_arr_clone
    dup arr_len
    dup arr_new

    dup arr_data
    3 pick arr_data
    3 pick
    arr_copy_elements

    dup >r
    swap !
    drop
    r>
end

#dyn_arr_sort [* | dyn_arr] -> [* | dyn_arr]
word dyn_arr_sort
    dup >r
    dup arr_data
    swap arr_len
    sort
    r>
end

#dyn_arr_sorted [* | dyn_arr] -> [* | dyn_arr_sorted]
word dyn_arr_sorted
    dyn_arr_clone
    dyn_arr_sort
end
