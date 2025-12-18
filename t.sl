import stdlib/stdlib.sl
import stdlib/io.sl
import fn.sl

fn foo(int a, int b){
    1
    puts
    return a b +;
}

: main
    extend-syntax
    foo(3, 2)
    puts
    0
;