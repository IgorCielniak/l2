import stdlib/stdlib.sl
import stdlib/io.sl
import libs/fn.sl

fn foo(int a, int b){
    1
    puti cr
    return a b +;
}

fn bar(int a, int b){
    return a + b;
}

word main
    extend-syntax
    foo(3, 2)
    puti cr
    bar(1, 2)
    puti cr
end