import ../stdlib/stdlib.sl
import ../stdlib/io.sl

word main
    mem 5 swap !
    mem 8 + 6 swap !
    mem @ puti cr
    mem 8 + @ puti cr
end
