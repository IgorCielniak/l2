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

#dyn_arr_free [* | arr] -> [*]
word dyn_arr_free
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

#arr_item_ptr [*, i | arr] -> [* | ptr]
word arr_item_ptr
    swap 8 * swap 8 + +
end

#arr_get [*, arr | i] -> [* | x]
# Get element from built-in static array
word arr_get
    swap arr_item_ptr @
end

#arr_set [*, arr, x | i] -> [*]
# Set element in built-in static array
word arr_set
    rot arr_item_ptr swap !
end

#arr_free [* | arr] -> [*]
# Free built-in static array allocation produced by list literals.
word arr_free
    dup @ 1 + 8 * free
end

#sort [*, addr | len] -> [*]
# In-place ascending sort of qword elements at `addr`.
:asm sort {
    mov rcx, [r12]          ; len
    mov rbx, [r12 + 8]      ; addr
    add r12, 16

    cmp rcx, 1
    jle .done

    dec rcx                 ; outer = len - 1
.outer:
    xor rdx, rdx            ; j = 0

.inner:
    cmp rdx, rcx
    jge .next_outer

    lea r8, [rbx + rdx*8]   ; &data[j]
    mov r9, [r8]            ; a = data[j]
    mov r10, [r8 + 8]       ; b = data[j+1]
    cmp r9, r10
    jle .no_swap

    mov [r8], r10
    mov [r8 + 8], r9

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

#sort8 [*, addr | len] -> [*]
# In-place ascending sort of byte elements at `addr`.
:asm sort8 {
    mov rcx, [r12]          ; len
    mov rbx, [r12 + 8]      ; addr
    add r12, 16

    cmp rcx, 1
    jle .done

    dec rcx                 ; outer = len - 1
.outer:
    xor rdx, rdx            ; j = 0

.inner:
    cmp rdx, rcx
    jge .next_outer

    lea r8, [rbx + rdx]     ; &data[j]
    movzx r9, byte [r8]     ; a = data[j]
    movzx r10, byte [r8 + 1] ; b = data[j+1]
    cmp r9, r10
    jle .no_swap

    mov byte [r8], r10b
    mov byte [r8 + 1], r9b

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

#sorted [*, addr | len] -> [* | sorted_addr]
# Clone qword elements and return sorted copy.
word sorted
    dup >r
    8 * alloc
    dup >r
    swap
    r@
    arr_copy_elements
    r>
    r>
    over >r
    sort
    r>
end

#sorted8 [*, addr | len] -> [* | sorted_addr]
# Clone byte elements and return sorted copy.
word sorted8
    dup >r
    alloc
    dup >r
    swap
    r@
    while dup 0 > do
        over c@ 3 pick swap c!
        swap 1 + swap
        rot 1 + -rot
        1 -
    end
    drop 2drop
    r>
    r>
    over >r
    sort8
    r>
end

#arr_sort [* | arr] -> [* | arr]
# Sort built-in static array in-place in ascending order.
word arr_sort
    dup >r
    dup 8 +
    swap @
    sort
    r>
end

#arr_sorted [* | arr] -> [* | arr_sorted]
word arr_sorted
    arr_clone
    arr_sort
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

# arr_contains [*, addr | x] -> [* | bool]
word arr_contains
    over @ >r >r 8 + r> r>
    for
        2dup swap @ == if 1 nip nip rdrop ret end
        swap 8 + swap
    end 0 nip nip
end

# arr_find [*, addr | x] -> [* | bool]
word arr_find
    over @ >r >r 8 + r> r>
    0 >r
    for
        2dup swap @ == if rswap r> nip nip rdrop ret end
        swap 8 + swap rswap r> 1 + >r rswap
    end rdrop -1 nip nip
end
