import stdlib/stdlib.sl
import stdlib/io.sl
import libs/fn.sl

use-fn-dsl

defn bad(number x){
    let int i = 1;
    let float f = i;
    return x;
}

word main
    bad(10) puti cr
end
