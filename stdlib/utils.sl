
#strcmp [*, addr, len, addr | len] -> [*, addr, len, addr, len | bool]
word strcmp
    3 pick 2 pick @ swap @ ==
end

#strconcat [*, addr, len, addr | len] -> [*, addr | len]
word strconcat
    0 pick 3 pick +
    dup
    >r >r >r >r >r >r
    5 rpick
    alloc
    r> r>
    dup >r
    memcpy
    swap
    r> dup -rot +
    r> r>
    memcpy
    swap
    3 pick
    -
    swap
    drop
    swap
    0 rpick
    nip
    rot
    drop
    rdrop rdrop rdrop
end

#strlen [* | addr] -> [* | len]
# for null terminated strings
word strlen
    0 swap                  # len addr
    while dup c@ 0 != do
        1 +                 # addr++
        swap 1 + swap       # len++
    end
    drop                    # drop addr, leave len
end

#digitsN>num [*, d_{n-1}, d0 | n] -> [* | value]
word digitsN>num  # digits bottom=MSD, top=LSD, length on top (MSD-most significant digit, LSD-least significant digit)
    0 swap        # place accumulator below length
    for           # loop n times using the length on top
        r@ pick   # fetch next digit starting from MSD (uses loop counter as index)
        swap      # acc on top
        10 *      # acc *= 10
        +         # acc += digit
    end
end

#toint [*, addr | len] -> [* | int]
# converts a string to an int
word toint
    swap
    over 0 swap
    dup >r
    for
        over over +
        c@ 48 -
        swap rot
        swap
        1 +
    end
    2drop
    r>
    dup >r
    digitsN>num
    r> 1 +
    for
        swap drop
    end
    rdrop rdrop
end

#count_digits [* | int] -> [* | int]
# returns the amount of digits of an int
word count_digits
    0
    swap
    while dup 0 > do
        10 / swap 1 + swap
    end
    drop
end

#tostr [* | int] -> [*, addr | len]
# the function allocates a buffer, remember to free it
word tostr
    dup
    count_digits
    2dup >r alloc
    swap rot swap
    for
        dup 10 % swap 10 /
    end
    drop

    r>
    1 swap dup
    for
        dup
        2 + pick
        2 pick
        2 + pick
        3 pick rot +
        swap 48 + swap 1 - swap c!
        swap
        1 +
        swap
    end

    swap 0 +
    pick 1 +
    over for
    rot drop
    end drop
end
