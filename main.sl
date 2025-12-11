import stdlib.sl
import fn.sl

: main
    2 40 +
    puts
    extend-syntax
    foo(1, 2)
    puts
    0
;

fn foo(int a, int b){
    return a + b;
}