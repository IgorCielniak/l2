import stdlib.sl
import gvars.sl

# hma - heap memory allocator

# Heap allocator (explicit free list) with split/coalesce + realloc.
#
# Block layout (all sizes in bytes, qword aligned):
#   [block + 0]  size|flag (qword)   flag=1 => allocated
#   [block + 8]  prev_free (qword)   only valid when free
#   [block + 16] next_free (qword)   only valid when free
#   [block + N-8] size|flag (qword)  footer
#
# Payload pointer returned to user = block + 8.
# Minimum free block size is 32 bytes (header+footer+prev+next).
#
# halloc/hfree/hrealloc auto-initialize the heap.
# Regions are automatically unmapped when completely freed.

sized_global h_state 32

# ---- State accessors ----

word h_init_flag@
    h_state @
end

word h_init_flag!
    h_state swap !
end

word h_free_head@
    h_state 8 + @
end

word h_free_head!
    h_state 8 + swap !
end

word h_regions@
    h_state 16 + @
end

word h_regions!
    h_state 16 + swap !
end

word h_grow_size@
    h_state 24 + @
end

word h_grow_size!
    h_state 24 + swap !
end

# ---- Region node accessors ----

# h_region_base@ [* | node] -> [* | base]
word h_region_base@
    @
end

# h_region_size@ [* | node] -> [* | size]
word h_region_size@
    8 + @
end

# h_region_next@ [* | node] -> [* | next]
word h_region_next@
    16 + @
end

# h_region_base! [*, node | base] -> [*]
word h_region_base!
    !
end

# h_region_size! [*, node | size] -> [*]
word h_region_size!
    swap 8 + swap !
end

# h_region_next! [*, node | next] -> [*]
word h_region_next!
    swap 16 + swap !
end

# ---- Helpers ----

# h_align [* | size] -> [* | aligned]
word h_align
    7 + 7 bnot band
end

# h_page_align [* | size] -> [* | aligned]
word h_page_align
    4095 + 4095 bnot band
end

# h_mask_size [* | size_flags] -> [* | size]
word h_mask_size
    1 bnot band
end

# h_block_size [* | block] -> [* | size]
word h_block_size
    @ h_mask_size
end

# h_block_alloc? [* | block] -> [* | flag]
word h_block_alloc?
    @ 1 band
end

# h_free_prev@ [* | block] -> [* | prev]
word h_free_prev@
    8 + @
end

# h_free_next@ [* | block] -> [* | next]
word h_free_next@
    16 + @
end

# h_free_prev! [*, block | prev] -> [*]
word h_free_prev!
    swap 8 + swap !
end

# h_free_next! [*, block | next] -> [*]
word h_free_next!
    swap 16 + swap !
end

# h_block_set [*, block, size | flag] -> [* | block]
# Writes header/footer with size|flag. Size includes header+footer.
word h_block_set
    rot >r
    bor
    dup r@ swap !
    dup h_mask_size r@ + 8 - swap !
    r>
end

# h_req_block_size [* | size] -> [* | block_size]
word h_req_block_size
    dup 0 <= if drop 0 ret end
    h_align
    16 +
    dup 32 < if drop 32 end
end

# ---- Free list ----

# h_free_list_insert [* | block] -> [*]
word h_free_list_insert
    h_free_head@ >r
    dup 0 h_free_prev!
    dup r@ h_free_next!
    r@ 0 != if
        dup r@ swap h_free_prev!
    end
    rdrop
    h_free_head!
end

# h_free_list_remove [* | block] -> [*]
word h_free_list_remove
    dup h_free_prev@ >r
    dup h_free_next@ >r
    drop

    1 rpick 0 == if
        r@ h_free_head!
    else
        1 rpick r@ h_free_next!
    end

    r@ 0 != if
        r@ 1 rpick h_free_prev!
    end

    rdrop rdrop
end

# h_find_fit [* | size] -> [* | block_or_0]
word h_find_fit
    >r
    h_free_head@
    while dup 0 != do
        dup h_block_size r@ >= if
            rdrop
            ret
        end
        h_free_next@
    end
    rdrop
end

# ---- Regions ----

# h_region_add [* | size] -> [*]
# Adds a new mmap region with prologue/epilogue sentinels.
word h_region_add
    h_page_align
    dup 4096 < if drop 4096 end
    dup >r
    dup alloc              # stack: [size | base]
    dup >r                 # save base for node creation

    # prologue (allocated, size=16)
    dup 16 1 h_block_set drop

    # main free block
    dup 16 +               # free_block = base + 16
    2 pick 32 -            # free_size = size - 32
    0 h_block_set
    dup h_free_list_insert drop

    # epilogue (allocated, size=16)
    over + 16 -            # epilogue_addr = base + size - 16
    dup 16 1 h_block_set drop
    drop                   # drop epilogue addr

    drop                   # drop size

    # region node
    24 alloc dup >r
    r@ 1 rpick h_region_base!
    r@ 2 rpick h_region_size!
    r@ h_regions@ h_region_next!
    r@ h_regions!
    drop
    rdrop rdrop rdrop
end

# ---- Automatic Region Cleanup ----

# h_should_unmap_region [*, block | block | flag]
# Check if a block spans an entire region. If yes, unmap and return 1.
# Otherwise return 0 and block remains on stack.
word h_should_unmap_region
    >r >r               # R: [block | block_size | original_R...]
    
    h_regions@
    while dup 0 != do
        dup >r          # R: [...| current_region]
        
        # Check: block == current.base+16 and block_size == current.size-32
        # On return stack: current (0rpick), block (1rpick), block_size (2rpick)
        0 rpick h_region_base@ 16 + 1 rpick == if
            0 rpick h_region_size@ 32 - 2 rpick == if
                # MATCH! This block spans entire region - unmap it
                
                # Unlink region from list
                h_regions@ dup 0 rpick == if
                    # Region is at head
                    drop
                    0 rpick h_region_next@ h_regions!
                else
                    # Find previous region and unlink
                    while dup h_region_next@ 0 rpick != do
                        h_region_next@
                    end
                    dup h_region_next@ swap h_region_next!
                    drop
                end
                
                # Unmap the region
                0 rpick h_region_base@ over h_region_size@ free
                # Free the region node (24 bytes)
                0 rpick 24 free drop
                
                # Clean return stack and return 1
                rdrop rdrop rdrop      # drop current, block_size, block
                1
                ret
            end
        end
        
        rdrop          # drop current_region
        h_region_next@
    end
    
    # No region matched - return 0 with block preserved on stack
    drop
    rdrop rdrop        # drop block_size and block from R
    0
end

# ---- Init / Shutdown ----

# hinit [* | heap_bytes] -> [*]
word hinit
    # Reset state unconditionally to avoid stale persistent data.
    0 h_free_head!
    0 h_regions!
    0 h_grow_size!
    0 h_init_flag!
    dup 0 <= if drop 1048576 end
    h_page_align
    dup 4096 < if drop 4096 end
    dup h_grow_size!
    h_region_add
    1 h_init_flag!
end

# hshutdown [*] -> [*]
word hshutdown
    h_regions@
    while dup 0 != do
        dup h_region_next@ >r
        dup h_region_base@ over h_region_size@ free
        dup 24 free drop
        r>
    end
    drop
    0 h_regions!
    0 h_free_head!
    0 h_grow_size!
    0 h_init_flag!
end

# h_ensure_init [*] -> [*]
word h_ensure_init
    h_init_flag@ 0 == if
        1048576 hinit
    end
end

# h_grow [* | min_bytes] -> [*]
word h_grow
    h_grow_size@ max
    h_region_add
end

# ---- Public API ----

# halloc [* | size] -> [* | ptr]
word halloc
    h_ensure_init
    h_req_block_size
    dup 0 == if drop 0 ret end
    >r

    r@ h_find_fit
    dup 0 == if
        drop r@ h_grow
        r@ h_find_fit
    end

    dup 0 == if
        rdrop
        0 ret
    end

    dup h_free_list_remove
    dup h_block_size r@ -
    dup 32 >= if
        over r@ +
        -rot swap >r
        0 h_block_set
        h_free_list_insert
        r>
        dup 8 + swap
        r@ 1 h_block_set
        drop
    else
        drop
        dup h_block_size
        over 8 +
        -rot
        1 h_block_set
        drop
    end
    rdrop
end

# hfree [* | ptr] -> [*]
word hfree
    h_init_flag@ 0 == if drop ret end
    dup 0 == if drop ret end
    8 -
    dup h_block_size >r

    # merge with previous block if free
    dup 8 - @ h_mask_size
    over swap -
    dup h_block_alloc? 0 == if
        dup h_free_list_remove
        dup h_block_size r@ +
        r> drop >r
        swap drop
    else
        drop
    end

    # merge with next block if free
    dup r@ +
    dup h_block_alloc? 0 == if
        dup h_free_list_remove
        dup h_block_size r@ +
        r> drop >r
        drop
    else
        drop
    end

    r@ 0 h_block_set
    dup r@ h_should_unmap_region
    0 == if
        h_free_list_insert
    end
    drop
    rdrop
end

# hrealloc [*, ptr | new_size] -> [* | new_ptr]
word hrealloc
    over 0 == if
        swap drop
        halloc
        ret
    end
    dup 0 == if
        drop
        hfree
        0
        ret
    end

    dup h_req_block_size >r
    over 8 -
    dup h_block_size dup 16 - >r drop

    # shrink in place
    over r@ <= if
        swap drop
        dup h_block_size 1 rpick -
        dup 32 >= if
            over 1 rpick +
            -rot swap >r
            0 h_block_set
            h_free_list_insert
            r>
            1 rpick 1 h_block_set drop
        else
            drop drop
        end
        rdrop rdrop
        ret
    end

    # try to expand in place
    dup h_block_size over +
    dup h_block_alloc? 0 == if
        over h_block_size
        over h_block_size
        +
        dup 1 rpick >= if
            1 pick h_free_list_remove
            dup 1 rpick -
            dup 32 >= if
                3 pick 1 rpick +
                swap 0 h_block_set
                h_free_list_insert
                drop
                drop
                swap drop
                1 rpick 1 h_block_set drop
            else
                drop
                swap drop
                rot drop
                1 h_block_set drop
            end
            rdrop rdrop
            ret
        else
            drop
            drop
        end
    else
        drop
    end

    # fallback: allocate new, copy, free old
    over halloc dup 0 == if
        drop drop drop
        rdrop rdrop
        0 ret
    end

    2 pick r@ min
    rot drop
    3 pick >r
    rot drop
    rot swap
    memcpy drop
    r> hfree
    rdrop rdrop
end

# dump_blocks [*] -> [*]
# Debug helper: print free list blocks and sizes.
word dump_blocks
    "free list:" puts
    h_free_head@
    while dup 0 != do
        "block addr: " write_buf dup puti cr
        "block size: " write_buf dup h_block_size puti cr
        h_free_next@
    end
    drop
end

