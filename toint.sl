import stdlib.sl
import io.sl

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

word main
    "1234" toint 1 + puti cr
end
