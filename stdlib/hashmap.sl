# Hash Map (open-addressing, linear probing)
#
# Layout at address `hm`:
#   [hm +  0]  count     (qword)  — number of live entries
#   [hm +  8]  capacity  (qword)  — number of slots (always power of 2)
#   [hm + 16]  keys_ptr  (qword)  — pointer to keys array  (cap * 8 bytes)
#   [hm + 24]  vals_ptr  (qword)  — pointer to values array (cap * 8 bytes)
#   [hm + 32]  flags_ptr (qword)  — pointer to flags array  (cap bytes, 0=empty 1=live 2=tombstone)
#
# Keys and values are 64-bit integers. For string keys, store
# a hash or pointer; the caller is responsible for hashing.
#
# Allocation: mmap; free: munmap.
# Growth: doubles capacity when load factor exceeds 70%.

import mem.sl

# ── Hash function ─────────────────────────────────────────────

#__hm_hash [* | key] -> [* | hash]
# Integer hash (splitmix64-style mixing)
word __hm_hash
    dup 30 shr bxor
    0xbf58476d1ce4e5b9 *
    dup 27 shr bxor
    0x94d049bb133111eb *
    dup 31 shr bxor
end

# ── Accessors ─────────────────────────────────────────────────

#hm_count [* | hm] -> [* | count]
word hm_count @ end

#hm_capacity [* | hm] -> [* | cap]
word hm_capacity 8 + @ end

#hm_keys [* | hm] -> [* | ptr]
word hm_keys 16 + @ end

#hm_vals [* | hm] -> [* | ptr]
word hm_vals 24 + @ end

#hm_flags [* | hm] -> [* | ptr]
word hm_flags 32 + @ end

# ── Constructor / Destructor ──────────────────────────────────

#hm_new [* | cap_hint] -> [* | hm]
# Create a new hash map. Capacity is rounded up to next power of 2 (min 8).
# Note: alloc uses mmap(MAP_ANONYMOUS) which returns zeroed pages.
word hm_new
    dup 8 < if drop 8 end
    # Round up to power of 2
    1 while 2dup swap < do 2 * end nip

    >r  # r0 = cap

    # Allocate header (40 bytes)
    40 alloc  # stack: [* | hm]

    # count = 0
    0 over swap !

    # capacity
    r@ over 8 + swap !

    # keys array: cap * 8 (zeroed by mmap)
    r@ 8 * alloc
    over 16 + swap !

    # vals array: cap * 8 (zeroed by mmap)
    r@ 8 * alloc
    over 24 + swap !

    # flags array: cap bytes (zeroed by mmap)
    r> alloc
    over 32 + swap !
end

#hm_free [* | hm] -> [*]
# Free a hash map and all its internal buffers.
word hm_free
    dup hm_capacity >r
    dup hm_keys r@ 8 * free
    dup hm_vals r@ 8 * free
    dup hm_flags r> free
    40 free
end

# ── Core probe: find slot in assembly ─────────────────────────

#__hm_probe [*, hm | key] -> [*, slot_idx | found_flag]
# Linear probe. Returns slot index and 1 if found, or first empty slot and 0.
:asm __hm_probe {
    ; TOS = key, NOS = hm
    push r14                 ; save callee-saved reg
    mov rdi, [r12]          ; key
    mov rsi, [r12 + 8]      ; hm ptr

    ; Hash the key
    mov rax, rdi
    mov rcx, rax
    shr rcx, 30
    xor rax, rcx
    mov rcx, 0xbf58476d1ce4e5b9
    imul rax, rcx
    mov rcx, rax
    shr rcx, 27
    xor rax, rcx
    mov rcx, 0x94d049bb133111eb
    imul rax, rcx
    mov rcx, rax
    shr rcx, 31
    xor rax, rcx
    ; rax = hash

    mov r8, [rsi + 8]       ; capacity
    mov r9, r8
    dec r9                   ; mask = cap - 1
    and rax, r9              ; idx = hash & mask

    mov r10, [rsi + 16]     ; keys_ptr
    mov r11, [rsi + 32]     ; flags_ptr

    ; r14 = first tombstone slot (-1 = none)
    mov r14, -1

.loop:
    movzx ecx, byte [r11 + rax]   ; flags[idx]

    cmp ecx, 0              ; empty?
    je .empty

    cmp ecx, 2              ; tombstone?
    je .tombstone

    ; live: check key match
    cmp rdi, [r10 + rax*8]
    je .found

    ; advance
    inc rax
    and rax, r9
    jmp .loop

.tombstone:
    ; remember first tombstone
    cmp r14, -1
    jne .skip_save
    mov r14, rax
.skip_save:
    inc rax
    and rax, r9
    jmp .loop

.empty:
    ; Use first tombstone if available
    cmp r14, -1
    je .use_empty
    mov rax, r14
.use_empty:
    ; Return: slot=rax, found=0
    mov [r12 + 8], rax      ; overwrite hm slot with idx
    mov qword [r12], 0      ; found = 0
    pop r14
    ret

.found:
    ; Return: slot=rax, found=1
    mov [r12 + 8], rax
    mov qword [r12], 1
    pop r14
} ;

# ── Internal: rehash ──────────────────────────────────────────

#__hm_rehash [* | hm] -> [* | hm]
# Double capacity and re-insert all live entries.
# Strategy: create a new map, insert live entries with hm_set,
# then swap internals and free the temporary/new header.
word __hm_rehash
    dup hm_keys >r
    dup hm_vals >r
    dup hm_flags >r
    dup hm_capacity >r

    r@ 2 * hm_new
    3 rpick 2 rpick 1 rpick

    r@ for
        dup c@ 1 == if
            2 pick @
            2 pick @
            5 pick
            -rot
            hm_set
            drop
        end
        rot 8 + -rot
        swap 8 + swap
        1 +
    end

    drop drop drop

    3 rpick r@ 8 * free
    2 rpick r@ 8 * free
    1 rpick r@ free

    dup @ 2 pick swap !
    dup 8 + @ 2 pick 8 + swap !
    dup 16 + @ 2 pick 16 + swap !
    dup 24 + @ 2 pick 24 + swap !
    dup 32 + @ 2 pick 32 + swap !

    swap >r
    40 free
    r>

    rdrop rdrop rdrop rdrop
end

# ── Public API ────────────────────────────────────────────────

#hm_set [*, hm, key | val] -> [* | hm]
# Insert or update a key-value pair. Returns the (possibly moved) hm.
word hm_set
    >r >r   # r0 = val, r1 = key, stack: [* | hm]
    # Return stack: [... | val | key] (key on top, 0 rpick=key, 1 rpick=val)

    # Check load: count * 10 >= capacity * 7 → rehash
    dup hm_count 10 * over hm_capacity 7 * >= if
        __hm_rehash
    end

    # Probe for key (r@ = key, top of return stack)
    dup r@ __hm_probe  # stack: [*, hm | slot, found]

    swap >r  # push slot; R: [val, key, slot]
    # Now: 0 rpick=slot, 1 rpick=key, 2 rpick=val

    # Store key at keys[slot]
    over hm_keys r@ 8 * + 1 rpick !
    # Store val at vals[slot]
    over hm_vals r@ 8 * + 2 rpick !
    # Set flag = 1
    over hm_flags r> + 1 c!

    # If found=0 (new entry), increment count
    0 == if
        dup @ 1 + over swap !
    end

    rdrop rdrop  # drop key, val
end

#hm_get [*, hm | key] -> [*, hm | val, found_flag]
# Look up a key. Returns (val 1) if found, (0 0) if not.
word hm_get
    over swap __hm_probe   # stack: [*, hm | slot, found]
    dup 0 == if
        nip 0 swap         # stack: [*, hm | 0, 0]
    else
        swap
        2 pick hm_vals swap 8 * + @
        swap               # stack: [*, hm | val, 1]
    end
end

#hm_has [*, hm | key] -> [*, hm | bool]
# Check if key exists. Returns 1 or 0.
word hm_has
    hm_get nip
end

#hm_del [*, hm | key] -> [*, hm | deleted_flag]
# Delete a key. Returns 1 if deleted, 0 if not found.
word hm_del
    over swap __hm_probe   # stack: [*, hm | slot, found]
    dup 0 == if
        nip                # stack: [*, hm | 0]
    else
        drop               # drop found=1; stack: [*, hm | slot]
        # Set flag to tombstone (2)
        over hm_flags over + 2 c!
        drop               # drop slot
        # Decrement count
        dup @ 1 - over swap !
        1                  # stack: [*, hm | 1]
    end
end

#__hm_bzero [*, len | addr] -> [*]
# Zero len bytes at addr
word __hm_bzero
    swap
    0 memset_bytes
end

#hm_clear [* | hm] -> [*]
# Remove all entries without freeing the map.
word hm_clear
    dup 0 !  # count = 0
    dup hm_capacity
    over hm_flags __hm_bzero
end
