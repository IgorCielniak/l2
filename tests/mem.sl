import ../stdlib/stdlib.sl

word main
    mem 5 !
    mem 8 + 6 !
    mem @ puti cr
    mem 8 + @ puti cr

    10 alloc
    dup
    10 97 memset
    10 2dup puts
    free


    # Test realloc: allocate 16 bytes, fill with data, then grow to 32 bytes
    16 alloc
    dup 111 !       # Store 111 at offset 0
    dup 8 + 222 !   # Store 222 at offset 8

    # Realloc to 32 bytes
    16 32 realloc  # ( addr old_len=16 new_len=32 ) -> ( new_addr )

    # Verify old data is preserved
    dup @ puti cr         # Should print 111
    dup 8 + @ puti cr     # Should print 222

    # Write new data to the expanded region
    dup 16 + 333 !        # Store 333 at offset 16
    dup 24 + 444 !        # Store 444 at offset 24

    # Print all values to verify
    dup @ puti cr         # 111
    dup 8 + @ puti cr     # 222
    dup 16 + @ puti cr    # 333
    dup 24 + @ puti cr    # 444

    32 free
end
