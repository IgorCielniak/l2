import stdlib/stdlib.sl

macro make_push_const 2
    :asm $0 {
        sub r12, 8
        mov qword [r12], $1
    }
    ;
;

make_push_const push42 42

word main
    push42 puti cr
end
