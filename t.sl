import stdlib.sl
import fn.sl

fn foo(int a){
    return a + 1;
}

: main
    extend-syntax
    foo(1)
    puts
    0
;