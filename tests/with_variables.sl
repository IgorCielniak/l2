import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    3 4 5 6
    with a b c d in
        b puti cr
        a puti cr
        d puti cr
        c puti cr
    end
    0
end
