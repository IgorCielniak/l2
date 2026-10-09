import stdlib.sl
import arr.sl

word list_inc
    dup @ 1 + !
end

word main
    {1 2 3 4 5} dup

    &list_inc map

    dup @ for
        8 + dup @ puti cr
    end drop
end
