import stdlib.sl
import io.sl
import mem.sl

word digitsN>num  # ( d_{n-1} ... d0 n -- value ), digits bottom=MSD, top=LSD, length on top (MSD-most significant digit, LSD-least significant digit)
    0 swap        # place accumulator below length
    for           # loop n times using the length on top
        r@ pick   # fetch next digit starting from MSD (uses loop counter as index)
        swap      # acc on top
        10 *      # acc *= 10
        +         # acc += digit
    end
end


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
    rdrop
end

word count_digits
    0
    swap
    while dup 0 > do
        10 / swap 1 + swap
    end
    drop
end

word tostr
    dup
    count_digits
    2dup >r alloc
    nip swap rot swap
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
        swap 48 + c!
        drop
        swap
        1 +
        swap
    end

    swap 0 +
    pick 1 +
    over for
    rot drop
    end drop
    swap 1 + swap puts
end

word main
    "1234" toint 1 + dup puti cr
    tostr
    puts
end
