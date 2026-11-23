import stdlib/stdlib.sl

macro make_push_const (name value) `
    :asm $name {
        sub r12, 8
        mov qword [r12], $value
    }
    ;
` emit ;

make_push_const push42 42

word main
    push42 puti cr
end
