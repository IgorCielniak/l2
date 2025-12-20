
global raw_add
section .text
raw_add:
    ; expects args on stack: [r12]=b, [r12+8]=a
    ; returns sum on stack
    mov rax, [r12]       ; b
    add r12, 8
    mov rbx, [r12]       ; a
    add rax, rbx
    mov [r12], rax
    ret
