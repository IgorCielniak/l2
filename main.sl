import stdlib.sl

: main
    2 40 +
    puts
    extend-syntax
    1
    2
    foo()
    puts
    0
;
fn foo(int a, int b){
    return a + b;
}