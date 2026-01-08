import stdlib/stdlib.sl
import stdlib/io.sl
import fn.sl

word main
    2 40 +
    puti cr
    extend-syntax
    foo(1, 2)
    puti cr
    0
end

fn foo(int a, int b){
    return a + b;
}