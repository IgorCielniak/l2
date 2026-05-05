import stdlib.sl
import gvars.sl
import debug.sl

# sma - small/slow/shit memory allocator

sized_global blocks 16

# [ blocks_count blocks_ptr ]
# [ ptr size ptr1 size1 ... ptrN sizeN  ]

# blocks_init [*] -> [*]
word blocks_init
    blocks 8 + dup @ 0 == if
        16 alloc !
    else
        blocks 8 + @
        blocks @ 16 *
        blocks @ 1 + 16 *
        realloc !
    end
end

# salloc [* | size] -> [* | ptr]
word salloc
    blocks_init
    blocks @ 1 + blocks swap !
    blocks 8 + @
    blocks @ 1 - 8 * 2 * +
    over dup alloc dup
    3 pick swap !
    -rot swap 8 + swap !
    nip
end

# sfree [* | ptr] -> [*]
word sfree
    blocks 8 + @
    blocks @
    while dup 0 > do
        2dup 1 - 16 * + dup dup @
        5 pick == if
            dup @ swap 8 + @ free
            nip dup 0 ! 8 + 0 !
            blocks @ 1 - blocks swap !
            2drop
            blocks @ 0 == if
                blocks 8 + @
                blocks @ 1 + 16 *
                free
                blocks 8 + 0 !
            else
                blocks_defrag
            end
            drop ret
        end
        2drop
        1 -
    end drop
end

# srealloc [*, ptr | new_size] -> [* | new_ptr]
word srealloc
    0 while dup blocks @ < do
        blocks 8 + @ over 16 * + @ 3 pick == if
            dup 16 * blocks 8 + @ +
            dup 8 + @ swap dup @ 2
            pick 5 pick realloc dup
            -rot over swap ! 8 + 4
            pick ! nip nip nip nip ret
        end
        1 +
    end
end

# copy_alive_block_records [*, old_count, old_addr | new_addr] -> [*]
word copy_alive_block_records
    2 pick for
        2dup 4 pick 2 *
        2 pick dup @ swap 8 + @
        swap 3 pick dup rot dup 0 > if
            ! 8 + swap !
            2drop drop
            16 + swap 16 + rot 1 - -rot swap
        else
            2drop 2drop 2drop drop
            swap 16 + rot 1 - -rot swap
        end
    end

    2drop drop
end

# blocks_defrag [*] -> [*]
word blocks_defrag
    blocks @ 16 * alloc dup
    blocks @ 1 +
    blocks 8 + @
    2 pick
    copy_alive_block_records
    blocks 8 + @ blocks @ 1 + 16 * free
    blocks 8 + swap !
end

# dump_blocks [*] -> [*]
word dump_blocks
    blocks @ "blocks count: " write_buf puti cr cr
    blocks 8 + @ dup "blocks buffer ptr: " write_buf puti cr cr
    "blocks pairs: " puts
    blocks @ 2 * memdump drop
end
