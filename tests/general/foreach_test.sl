import stdlib.sl
import arr.sl

word main
    { 1 2 3 } dup dup

    dup @ swap 8 + swap 0 swap for
        2dup 8 * + @ puti cr
        1 +
    end 2drop

    foreach
        1 + puti cr
    end

    foreachwith i
        1 i + puti cr
    end
end
