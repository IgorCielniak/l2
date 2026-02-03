import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    6 3 band puti cr
    6 3 bor puti cr
    6 3 bxor puti cr
    0 bnot puti cr
    1 3 shl puti cr
    1 3 sal puti cr
    8 1 shr puti cr
    8 neg 1 sar puti cr
    5 inc puti cr
    5 dec puti cr
    3 7 min puti cr
    3 7 max puti cr
    1 3 rol puti cr
    8 1 ror puti cr
    5 1 4 clamp puti cr
    0 1 4 clamp puti cr
    3 1 4 clamp puti cr
    time 0 > puti cr
    1 mem swap !
    rand puti cr
    0
end
