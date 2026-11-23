import stdlib.sl
# Assembler comments in a quoted :asm body may hold any character.
macro make_push_const (name value) `
    :asm $name {
        ; push the caller's value } (not the end)
        sub r12, 8          ; it's "quoted"
        mov qword [r12], $value
    }
    ;
` emit ;
make_push_const push42 42
word main push42 puti cr end
