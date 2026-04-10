# Native list helpers for L2 list literals.
#
# Layout at address `arr`:
#   [arr + 0]  len   (qword)
#   [arr + 8]  elements start (qword-aligned)
#
# This module targets fixed-layout lists (heap literals and BSS literals).
# Growable dynamic arrays live in stdlib/dyn_arr.sl.

import mem.sl

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

#arr_item_ptr [*, i | arr] -> [* | ptr]
word arr_item_ptr
    swap 8 * swap 8 + +
end

#list_len [* | arr] -> [* | len]
word list_len @ end

#list_data [* | arr] -> [* | ptr]
word list_data 8 + end

#list_empty [* | arr] -> [* | bool]
word list_empty @ 0 == end

#list_head [* | arr] -> [* | x]
word list_head 0 arr_get end

#list_last [* | arr] -> [* | x]
word list_last dup @ 1 - arr_get end

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

word fw-opener-lex?
    dup "if" string= if drop 1 exit end
    dup "for" string= if drop 1 exit end
    dup "while" string= if drop 1 exit end
    dup "begin" string= if drop 1 exit end
    dup "with" string= if drop 1 exit end
    dup "foreach" string= if drop 1 exit end
    dup "foreachwith" string= if drop 1 exit end
    drop 0
end
compile-only

word fw-collect-body
    list-new 0
    with body depth in
    begin
        next-token dup nil? if
            drop
            "unterminated 'foreachwith' block (missing 'end')" parse-error
        end
        dup token-lexeme "end" string= if
            depth 0 == if
                drop
                body exit
            end
            depth 1 - depth !
            body swap list-append body !
            continue
        end
        dup token-lexeme fw-opener-lex? if
            depth 1 + depth !
        end
        body swap list-append body !
    again
    end
end
compile-only

word fw-append-lex
    swap token-from-lexeme
    list-append
end
compile-only

word foreach
    fw-collect-body
    with body in
        list-new with out in
            out nil "dup" fw-append-lex out !
            out nil "@" fw-append-lex out !
            out nil "swap" fw-append-lex out !
            out nil "8" fw-append-lex out !
            out nil "+" fw-append-lex out !
            out nil "swap" fw-append-lex out !
            out nil "0" fw-append-lex out !
            out nil "swap" fw-append-lex out !
            out nil "for" fw-append-lex out !
            out nil "2dup" fw-append-lex out !
            out nil "8" fw-append-lex out !
            out nil "*" fw-append-lex out !
            out nil "+" fw-append-lex out !
            out nil "@" fw-append-lex out !
            out body list-extend out !
            out nil "1" fw-append-lex out !
            out nil "+" fw-append-lex out !
            out nil "end" fw-append-lex out !
            out nil "2drop" fw-append-lex out !
            out inject-tokens
        end
    end
end
immediate
compile-only

word foreachwith
    next-token dup nil? if
        drop
        "missing variable name after 'foreachwith'" parse-error
    end
    dup token-lexeme dup identifier? 0 == if
        drop drop
        "invalid variable name in 'foreachwith'" parse-error
    end
    drop
    >r

    fw-collect-body
    r>
    with body name_tok in
        list-new with out in
            out name_tok "foreach" fw-append-lex out !
            out name_tok "with" fw-append-lex out !
            out name_tok list-append out !
            out name_tok "in" fw-append-lex out !
            out body list-extend out !
            out name_tok "end" fw-append-lex out !
            out name_tok "end" fw-append-lex out !
            out name_tok "drop" fw-append-lex out !
            out inject-tokens
        end
    end
end
immediate
compile-only