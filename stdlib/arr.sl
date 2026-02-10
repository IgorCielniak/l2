# Dynamic arrays (qword elements)
#
# Layout at address `arr`:
#   [arr + 0]  len   (qword)
#   [arr + 8]  cap   (qword)
#   [arr + 16] data  (qword)  = arr + 24
#   [arr + 24] elements (cap * 8 bytes)
#
# Allocation: mmap; free: munmap.
# Growth: allocate new block, copy elements, munmap old block.

import mem.sl

#arr_to_dyn [* | std_arr] -> [* | dyn_arr]
word arr_to_dyn
    dup @ dup dup 3 + alloc dup rot !
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

#arr_free [* | arr] -> [*]
word arr_free
    dup arr_cap 8 * 24 + free
end

# Helper: copy n qwords from src to dst [*, dst, src | n] -> [*]
word arr_copy_elements
    while dup 0 > do
        over @ 3 pick swap !   # dst = *src
        swap 8 + swap          # src += 8
        rot 8 + -rot           # dst += 8
        1 -
    end
    drop 2drop
end

#arr_reserve [*, cap | arr] -> [* | arr]
# Ensures capacity >= cap; returns (possibly moved) arr pointer.
word arr_reserve
    swap dup 1 < if drop 1 end swap   # reqcap arr

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
        swap arr_free
        nip
    end
end

#arr_push [*, x | arr] -> [* | arr]
# Push element onto array, growing if needed
word arr_push
    dup arr_len over arr_cap >= if
        dup arr_cap dup 1 < if drop 1 end 2 *
        over arr_reserve
        nip
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

#arr_get [*, i | arr] -> [* | x]
# Get element at index i
word arr_get
    arr_data swap 8 * + @
end

#arr_set [*, x, i | arr] -> [*]
# Set element at index i to x
word arr_set
    arr_data swap 8 * + swap !
end