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
:asm __hm_hash {
    mov rax, [r12]
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
    mov [r12], rax
} ;

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
# Strategy: create new map, copy entries, swap internals, free old arrays.
:asm __hm_rehash {
    push r14                   ; save callee-saved regs
    push r15
    mov rbx, [r12]            ; hm

    ; Load old state
    mov r8, [rbx + 8]         ; old_cap
    mov r9, [rbx + 16]        ; old_keys
    mov r10, [rbx + 24]       ; old_vals
    mov r11, [rbx + 32]       ; old_flags

    ; New capacity = old_cap * 2
    mov rdi, r8
    shl rdi, 1                ; new_cap

    ; Save hm, old_cap, old_keys, old_vals, old_flags, new_cap on x86 stack
    push rbx
    push r8
    push r9
    push r10
    push r11
    push rdi

    ; Allocate new_keys = alloc(new_cap * 8)
    ; mmap(0, size, PROT_READ|PROT_WRITE=3, MAP_PRIVATE|MAP_ANON=34, -1, 0)
    mov rax, 9
    xor rdi, rdi
    mov rsi, [rsp]            ; new_cap
    shl rsi, 3                ; new_cap * 8
    mov rdx, 3
    mov r10, 34
    push r8                   ; save r8
    mov r8, -1
    xor r9, r9
    syscall
    pop r8
    push rax                  ; save new_keys

    ; Allocate new_vals = alloc(new_cap * 8)
    mov rax, 9
    xor rdi, rdi
    mov rsi, [rsp + 8]        ; new_cap
    shl rsi, 3
    mov rdx, 3
    mov r10, 34
    push r8
    mov r8, -1
    xor r9, r9
    syscall
    pop r8
    push rax                  ; save new_vals

    ; Allocate new_flags = alloc(new_cap)
    mov rax, 9
    xor rdi, rdi
    mov rsi, [rsp + 16]       ; new_cap
    mov rdx, 3
    mov r10, 34
    push r8
    mov r8, -1
    xor r9, r9
    syscall
    pop r8
    push rax                  ; save new_flags

    ; Stack: new_flags, new_vals, new_keys, new_cap, old_flags, old_vals, old_keys, old_cap, hm
    ; Offsets: [rsp]=new_flags, [rsp+8]=new_vals, [rsp+16]=new_keys
    ;          [rsp+24]=new_cap, [rsp+32]=old_flags, [rsp+40]=old_vals
    ;          [rsp+48]=old_keys, [rsp+56]=old_cap, [rsp+64]=hm

    mov r14, [rsp + 24]       ; new_cap
    dec r14                    ; new_mask

    ; Re-insert loop: for i in 0..old_cap
    xor rcx, rcx              ; i = 0
    mov r8, [rsp + 56]        ; old_cap
.rehash_loop:
    cmp rcx, r8
    jge .rehash_done

    ; Check old_flags[i]
    mov rdi, [rsp + 32]       ; old_flags
    movzx eax, byte [rdi + rcx]
    cmp eax, 1                ; live?
    jne .rehash_next

    ; Get key and val
    mov rdi, [rsp + 48]       ; old_keys
    mov rsi, [rdi + rcx*8]    ; key
    mov rdi, [rsp + 40]       ; old_vals
    mov rdx, [rdi + rcx*8]    ; val

    ; Hash key to find slot in new map
    push rcx
    push rsi
    push rdx

    ; Hash rsi (key)
    mov rax, rsi
    mov rbx, rax
    shr rbx, 30
    xor rax, rbx
    mov rbx, 0xbf58476d1ce4e5b9
    imul rax, rbx
    mov rbx, rax
    shr rbx, 27
    xor rax, rbx
    mov rbx, 0x94d049bb133111eb
    imul rax, rbx
    mov rbx, rax
    shr rbx, 31
    xor rax, rbx
    and rax, r14              ; slot = hash & new_mask

    ; Linear probe (new map is all empty, so first empty slot is fine)
    mov rdi, [rsp + 24]       ; new_flags (3 pushes offset: +24)
.probe_new:
    movzx ebx, byte [rdi + rax]
    cmp ebx, 0
    je .probe_found
    inc rax
    and rax, r14
    jmp .probe_new
.probe_found:
    ; Store key, val, flag
    pop rdx                    ; val
    pop rsi                    ; key
    mov rdi, [rsp + 16 + 8]   ; new_keys (adjusted for 1 remaining push: rcx)
    mov [rdi + rax*8], rsi
    mov rdi, [rsp + 8 + 8]    ; new_vals
    mov [rdi + rax*8], rdx
    mov rdi, [rsp + 0 + 8]    ; new_flags
    mov byte [rdi + rax], 1
    pop rcx                    ; restore i

.rehash_next:
    inc rcx
    jmp .rehash_loop

.rehash_done:
    ; Free old arrays
    ; munmap(old_keys, old_cap * 8)
    mov rax, 11
    mov rdi, [rsp + 48]       ; old_keys
    mov rsi, [rsp + 56]       ; old_cap
    shl rsi, 3
    syscall

    ; munmap(old_vals, old_cap * 8)
    mov rax, 11
    mov rdi, [rsp + 40]       ; old_vals
    mov rsi, [rsp + 56]
    shl rsi, 3
    syscall

    ; munmap(old_flags, old_cap)
    mov rax, 11
    mov rdi, [rsp + 32]       ; old_flags
    mov rsi, [rsp + 56]       ; old_cap
    syscall

    ; Update hm header
    mov rbx, [rsp + 64]       ; hm
    mov rax, [rsp + 24]       ; new_cap
    mov [rbx + 8], rax
    mov rax, [rsp + 16]       ; new_keys
    mov [rbx + 16], rax
    mov rax, [rsp + 8]        ; new_vals
    mov [rbx + 24], rax
    mov rax, [rsp]            ; new_flags
    mov [rbx + 32], rax

    ; Clean up x86 stack (9 pushes + 2 callee-saved)
    add rsp, 72
    pop r15
    pop r14

    ; hm is still on r12 stack, unchanged
} ;

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
:asm __hm_bzero {
    mov rdi, [r12]        ; addr
    mov rcx, [r12 + 8]    ; len
    add r12, 16
    xor al, al
    rep stosb
} ;

#hm_clear [* | hm] -> [*]
# Remove all entries without freeing the map.
word hm_clear
    dup 0 !  # count = 0
    dup hm_capacity
    over hm_flags __hm_bzero
end
