import stdlib.sl

macro TCGETS 0 0x5401 ;
macro ENOTTY 0 25 ;
macro EBADF 0 9 ;

# isatty [* | fd] -> [* | flag]
word isatty
    >r                           # save fd

    60 alloc                     # push addr
    r@ TCGETS over 3 16 syscall  # addr result

    # Duplicate result and save it
    dup >r                       # push result to return stack

    # Free buffer
    60 swap free                 # free(addr, size)

    # Restore result
    r>                           # result back to data stack

    # Check result
    dup ENOTTY neg == if                # -ENOTTY (not a tty)
        drop 0
    else
        dup EBADF neg == if             # -EBADF (bad fd)
            drop -1
        else
            # Any other value means it's a tty
            drop 1
        end
    end

    rdrop
end
