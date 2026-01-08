import stdlib/stdlib.sl
import stdlib/io.sl
import fn.sl

fn foo(int a, int b){
    1
    puti cr
    return a b +;
}

word main
    extend-syntax
    foo(3, 2)
    puti cr
    0
end