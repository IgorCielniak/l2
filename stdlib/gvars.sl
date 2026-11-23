import stdlib.sl

# gvars-accessor [*, nameTok | label] -> [*]
# Emits `:asm <name> { lea rax, [rel <label>] sub r12, 8 mov [r12], rax } ;`
# The `:asm`, name, and label tokens are made from the user's name token,
# so the definition is located at the user's declaration.
word gvars-accessor
    over swap token-with-lexeme             # nameTok labelTok
    over ":asm" token-with-lexeme           # nameTok labelTok asmTok
    list-new swap list-append 2 pick list-append list>syntax   # nameTok labelTok head
    `{ lea rax, [rel` syntax-concat
    swap list-new swap list-append list>syntax syntax-concat
    `] sub r12, 8 mov [r12], rax } ;` syntax-concat
    nip emit
end
compile-only

# global <name>: an 8-byte zeroed variable; <name> pushes its address.
word global
    ct-parser-consume                       # nameTok
    dup token-lexeme "_global" string-append    # nameTok label
    dup ": dq 0" string-append ct-output-append-data
    gvars-accessor
end
immediate
compile-only

# sized_global <name> <bytes>: a zeroed variable of <bytes> bytes.
word sized_global
    ct-parser-consume                       # nameTok
    dup token-lexeme "_global" string-append    # nameTok label
    ct-parser-consume token-lexeme          # nameTok label size
    over ": times " string-append swap string-append " db 0" string-append
    ct-output-append-data
    gvars-accessor
end
immediate
compile-only
