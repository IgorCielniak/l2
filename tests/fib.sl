import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../stdlib/debug.sl

word main
    1 1 2dup 2dup puti cr puti cr
    +
    dup puti cr
    rot
    22 dup >r for
        2dup + dup puti cr
        rot
    end
    "-------" puts
    r> 3 + puti
    " numbers printed from the fibonaci sequence" puts
    0
end

word main2
    1 2 while over 100 < do
        over puti cr
        swap over +
    end
end

