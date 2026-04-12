import stdlib.sl

word main
    5          # loop count
    get_addr   # push loop target address (next instruction)
    "gg" puts

    swap       # bring count to top
    1 -        # decrement
    dup 0 > if
        swap   # put addr on top
        dup    # keep a copy for next iteration
        jmp
    else
        drop   # drop count
        drop   # drop addr
    end
end