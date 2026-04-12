import stdlib/stdlib.sl
import stdlib/io.sl

word main
    3
    label loop
    dup puti cr
    1 -
    dup 0 >
    if goto loop end
    drop
end