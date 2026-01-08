import ../stdlib/stdlib.sl
import ../stdlib/io.sl
import ../fn.sl

: main
    2 40 +
    puti cr
    extend-syntax
    foo(1, 2)
    puti cr
    0
;

fn foo(int a, int b){
    return a + b;
}
