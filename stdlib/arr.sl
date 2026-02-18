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

#arr_free [* | arr] -> [*]
word arr_free
    dup arr_cap 8 * 24 + free
end

# Helper: copy n qwords from src to dst
#arr_copy_elements [*, dst, src | n] -> [*]
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

#arr_item_ptr [*, i | arr] -> [* | ptr]
word arr_item_ptr
    swap 8 * swap 8 + +
end

#arr_get [*, i | arr] -> [* | x]
# Get element from built-in static array
word arr_get_static
    arr_item_ptr @
end

#arr_set [*, x, i | arr] -> [*]
# Set element in built-in static array
word arr_set_static
    arr_item_ptr swap !
end

#arr_sort [* | arr] -> [* | arr]
# Sort built-in static array in-place in ascending order
word arr_sort
    dup >r
    dup arr_to_dyn
    dyn_arr_sort
    dup arr_data
    r@ 8 +
    swap
    r@ @
    arr_copy_elements
    arr_free
    rdrop
end

#dyn_arr_sort [* | dyn_arr] -> [* | dyn_arr]
:asm dyn_arr_sort {
    mov rbx, [r12]          ; arr
    mov rcx, [rbx]          ; len
    cmp rcx, 1
    jle .done

    dec rcx                 ; outer = len - 1
.outer:
    xor rdx, rdx            ; j = 0

.inner:
    cmp rdx, rcx
    jge .next_outer

    mov r8, [rbx + 16]      ; data ptr
    lea r9, [r8 + rdx*8]    ; &data[j]
    mov r10, [r9]           ; a = data[j]
    mov r11, [r9 + 8]       ; b = data[j+1]
    cmp r10, r11
    jle .no_swap

    mov [r9], r11
    mov [r9 + 8], r10

.no_swap:
    inc rdx
    jmp .inner

.next_outer:
    dec rcx
    jnz .outer

.done:
    ret
}
;

#arr_clone [* | arr] -> [* | arr_copy]
# Clone built-in static array (len header + payload)
word arr_clone
    dup @ 1 +
    dup 8 * alloc
    dup >r
    rot rot
    arr_copy_elements
    r>
end

#arr_sorted [* | arr] -> [* | arr_sorted]
word arr_sorted
    arr_clone
    arr_sort
end

#dyn_arr_sorted [* | dyn_arr] -> [* | dyn_arr_sorted]
word dyn_arr_sorted
    dyn_arr_clone
    dyn_arr_sort
end
